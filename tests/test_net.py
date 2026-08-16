"""The relay client.

Driven against a fake socket rather than a network, so the reconnect loop, the
queue policy and the room switch are all exercised for real — those are where
the bugs are, and none of them need a relay to reproduce.

The rule the whole module is built around: **voice going quiet must never cost
somebody their trigger.** Several tests below exist only to prove that a
failure stays inside this component.
"""

import queue
import threading

import pytest

from pitradio import net, voice

KEY = "93badd13775354e6c603fbf5553b1c4b"


def clip(driver="Nick Tandy", audio=b"aud") -> bytes:
    return voice.encode_clip(voice.Speaker(driver, (1.0, 2.0, 3.0)), audio, sent_at=1.0)


# -- which room ------------------------------------------------------------


def test_a_room_url_is_the_relay_plus_the_session():
    assert net.room_url("wss://relay.example.com", KEY) == (
        f"wss://relay.example.com/chat/{KEY}")


def test_a_trailing_slash_does_not_double_up():
    assert net.room_url("wss://relay.example.com/", KEY) == (
        f"wss://relay.example.com/chat/{KEY}")


def test_a_relay_under_a_prefix_keeps_it():
    assert net.room_url("wss://example.com/voice", KEY) == (
        f"wss://example.com/voice/chat/{KEY}")


def test_localhost_over_plaintext_is_allowed_for_development():
    assert net.room_url("ws://localhost:8080", KEY) == (
        f"ws://localhost:8080/chat/{KEY}")


@pytest.mark.parametrize("relay", [
    "", "   ", None, "https://relay.example.com", "relay.example.com",
    "wss://", "not a url",
])
def test_an_unusable_relay_is_no_room(relay):
    assert net.room_url(relay, KEY) is None


@pytest.mark.parametrize("key", ["", None, "nope", "../../etc", "A" * 32])
def test_an_unusable_session_is_no_room(key):
    """No multiplayer session is an ordinary answer: voice is simply off."""
    assert net.room_url("wss://relay.example.com", key) is None


# -- backoff ---------------------------------------------------------------


def test_backoff_grows_and_then_stops_growing():
    delays = [net.backoff_seconds(n) for n in range(8)]
    assert delays == sorted(delays)
    assert delays[-1] == net.BACKOFF[-1]


def test_backoff_never_returns_zero():
    """A zero would turn a dead relay into a busy loop on somebody's race PC."""
    assert all(net.backoff_seconds(n) > 0 for n in range(-2, 10))


# -- a fake socket ---------------------------------------------------------


class FakeSocket:
    """Records what was sent, replays what was queued."""

    def __init__(self, incoming=()):
        self.sent: list[bytes] = []
        self.incoming = list(incoming)
        self.closed = False
        self.delivered = threading.Event()

    def send(self, frame: bytes) -> None:
        self.sent.append(frame)

    def recv(self, _timeout):
        if self.incoming:
            frame = self.incoming.pop(0)
            if not self.incoming:
                self.delivered.set()
            return frame
        return None

    def close(self) -> None:
        self.closed = True


@pytest.fixture
def received():
    return queue.Queue()


def run_client(socket, received, url=f"wss://relay.example.com/chat/{KEY}",
               *, wait=True):
    """Start a client against one fake socket and stop it again."""
    client = net.RelayClient(received.put, connect=lambda _url: socket)
    client.start()
    client.set_room(url)
    if wait:
        socket.delivered.wait(timeout=2.0)
    return client


# -- receiving -------------------------------------------------------------


def test_a_received_clip_reaches_the_handler(received):
    socket = FakeSocket([clip("Nyck de Vries", b"hello")])
    client = run_client(socket, received)
    try:
        got = received.get(timeout=2.0)
    finally:
        client.stop()

    assert got.speaker.driver == "Nyck de Vries"
    assert got.audio == b"hello"


def test_a_frame_that_is_not_a_clip_is_dropped_not_delivered(received):
    """Bytes from a stranger's machine. One bad frame costs that clip."""
    socket = FakeSocket([b"junk", clip("Nick Tandy", b"good")])
    client = run_client(socket, received)
    try:
        got = received.get(timeout=2.0)
    finally:
        client.stop()

    assert got.audio == b"good"
    assert received.empty()


def test_a_handler_that_raises_does_not_end_the_connection(received):
    """It runs on this thread; an exception there would take voice down for
    the rest of the session with nothing to show for it."""
    seen = []

    def explode(received_clip):
        seen.append(received_clip)
        raise RuntimeError("handler blew up")

    socket = FakeSocket([clip(audio=b"one"), clip(audio=b"two")])
    client = net.RelayClient(explode, connect=lambda _url: socket)
    client.start()
    client.set_room(f"wss://relay.example.com/chat/{KEY}")
    try:
        socket.delivered.wait(timeout=2.0)
    finally:
        client.stop()

    assert [c.audio for c in seen] == [b"one", b"two"]


# -- sending ---------------------------------------------------------------


def test_a_queued_clip_is_sent(received):
    socket = FakeSocket()
    client = run_client(socket, received, wait=False)
    try:
        client.send(b"a clip")
        deadline = threading.Event()
        deadline.wait(0.5)
    finally:
        client.stop()

    assert socket.sent == [b"a clip"]


def test_sending_with_no_room_is_discarded():
    """Not queued for later: by the time there is a room it is a different
    session, and delivering it there would be worse than losing it."""
    client = net.RelayClient(lambda _clip: None, connect=lambda _url: FakeSocket())
    client.send(b"a clip")
    assert client._outbox.qsize() == 0


def test_a_full_queue_drops_the_oldest():
    """The newest thing said is the one worth hearing, and a full queue means
    the relay is not draining anyway."""
    client = net.RelayClient(lambda _clip: None, connect=lambda _url: None)
    client._url = "wss://relay.example.com/chat/x"

    for index in range(net.SEND_QUEUE + 2):
        client.send(f"clip {index}".encode())

    queued = [client._outbox.get_nowait() for _ in range(client._outbox.qsize())]
    assert len(queued) == net.SEND_QUEUE
    assert queued[-1] == f"clip {net.SEND_QUEUE + 1}".encode()
    assert b"clip 0" not in queued


def test_sending_never_raises_even_with_a_broken_relay():
    """It is called from the worker with somebody's trigger cycle waiting."""
    client = net.RelayClient(lambda _clip: None, connect=lambda _url: None)
    client._url = "wss://relay.example.com/chat/x"
    for _ in range(50):
        client.send(b"x")


# -- changing rooms --------------------------------------------------------


def test_leaving_a_room_discards_what_was_queued_for_it():
    """Staying in the previous room would keep sending somebody's voice to a
    session they have left, which is the worst failure this component has."""
    client = net.RelayClient(lambda _clip: None, connect=lambda _url: None)
    client._url = "wss://relay.example.com/chat/one"
    client.send(b"for the old room")

    client.set_room(None)
    assert client._outbox.qsize() == 0


def test_setting_the_same_room_again_changes_nothing():
    client = net.RelayClient(lambda _clip: None, connect=lambda _url: None)
    client.set_room("wss://relay.example.com/chat/one")
    client.send(b"queued")

    client.set_room("wss://relay.example.com/chat/one")
    assert client._outbox.qsize() == 1


# -- failure stays inside --------------------------------------------------


def test_a_relay_that_cannot_be_reached_does_not_stop_the_client():
    """No exception escapes, and the thread stays alive to retry."""
    client = net.RelayClient(lambda _clip: None, connect=lambda _url: None)
    client.start()
    client.set_room(f"wss://relay.example.com/chat/{KEY}")
    threading.Event().wait(0.3)

    assert client._thread.is_alive()
    assert client.connected is False
    client.stop()


def test_a_connect_that_raises_is_survived():
    def explode(_url):
        raise OSError("no route to host")

    client = net.RelayClient(lambda _clip: None, connect=explode)
    client.start()
    client.set_room(f"wss://relay.example.com/chat/{KEY}")
    threading.Event().wait(0.3)

    assert client._thread.is_alive()
    client.stop()


def test_stopping_closes_the_socket(received):
    socket = FakeSocket([clip()])
    client = run_client(socket, received)
    client.stop()

    assert socket.closed is True


def test_stopping_a_client_that_never_started_is_not_an_error():
    net.RelayClient(lambda _clip: None).stop()


# -- finding the room without being told -----------------------------------


def test_the_room_is_polled_rather_than_pushed(received):
    """Somebody spectating never presses the trigger. A room only updated on
    the worker's cycle would never be joined, and they would sit in silence
    with everything apparently working."""
    socket = FakeSocket([clip()])
    url = f"wss://relay.example.com/chat/{KEY}"
    client = net.RelayClient(
        received.put, connect=lambda _url: socket, room=lambda: url)
    client.start()
    try:
        assert received.get(timeout=2.0) is not None
    finally:
        client.stop()


def test_a_room_provider_that_raises_does_not_end_the_thread():
    """It reaches into a plugin, and a sim that just closed must cost the room
    and not the connection."""
    def explode():
        raise RuntimeError("sim went away")

    client = net.RelayClient(
        lambda _clip: None, connect=lambda _url: None, room=explode)
    client.start()
    threading.Event().wait(0.3)

    assert client._thread.is_alive()
    client.stop()


# -- the service: who you actually hear ------------------------------------


class Store:
    def __init__(self, cfg):
        self.config = cfg


class FakePlugins:
    """A registry reporting one session, with settings we control."""

    def __init__(self, session, settings=None):
        self._session = session
        self._settings = settings or {}

    def any_session(self):
        return "fake", self._session

    def settings_for(self, _plugin_id, _stored=None):
        return dict(self._settings)


def a_session(*, listener_at=(0.0, 0.0, 0.0), others=()):
    """A session with a car being watched and some cars around it."""
    from pitradio.plugins.base import Car, SessionInfo

    cars = [Car(slot=1, driver="Watched", position=listener_at, control=0,
                is_player=True)]
    for index, (name, position) in enumerate(others, start=2):
        cars.append(Car(slot=index, driver=name, position=position, control=2))
    return SessionInfo(key=KEY, cars=tuple(cars))


def service(session, settings=None, *, cfg=None, played=None):
    from pitradio import config as config_mod

    configuration = cfg or config_mod.Config()
    configuration.voice.relay = "wss://relay.example.com"
    return net.VoiceService(
        Store(configuration), FakePlugins(session, settings),
        connect=lambda _url: None,
        play=(lambda clip, _cfg: played.append(clip.audio)) if played else None,
    )


def test_the_room_comes_from_whichever_sim_is_in_a_session():
    assert service(a_session())._room() == f"wss://relay.example.com/chat/{KEY}"


def test_no_room_when_neither_sending_nor_listening():
    """Holding a socket open to a room you cannot hear is worse than not."""
    from pitradio import config as config_mod

    cfg = config_mod.Config()
    cfg.voice.enabled = False
    cfg.voice.playback = False
    assert service(a_session(), cfg=cfg)._room() is None


def test_listening_alone_is_enough_to_join():
    """Somebody who never presses the trigger still belongs in the room."""
    from pitradio import config as config_mod

    cfg = config_mod.Config()
    cfg.voice.enabled = False
    cfg.voice.playback = True
    assert service(a_session(), cfg=cfg)._room() is not None


def test_a_clip_from_a_car_in_range_is_queued():
    session = a_session(others=[("Nick Tandy", (50.0, 0.0, 0.0))])
    voice_service = service(
        session, {"proximity_only": True, "proximity_metres": 200})

    voice_service._on_clip(voice.decode_clip(clip("Nick Tandy", b"near")))
    assert voice_service.playback._queue.qsize() == 1


def test_a_clip_from_a_car_out_of_range_is_not():
    session = a_session(others=[("Nick Tandy", (5000.0, 0.0, 0.0))])
    voice_service = service(
        session, {"proximity_only": True, "proximity_metres": 200})

    voice_service._on_clip(voice.decode_clip(clip("Nick Tandy", b"far")))
    assert voice_service.playback._queue.qsize() == 0


def test_with_proximity_off_the_whole_session_is_heard():
    session = a_session(others=[("Nick Tandy", (5000.0, 0.0, 0.0))])
    voice_service = service(
        session, {"proximity_only": False, "proximity_metres": 200})

    voice_service._on_clip(voice.decode_clip(clip("Nick Tandy", b"far")))
    assert voice_service.playback._queue.qsize() == 1


def test_muting_playback_drops_arriving_clips():
    """Going quiet without going deaf, and the reverse."""
    from pitradio import config as config_mod

    cfg = config_mod.Config()
    cfg.voice.playback = False
    voice_service = service(a_session(), cfg=cfg)

    voice_service._on_clip(voice.decode_clip(clip()))
    assert voice_service.playback._queue.qsize() == 0


def test_range_is_measured_from_the_watched_car_not_the_origin():
    """The whole spectator case: the listener is wherever the camera is."""
    session = a_session(
        listener_at=(1000.0, 0.0, 0.0),
        others=[("Near", (1050.0, 0.0, 0.0)), ("Far", (0.0, 0.0, 0.0))])
    voice_service = service(
        session, {"proximity_only": True, "proximity_metres": 200})

    voice_service._on_clip(voice.decode_clip(clip("Near", b"near")))
    voice_service._on_clip(voice.decode_clip(clip("Far", b"far")))

    queued = [voice_service.playback._queue.get_nowait()[1].audio
              for _ in range(voice_service.playback._queue.qsize())]
    assert queued == [b"near"]


# -- playback --------------------------------------------------------------


class Config:
    """Just enough config object for the playback thread."""

    def __init__(self, max_age_seconds=20.0):
        self.voice = type("V", (), {
            "max_age_seconds": max_age_seconds, "volume": 1.0,
            "output_device": None})()


def test_clips_are_played_one_at_a_time_in_order():
    """Two at once are unintelligible; queueing them is what makes it a radio."""
    played = []
    done = threading.Event()

    def play(clip, _cfg):
        played.append(clip.audio)
        if len(played) == 3:
            done.set()

    config = Config()
    playback = net.Playback(lambda: config, play=play)
    playback.start()
    try:
        for index in range(3):
            playback.offer(voice.decode_clip(clip(audio=f"clip {index}".encode())))
        done.wait(timeout=2.0)
    finally:
        playback.stop()

    assert played == [b"clip 0", b"clip 1", b"clip 2"]


def test_a_clip_that_waited_too_long_is_dropped(monkeypatch):
    """Racing information goes stale: a warning about a car alongside is
    misleading once the corner is over."""
    played = []
    clock = [0.0]
    monkeypatch.setattr(net, "_now", lambda: clock[0])

    config = Config(max_age_seconds=5.0)
    playback = net.Playback(lambda: config, play=lambda c, _cfg: played.append(c))

    playback._queue.put_nowait((0.0, voice.decode_clip(clip(audio=b"old"))))
    clock[0] = 60.0
    playback.start()
    try:
        threading.Event().wait(0.4)
    finally:
        playback.stop()

    assert played == []


def test_staleness_is_measured_locally_not_from_the_senders_clock():
    """Comparing the sender's timestamp to ours measures the difference between
    two computers' clocks, not any delay. A machine a minute fast would silence
    everybody with nothing to show why."""
    played = []
    done = threading.Event()

    def play(clip, _cfg):
        played.append(clip.audio)
        done.set()

    # Stamped an hour in the past by the sender, but only just arrived here.
    stale_looking = voice.encode_clip(
        voice.Speaker("Nick Tandy"), b"fresh", sent_at=1.0)

    config = Config(max_age_seconds=5.0)
    playback = net.Playback(lambda: config, play=play)
    playback.start()
    try:
        playback.offer(voice.decode_clip(stale_looking))
        done.wait(timeout=2.0)
    finally:
        playback.stop()

    assert played == [b"fresh"]


def test_a_full_playback_queue_drops_arrivals_rather_than_blocking():
    """It is offered from the receive loop, which has to keep reading."""
    config = Config()
    playback = net.Playback(lambda: config, play=lambda _c, _cfg: None)

    for index in range(net.PLAY_QUEUE + 5):
        playback.offer(voice.decode_clip(clip(audio=str(index).encode())))

    assert playback._queue.qsize() == net.PLAY_QUEUE


def test_a_playback_failure_does_not_end_the_thread():
    """A missing or busy output device must not cost every later clip."""
    attempts = []
    done = threading.Event()

    def play(clip, _cfg):
        attempts.append(clip.audio)
        if len(attempts) == 2:
            done.set()
        raise RuntimeError("no such device")

    config = Config()
    playback = net.Playback(lambda: config, play=play)
    playback.start()
    try:
        playback.offer(voice.decode_clip(clip(audio=b"one")))
        playback.offer(voice.decode_clip(clip(audio=b"two")))
        done.wait(timeout=2.0)
    finally:
        playback.stop()

    assert attempts == [b"one", b"two"]


def test_stopping_playback_that_never_started_is_not_an_error():
    net.Playback(lambda: Config()).stop()


def test_starting_twice_makes_one_thread():
    client = net.RelayClient(lambda _clip: None, connect=lambda _url: None)
    client.start()
    first = client._thread
    client.start()
    try:
        assert client._thread is first
    finally:
        client.stop()

"""Talking to the relay.

One thread owns the connection. It is neither the worker nor the hook nor the
GUI, because all three have deadlines: the hook must return before Windows
unregisters it, the worker is holding somebody's trigger, and the GUI is the
only thread allowed to touch a widget. A socket that blocks for thirty seconds
on a dead relay must not be on any of them.

Nothing here imports `winapi`, so it stays testable off Windows — and nothing
here decides who can hear what. That is [voice.py](voice.py), which is pure.

The connection is a **detail that is allowed to fail.** Voice going quiet must
never cost somebody their trigger, their transcription or their message in the
chat box; every failure path here ends in a log line and a retry.
"""

from __future__ import annotations

import logging
import queue
import threading
from urllib.parse import urlsplit, urlunsplit

from pitradio import voice

log = logging.getLogger(__name__)

#: Clips waiting to be sent. Small on purpose: a clip that has been queued for
#: more than a few seconds is stale, and a backlog means the relay is gone —
#: at which point holding onto them is just memory. The oldest is dropped.
SEND_QUEUE = 4

#: Received clips waiting to be played. Larger, because playing takes real time
#: and a busy session legitimately queues up.
PLAY_QUEUE = 16

#: Reconnect backoff, seconds. Ends where it stays: a relay that has been down
#: for a minute is being restarted or replaced, and hammering it helps nobody.
BACKOFF = (1.0, 2.0, 5.0, 10.0, 30.0)

#: How long a receive may block before the loop checks whether it should stop.
#: Short enough that quitting feels immediate, long enough not to spin.
RECV_TIMEOUT = 0.5


def room_url(relay: str, session_key: str) -> str | None:
    """The room to connect to, or None if there is not one.

    None is an ordinary answer, not a failure: no relay configured in this
    build, or no multiplayer session to be in. Both mean voice is simply off.
    """
    relay = (relay or "").strip().rstrip("/")
    if not relay or not voice.is_session_key(session_key):
        return None

    parts = urlsplit(relay)
    if parts.scheme not in ("ws", "wss") or not parts.netloc:
        return None
    # Any path on the configured relay is kept, so a relay living under a
    # prefix works; the room is appended to it.
    path = f"{parts.path.rstrip('/')}/chat/{session_key}"
    return urlunsplit((parts.scheme, parts.netloc, path, "", ""))


def backoff_seconds(attempt: int) -> float:
    """How long to wait before retry number `attempt`, counting from zero."""
    if attempt < 0:
        return BACKOFF[0]
    return BACKOFF[min(attempt, len(BACKOFF) - 1)]


class RelayClient:
    """A connection to one room, reconnecting for as long as it is wanted.

    `on_clip` is called from this thread with each decoded clip. It must not
    block for long and must not touch a widget — the GUI marshals through
    `AppState.events` like everything else.
    """

    def __init__(self, on_clip, *, connect=None, room=None) -> None:
        self._on_clip = on_clip
        # Polled rather than pushed, because nothing else is reliably running:
        # somebody spectating never presses the trigger, so a room that were
        # only updated on the worker's cycle would never be joined at all, and
        # they would sit in silence with everything apparently working.
        self._room = room
        # Injected so the whole loop can be tested without a relay, a socket or
        # a network. The default is imported lazily: websocket-client is only
        # needed by a build that actually has voice switched on.
        self._connect = connect or _connect
        self._outbox: queue.Queue[bytes] = queue.Queue(maxsize=SEND_QUEUE)
        self._lock = threading.Lock()
        self._url: str | None = None
        self._wake = threading.Event()
        self._stopping = threading.Event()
        self._thread: threading.Thread | None = None
        self._connected = False

    # -- lifecycle -------------------------------------------------------

    def start(self) -> None:
        if self._thread is not None:
            return
        self._stopping.clear()
        self._thread = threading.Thread(
            target=self._run, name="voice-relay", daemon=True)
        self._thread.start()

    def stop(self, timeout: float = 2.0) -> None:
        self._stopping.set()
        self._wake.set()
        thread, self._thread = self._thread, None
        if thread is not None:
            thread.join(timeout=timeout)

    @property
    def connected(self) -> bool:
        return self._connected

    # -- what room -------------------------------------------------------

    def set_room(self, url: str | None) -> None:
        """Point at a room, or at nothing.

        Changing it drops the current connection: staying in the previous room
        would keep sending somebody's voice to a session they have left, which
        is the worst failure this component has.
        """
        with self._lock:
            if url == self._url:
                return
            self._url = url
        log.info("voice room: %s", "none" if url is None else "changed")
        self._drain()
        self._wake.set()

    # -- sending ---------------------------------------------------------

    def send(self, frame: bytes) -> None:
        """Queue a clip. Never blocks, never raises.

        Called from the worker with somebody's trigger cycle waiting on it.
        """
        if not frame or self._url is None:
            return
        try:
            self._outbox.put_nowait(frame)
        except queue.Full:
            # Drop the oldest: the newest thing said is the one worth hearing,
            # and a full queue means the relay is not draining anyway.
            try:
                self._outbox.get_nowait()
                self._outbox.put_nowait(frame)
            except (queue.Empty, queue.Full):
                pass
            log.info("send queue was full; dropped the oldest clip")

    def _drain(self) -> None:
        while True:
            try:
                self._outbox.get_nowait()
            except queue.Empty:
                return

    # -- the loop --------------------------------------------------------

    def _poll_room(self) -> None:
        """Ask what room we should be in, if anybody can say.

        Swallows everything: this reaches into a plugin, and a sim that has
        just closed must cost the room, never the thread.
        """
        if self._room is None:
            return
        try:
            self.set_room(self._room())
        except Exception:
            log.debug("could not work out the voice room", exc_info=True)

    def _run(self) -> None:
        attempt = 0
        while not self._stopping.is_set():
            self._poll_room()
            url = self._url
            if url is None:
                # Nothing to connect to. Wait to be told otherwise rather than
                # spinning; set_room wakes this.
                self._wake.wait(timeout=RECV_TIMEOUT)
                self._wake.clear()
                continue

            try:
                if self._session(url):
                    # Connected and ran; whatever ended it, the next attempt
                    # starts from the top of the backoff.
                    attempt = 0
                    continue
            except Exception:
                log.debug("voice relay session failed", exc_info=True)

            delay = backoff_seconds(attempt)
            attempt += 1
            log.info("voice relay unavailable; retrying in %.0fs", delay)
            self._wake.wait(timeout=delay)
            self._wake.clear()

    def _session(self, url: str) -> bool:
        """One connection, until it ends. True if it was ever established."""
        socket = self._connect(url)
        if socket is None:
            return False

        self._connected = True
        log.info("voice connected")
        try:
            while not self._stopping.is_set() and self._url == url:
                self._flush(socket)
                # Cheap, and it is how leaving a session is noticed while
                # connected — the sim closing, or the event moving on.
                self._poll_room()
                frame = socket.recv(RECV_TIMEOUT)
                if frame is None:
                    continue          # nothing waiting, go round again
                if frame is _CLOSED:
                    return True
                self._deliver(frame)
        finally:
            self._connected = False
            _close(socket)
            log.info("voice disconnected")
        return True

    def _flush(self, socket) -> None:
        while True:
            try:
                frame = self._outbox.get_nowait()
            except queue.Empty:
                return
            socket.send(frame)

    def _deliver(self, frame: bytes) -> None:
        clip = voice.decode_clip(frame)
        if clip is None:
            # Bytes from a stranger's machine. Dropping one costs that clip.
            log.debug("discarded a frame that was not a clip")
            return
        try:
            self._on_clip(clip)
        except Exception:
            log.exception("handling a received clip failed")


class VoiceService:
    """The relay, playback, and the decision about who you can hear.

    One object to start and stop, so the app has one thing to own and
    `--gui-only` can simply not build it. Everything it decides is delegated —
    the room to `room_url`, who is audible to `voice.audible`, where the
    listener is to the plugin — so this is wiring and nothing else.
    """

    def __init__(self, store, plugins, *, connect=None, play=None) -> None:
        self.store = store
        self.plugins = plugins
        self.playback = Playback(lambda: self.store.config, play=play)
        self.client = RelayClient(
            self._on_clip, connect=connect, room=self._room)

    def start(self) -> None:
        self.playback.start()
        self.client.start()

    def stop(self) -> None:
        self.client.stop()
        self.playback.stop()

    def send(self, frame: bytes) -> None:
        self.client.send(frame)

    # -- what the client asks it ------------------------------------------

    def _room(self) -> str | None:
        cfg = self.store.config
        # Neither sending nor listening: there is no reason to hold a socket
        # open to a room, and being in one you cannot hear is worse than not.
        if not (cfg.voice.enabled or cfg.voice.playback):
            return None
        _plugin_id, session = self.plugins.any_session()
        return room_url(cfg.voice.relay, session.key)

    def _on_clip(self, clip) -> None:
        cfg = self.store.config
        if not cfg.voice.playback:
            return

        plugin_id, session = self.plugins.any_session()
        settings = self.plugins.settings_for(
            plugin_id, self.store.config.profile_for_plugin(plugin_id).plugin_settings)

        listener = session.listener()
        if not voice.audible(
            clip.speaker,
            listener.position if listener else None,
            proximity_only=bool(settings.get("proximity_only")),
            metres=int(settings.get("proximity_metres") or 0),
            positions=session.positions(),
        ):
            log.debug("clip from %r was out of range", clip.speaker.driver)
            return

        self.playback.offer(clip)


class Playback:
    """Plays received clips, one at a time, on its own thread.

    One at a time because two clips at once are unintelligible; queued rather
    than mixed, which is what makes a radio a radio.

    Its own thread because playing takes real time — doing it on the receive
    loop would stall the socket for the length of every clip, and a busy
    session would fall further behind with each one.
    """

    def __init__(self, config, *, play=None) -> None:
        # A callable, so the queue behaviour is testable without a sound card.
        self._play = play or _play_clip
        self._config = config
        self._queue: queue.Queue = queue.Queue(maxsize=PLAY_QUEUE)
        self._stopping = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        if self._thread is not None:
            return
        self._stopping.clear()
        self._thread = threading.Thread(
            target=self._run, name="voice-playback", daemon=True)
        self._thread.start()

    def stop(self, timeout: float = 2.0) -> None:
        self._stopping.set()
        thread, self._thread = self._thread, None
        if thread is not None:
            thread.join(timeout=timeout)

    def offer(self, clip) -> None:
        """Queue a clip, dropping it if there is no room. Never blocks.

        Called from the receive loop, which must keep reading.
        """
        try:
            self._queue.put_nowait((_now(), clip))
        except queue.Full:
            # The backlog is the problem, not this clip. Dropping the newest
            # keeps what is already playing intelligible instead of extending
            # a queue nobody will listen to the end of.
            log.info("playback queue is full; dropped an arriving clip")

    def _run(self) -> None:
        while not self._stopping.is_set():
            try:
                queued_at, clip = self._queue.get(timeout=0.2)
            except queue.Empty:
                continue

            # **Measured on this machine's clock, from arrival to playback.**
            # The clip carries a timestamp from the sender, and comparing that
            # to ours would measure the difference between two computers'
            # clocks rather than any delay — a machine a minute fast would
            # silence everybody, with nothing to show why.
            waited = _now() - queued_at
            limit = float(getattr(self._config().voice, "max_age_seconds", 0) or 0)
            if limit and waited > limit:
                log.info("clip waited %.1fs and was dropped as stale", waited)
                continue

            try:
                self._play(clip, self._config())
            except Exception:
                log.exception("playing a clip failed")


def _play_clip(clip, cfg) -> None:
    from pitradio import speech

    # The whole config, not just the voice section: how loud is voice chat's
    # business and where it comes out is the app's, set once on the Audio tab.
    speech.play_clip(speech.from_pcm16(clip.audio), clip.rate,
                     cfg.voice.volume, cfg.audio.output_device)


def _now() -> float:
    import time

    return time.monotonic()


#: Returned by a socket's recv when the far end has gone.
_CLOSED = object()


def _connect(url: str):
    """The real socket. Imported here so a build without voice never loads it."""
    try:
        import websocket
    except ImportError:
        log.info("voice needs the websocket-client package, which is not installed")
        return None

    try:
        return _Socket(websocket.create_connection(url, timeout=RECV_TIMEOUT))
    except Exception as exc:
        log.debug("could not reach the relay: %s", exc)
        return None


class _Socket:
    """Thin wrapper turning websocket-client's exceptions into return values.

    The loop above reads better for it, and more importantly a timeout stops
    being indistinguishable from a disconnection — the first is normal every
    half second, the second must end the session.
    """

    def __init__(self, connection) -> None:
        self._connection = connection

    def send(self, frame: bytes) -> None:
        import websocket

        self._connection.send(frame, opcode=websocket.ABNF.OPCODE_BINARY)

    def recv(self, timeout: float):
        import websocket

        self._connection.settimeout(timeout)
        try:
            frame = self._connection.recv()
        except websocket.WebSocketTimeoutException:
            return None
        except (websocket.WebSocketConnectionClosedException, OSError):
            return _CLOSED
        if frame is None or frame == "":
            return _CLOSED
        # Text frames are the control channel and there is nothing on it yet.
        return frame if isinstance(frame, (bytes, bytearray)) else None

    def close(self) -> None:
        self._connection.close()


def _close(socket) -> None:
    try:
        socket.close()
    except Exception:
        log.debug("closing the relay socket failed", exc_info=True)

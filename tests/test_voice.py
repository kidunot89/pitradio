"""Room identity and who can hear whom.

Both of these fail silently when they are wrong. A session key that disagrees
between two clients puts them in separate rooms, each of which looks exactly
like a session where nobody else is running PitRadio; a proximity test that is
subtly wrong silences somebody you are racing. Neither raises anything.
"""

import hashlib

import pytest

from pitradio import voice

# The listener, at the origin, for readability below.
HERE = (0.0, 0.0, 0.0)


# -- session key ---------------------------------------------------------


def test_the_same_server_gives_the_same_room():
    """The whole point: two clients agree without ever talking to each other."""
    assert voice.session_key(3156777263, 30852) == voice.session_key(3156777263, 30852)


def test_a_different_server_is_a_different_room():
    assert voice.session_key(3156777263, 30852) != voice.session_key(3156777264, 30852)


def test_a_different_port_is_a_different_room():
    """One machine can host several servers, and they are not the same session."""
    assert voice.session_key(3156777263, 30852) != voice.session_key(3156777263, 30853)


def test_the_key_does_not_reveal_the_server():
    """The relay is handed a hash, so it never learns who is playing where."""
    key = voice.session_key(3156777263, 30852)
    assert "3156777263" not in key
    assert "30852" not in key


@pytest.mark.parametrize(("address", "port"), [(0, 30852), (3156777263, 0), (0, 0)])
def test_no_server_means_no_room(address, port):
    """Offline and single player. A shared fallback room would put every person
    in the world who is not in a session into one open microphone."""
    assert voice.session_key(address, port) == ""


def test_a_key_is_shaped_the_way_the_relay_expects():
    key = voice.session_key(3156777263, 30852)
    assert voice.is_session_key(key)
    assert len(key) == voice.KEY_LENGTH


@pytest.mark.parametrize("value", [
    "", "nope", "../../etc/passwd", "0" * 31, "0" * 33, "g" * 32,
    "ABCDEF0123456789ABCDEF0123456789",  # uppercase is not what we emit
    # It guards a URL path and is asked about whatever the caller has, which
    # includes "no session at all". A predicate that raises is not one.
    None, 12345, b"0" * 32, ["0" * 32],
])
def test_anything_else_is_not_a_key(value):
    """It goes in a URL path; a path segment off the network is trusted with
    nothing, least of all the filesystem."""
    assert not voice.is_session_key(value)


# -- proximity -----------------------------------------------------------


def near() -> voice.Speaker:
    return voice.Speaker("Nick Tandy", (50.0, 0.0, 0.0))


def far() -> voice.Speaker:
    return voice.Speaker("Nick Tandy", (4000.0, 0.0, 0.0))


def test_everyone_is_audible_when_proximity_is_off():
    assert voice.audible(far(), HERE, proximity_only=False, metres=200)


def test_a_car_beyond_the_range_is_not():
    assert not voice.audible(far(), HERE, proximity_only=True, metres=200)


def test_a_car_within_the_range_is():
    assert voice.audible(near(), HERE, proximity_only=True, metres=200)


def test_the_boundary_counts_as_audible():
    speaker = voice.Speaker("Nick Tandy", (200.0, 0.0, 0.0))
    assert voice.audible(speaker, HERE, proximity_only=True, metres=200)


def test_distance_is_measured_in_three_dimensions():
    """Daytona's banking, and any track that crosses over itself: two cars can
    be metres apart on the map and nowhere near each other."""
    assert voice.distance((0.0, 0.0, 0.0), (0.0, 30.0, 40.0)) == pytest.approx(50.0)


def test_not_knowing_where_the_listener_is_means_hearing_them():
    """Silence that cannot be explained is indistinguishable from the feature
    being broken, and a driver who cannot tell which will switch it off."""
    assert voice.audible(far(), None, proximity_only=True, metres=200)


def test_a_speaker_who_reported_no_position_is_audible():
    speaker = voice.Speaker("Nick Tandy")
    assert voice.audible(speaker, HERE, proximity_only=True, metres=200)


def test_the_origin_is_a_real_place_not_a_missing_position():
    """Tracks have a coordinate origin and cars drive over it. Treating it as
    "unknown" would make whoever is parked there permanently audible, however
    far away they later got — and nothing would ever say why."""
    at_origin = voice.Speaker("Nick Tandy", (0.0, 0.0, 0.0))

    assert not voice.audible(
        at_origin, (5000.0, 0.0, 0.0), proximity_only=True, metres=200)
    assert voice.audible(
        at_origin, (50.0, 0.0, 0.0), proximity_only=True, metres=200)


def test_a_range_of_zero_does_not_silence_the_session():
    """A misconfigured radius must not be a mute button with no error."""
    assert voice.audible(far(), HERE, proximity_only=True, metres=0)


# -- where the speaker actually is -----------------------------------------
#
# A clip arrives after its speaker stopped talking, so the position it carries
# is a second or two old — at racing speed, a hundred metres, which against a
# 200m radius decides the answer.


def test_our_own_view_of_the_speaker_wins_over_the_clip():
    """The question is who is near the target car *now*, when it plays."""
    stale = voice.Speaker("Nick Tandy", (4000.0, 0.0, 0.0))
    assert voice.locate(stale, {"Nick Tandy": (50.0, 0.0, 0.0)}) == (50.0, 0.0, 0.0)


def test_a_speaker_we_cannot_see_falls_back_to_what_they_reported():
    """Somebody who just joined, or whose entry has gone. A stale position
    beats none."""
    speaker = voice.Speaker("Nick Tandy", (50.0, 0.0, 0.0))
    assert voice.locate(speaker, {"Someone Else": HERE}) == (50.0, 0.0, 0.0)
    assert voice.locate(speaker, {}) == (50.0, 0.0, 0.0)
    assert voice.locate(speaker, None) == (50.0, 0.0, 0.0)


def test_a_car_that_has_driven_away_since_speaking_is_not_heard():
    """They were alongside when they pressed the button and are a straight
    away later by the time it plays."""
    speaker = voice.Speaker("Nick Tandy", (10.0, 0.0, 0.0))
    assert not voice.audible(
        speaker, HERE, proximity_only=True, metres=200,
        positions={"Nick Tandy": (4000.0, 0.0, 0.0)})


def test_a_car_that_has_arrived_since_speaking_is_heard():
    speaker = voice.Speaker("Nick Tandy", (4000.0, 0.0, 0.0))
    assert voice.audible(
        speaker, HERE, proximity_only=True, metres=200,
        positions={"Nick Tandy": (10.0, 0.0, 0.0)})


def test_a_speaker_nobody_can_place_is_still_heard():
    """Not in our block, and their own sim reported nothing either."""
    assert voice.audible(
        voice.Speaker("Nick Tandy"), HERE, proximity_only=True, metres=200,
        positions={"Someone Else": HERE})


# -- the wire format -----------------------------------------------------
#
# Decoding runs on the audio path against bytes from a stranger's machine.
# Nothing in here may raise: a bad frame costs that clip and nothing else.


def test_a_clip_survives_the_round_trip():
    speaker = voice.Speaker("Nyck de Vries", (10.0, 2.0, -30.0))
    frame = voice.encode_clip(speaker, b"audio-bytes", sent_at=1234.5)

    clip = voice.decode_clip(frame)
    assert clip.speaker.driver == "Nyck de Vries"
    assert clip.speaker.position == (10.0, 2.0, -30.0)
    assert clip.audio == b"audio-bytes"
    assert clip.sent_at == 1234.5
    assert clip.rate == 16000


def test_an_empty_recording_still_decodes():
    """Nothing downstream should have to special-case it."""
    clip = voice.decode_clip(
        voice.encode_clip(voice.Speaker("Nick Tandy"), b"", sent_at=1.0))
    assert clip is not None
    assert clip.audio == b""


@pytest.mark.parametrize("frame", [
    b"",
    b"PR",
    b"XXXX\x00\x02{}",                      # right shape, wrong magic
    b"PRV1\x00\xff{}",                      # header longer than the frame
    b"PRV1\x00\x02[]",                      # valid JSON, not an object
    b"PRV1\x00\x04\xff\xff\xff\xff",        # not UTF-8
    b"PRV1\x00\x02{,",                      # not JSON
    b"PRV1\x00\x02{}",                      # no speaker
    b'PRV1\x00\x0c{"from":"  "}',           # blank speaker
    "PRV1 but a string",
    None,
])
def test_a_frame_that_is_not_ours_is_rejected_quietly(frame):
    """Handing this to the sound card would be a burst of static in somebody's
    headset mid-corner. It must be dropped, and it must not raise."""
    assert voice.decode_clip(frame) is None


def test_an_oversized_frame_is_refused_before_it_is_parsed():
    """A push-to-talk clip is seconds of 16kHz mono. Past the cap it is a
    mistake or an attack, and either way not something to allocate."""
    assert voice.decode_clip(b"PRV1" + b"\x00" * voice.MAX_CLIP_BYTES) is None


def test_a_missing_position_reads_as_unknown():
    """Which `audible` treats as audible, rather than silently dropping."""
    frame = voice.encode_clip(voice.Speaker("Nick Tandy"), b"x", sent_at=1.0)
    clip = voice.decode_clip(frame)

    assert clip.speaker.position is None
    assert voice.audible(clip.speaker, HERE, proximity_only=True, metres=200)


@pytest.mark.parametrize("payload", ['"nope"', "[1,2]", "[1,2,3,4]", '[1,"a",3]'])
def test_a_malformed_position_reads_as_unknown(payload):
    header = f'{{"from":"Nick Tandy","pos":{payload}}}'.encode()
    frame = b"PRV1" + len(header).to_bytes(2, "big") + header + b"x"

    assert voice.decode_clip(frame).speaker.position is None


def test_an_infinite_position_is_not_trusted():
    """NaN and inf survive a JSON round trip in Python and would make every
    distance comparison false — silencing the speaker with no error."""
    header = b'{"from":"Nick Tandy","pos":[Infinity,0,0]}'
    frame = b"PRV1" + len(header).to_bytes(2, "big") + header + b"x"

    assert voice.decode_clip(frame).speaker.position is None


def test_a_clip_from_the_future_is_not_infinitely_fresh():
    """Clocks disagree. Without this it would outlive every staleness cutoff."""
    clip = voice.Clip(voice.Speaker("Nick Tandy"), b"x", sent_at=500.0)
    assert clip.age(100.0) == 0.0
    assert clip.age(520.0) == 20.0


# -- pinning a host's certificate ------------------------------------------
#
# A racer-owned host has no DNS name and no public certificate. It serves one
# the coordinator generated and named to us over a connection we already
# trust, so accepting that one and nothing else *is* the trust decision.


def _der(seed: bytes = b"cert") -> bytes:
    return seed * 40


def test_the_named_certificate_is_accepted():
    der = _der()
    assert voice.certificate_matches(der, hashlib.sha256(der).hexdigest())


def test_punctuation_is_not_identity():
    """Tools print fingerprints as AB:CD:… and as abcd… interchangeably. A
    difference of formatting must never read as a different machine."""
    digest = hashlib.sha256(_der()).hexdigest()
    spaced = ":".join(digest[i:i + 2] for i in range(0, len(digest), 2)).upper()
    assert voice.certificate_matches(_der(), spaced)


def test_any_other_certificate_is_refused():
    assert not voice.certificate_matches(
        _der(b"real"), hashlib.sha256(_der(b"impostor")).hexdigest())


@pytest.mark.parametrize("expected", ["", None, "nope", "ab" * 31, "ab" * 33])
def test_a_fingerprint_that_is_not_one_never_matches(expected):
    """It decides whether to trust a stranger's machine, so anything malformed
    is a refusal — never an accident that passes."""
    assert not voice.certificate_matches(_der(), expected)


def test_no_certificate_never_matches():
    """A handshake that produced nothing to check is not a match."""
    assert not voice.certificate_matches(b"", hashlib.sha256(b"").hexdigest())
    assert not voice.certificate_matches(None, "ab" * 32)

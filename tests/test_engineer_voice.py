"""Voice packs, reading their audio, and the queue that plays it.

None of this needs Windows or a sound card: a pack is a directory listing, a
WAV is bytes, and the speaker takes an injected player. Which is the point —
the parts that *do* need Windows are the two lines that start PowerShell, and
everything around them is checkable here.
"""

from __future__ import annotations

import struct
import time

import numpy as np
import pytest

from pitradio.engineer import packs, personas, speaking, tts

# -- phrase ids -----------------------------------------------------------


def test_a_phrase_id_is_derived_from_the_words():
    """Nothing maintains a table of ids, so a pack built for an older version
    keeps working and nothing can drift."""
    assert packs.slug("two tenths") == "two_tenths"
    assert packs.slug("that's enough") == "thats_enough"
    assert packs.slug("Curva Parabólica") == "curva_parabolica"


def test_punctuation_does_not_split_a_phrase_from_its_clip():
    assert packs.slug("that's enough") == packs.slug("thats enough")


# -- reading a pack -------------------------------------------------------


def write_wav(path, samples, rate=24000, float32=True):
    """A real RIFF file, because that is what the parser has to survive."""
    path.parent.mkdir(parents=True, exist_ok=True)
    data = np.asarray(samples, dtype=np.float32)
    if float32:
        tag, bits, body = 3, 32, data.astype("<f4").tobytes()
    else:
        tag, bits, body = 1, 16, (data * 32767).astype("<i2").tobytes()

    fmt = struct.pack("<HHIIHH", tag, 1, rate, rate * bits // 8, bits // 8, bits)
    riff = (b"WAVE" + b"fmt " + struct.pack("<I", len(fmt)) + fmt
            + b"data" + struct.pack("<I", len(body)) + body)
    path.write_bytes(b"RIFF" + struct.pack("<I", len(riff)) + riff)
    return path


def test_a_crew_chief_style_pack_is_read(tmp_path):
    """`crew-chief-autovoicepack` writes voice/<category>/<phrase>/*.wav, and
    dropping one of those in is the whole reason the layout is this one."""
    pack_dir = tmp_path / "Ada"
    write_wav(pack_dir / "voice" / "corners" / "two_tenths" / "a.wav", [0.1, 0.2])
    write_wav(pack_dir / "voice" / "corners" / "two_tenths" / "b.wav", [0.3, 0.4])

    pack = packs.load(pack_dir)
    assert pack.has("two tenths")
    assert len(pack.clips["two_tenths"]) == 2


def test_a_flat_pack_is_read_too(tmp_path):
    """What somebody recording their own produces."""
    pack_dir = tmp_path / "Mine"
    write_wav(pack_dir / "go_ahead" / "1.wav", [0.1])
    assert packs.load(pack_dir).has("go ahead")


def test_a_phrase_the_pack_has_never_heard_of_misses(tmp_path):
    """Driver names are the case this exists for; they fall through to the
    synthesiser rather than producing silence."""
    pack_dir = tmp_path / "Ada"
    write_wav(pack_dir / "go_ahead" / "1.wav", [0.1])
    pack = packs.load(pack_dir)
    assert pack.has("Verstappen") is False
    assert pack.take("Verstappen") is None


def test_a_folder_with_no_clips_is_not_a_pack(tmp_path):
    (tmp_path / "notes").mkdir()
    (tmp_path / "notes" / "readme.txt").write_text("hello", encoding="utf-8")
    assert packs.load(tmp_path / "notes") is None
    assert packs.discover(tmp_path) == []


def test_packs_are_found_and_named_by_folder(tmp_path):
    write_wav(tmp_path / "Ada" / "clear" / "a.wav", [0.1])
    write_wav(tmp_path / "Vic" / "clear" / "a.wav", [0.1])

    assert [pack.name for pack in packs.discover(tmp_path)] == ["Ada", "Vic"]
    assert packs.find(tmp_path, "Vic").name == "Vic"
    assert packs.find(tmp_path, "nobody") is None


def test_a_missing_pack_folder_is_not_an_error(tmp_path):
    assert packs.discover(tmp_path / "nothing here") == []


def test_the_phrase_list_is_written_for_a_generator(tmp_path):
    target = packs.inventory(
        [("go_ahead", "go ahead"), ("clear", "clear"), ("clear", "clear")],
        tmp_path / "phrase_inventory.csv")
    rows = target.read_text(encoding="utf-8").splitlines()

    assert rows[0] == "audio_filename,text_for_tts,subtitle"
    # Deduplicated: the same fragment appears in several sentences and nobody
    # wants to record it twice.
    assert len(rows) == 3
    assert "clear/clear.wav,clear,clear" in rows


# -- reading audio --------------------------------------------------------


def test_a_float_wav_is_read(tmp_path):
    """The standard library cannot: `wave` raises on IEEE float, which is
    exactly what the generator this feature supports writes."""
    path = write_wav(tmp_path / "a.wav", [0.0, 0.5, -0.5, 1.0], rate=24000)
    audio, rate = speaking.read_wav(path)

    assert rate == 24000
    assert audio.tolist() == pytest.approx([0.0, 0.5, -0.5, 1.0])


def test_a_16_bit_wav_is_read(tmp_path):
    path = write_wav(tmp_path / "a.wav", [0.0, 0.5], rate=22050, float32=False)
    audio, rate = speaking.read_wav(path)

    assert rate == 22050
    assert audio.tolist() == pytest.approx([0.0, 0.5], abs=1e-4)


def test_an_unreadable_clip_costs_that_clip(tmp_path):
    """One corrupt file in a pack of thousands must not take the engineer out."""
    broken = tmp_path / "broken.wav"
    broken.write_bytes(b"not a wav at all")
    audio, rate = speaking.read_wav(broken)
    assert audio.size == 0
    assert rate == 0

    missing, _ = speaking.read_wav(tmp_path / "nope.wav")
    assert missing.size == 0


def test_resampling_keeps_the_length_in_seconds():
    audio = np.linspace(0.0, 1.0, 100, dtype=np.float32)
    assert speaking.resample(audio, 24000, 48000).size == 200
    assert speaking.resample(audio, 24000, 24000) is audio


def test_fragments_are_joined_at_one_rate():
    """A pack take at 24kHz beside a synthesised name at 22.05kHz plays one of
    them at the wrong pitch unless they are brought together first."""
    first = np.ones(2400, dtype=np.float32)
    second = np.ones(2205, dtype=np.float32)

    audio, rate = speaking.join([(first, 24000), (second, 22050)])
    assert rate == 24000
    # Both fragments are a tenth of a second, plus one gap between them.
    expected = 2400 + 2400 + int(24000 * speaking.GAP_SECONDS)
    assert audio.size == pytest.approx(expected, abs=2)


def test_joining_nothing_is_silence():
    audio, rate = speaking.join([])
    assert audio.size == 0 and rate == 0


# -- the queue ------------------------------------------------------------


class FakeHost:
    """Stands in for PowerShell: records what it was asked to say."""

    def __init__(self, wavs):
        self.wavs = wavs
        self.asked = []

    def synthesize(self, text, *, voice="", rate=0):
        self.asked.append(text)
        return self.wavs.get(text)

    def voices(self):
        return []

    def close(self):
        pass


@pytest.fixture
def played():
    return []


def speaker(tmp_path, played, wavs=None, pack=None):
    host = FakeHost(wavs or {})
    made = speaking.Speaker(
        lambda: _Config(), host=host,
        play=lambda audio, rate, cfg: played.append((audio.size, rate)))
    made.configure(speaking.VoiceSettings(pack=pack))
    return made, host


class _Config:
    volume = 0.8
    output_device = None


def test_a_pack_take_is_preferred_and_a_name_falls_through(tmp_path, played):
    """The mixed sentence this whole design exists to allow."""
    pack_dir = tmp_path / "Ada"
    write_wav(pack_dir / "turn_four" / "a.wav", np.ones(100), rate=24000)
    pack = packs.load(pack_dir)

    name_wav = write_wav(tmp_path / "name.wav", np.ones(50), rate=24000)
    made, host = speaker(tmp_path, played, {"Verstappen": name_wav}, pack=pack)

    made._speak(["turn four", "Verstappen"])

    # Only the name reached the synthesiser; the fixed phrase came off disk.
    assert host.asked == ["Verstappen"]
    assert played and played[0][1] == 24000


def test_an_urgent_call_goes_first(tmp_path, played):
    made, _host = speaker(tmp_path, played)
    made.say(["last lap", "one twenty three"])
    made.say(["car left"], urgent=True)

    first = made._queue.get_nowait()
    assert first.utterance == ["car left"]


def test_a_full_queue_drops_rather_than_blocking(tmp_path, played):
    """Called from the polling thread with a sim read in progress, and from the
    worker with somebody's trigger held down."""
    made, _host = speaker(tmp_path, played)
    for index in range(speaking.QUEUE_SIZE + 4):
        made.say([f"call {index}"])
    assert made._queue.qsize() == speaking.QUEUE_SIZE


def test_clearing_throws_away_what_is_waiting(tmp_path, played):
    """What the stop phrase has to do: a routine stood down must not go on
    talking about the last four corners."""
    made, _host = speaker(tmp_path, played)
    made.say(["one"])
    made.say(["two"])
    made.clear()
    assert made._queue.empty()


def test_an_empty_utterance_is_never_queued(tmp_path, played):
    made, _host = speaker(tmp_path, played)
    made.say([])
    made.say(["", "   "])
    assert made._queue.empty()


def test_a_stale_call_is_dropped_rather_than_spoken(tmp_path, played):
    """A comparison for turn four reaching the driver on the run to turn seven
    is not late, it is wrong: they hear a number and think about the wrong
    piece of track."""
    made, _host = speaker(tmp_path, played)
    made.say(["turn four"])
    stale = made._queue.get_nowait()
    stale.queued_at = time.monotonic() - speaking.MAX_AGE - 1
    made._queue.put_nowait(stale)

    made._stopping.set()          # one pass through the loop, then stop
    made._stopping.clear()
    item = made._queue.get_nowait()
    assert time.monotonic() - item.queued_at > speaking.MAX_AGE


# -- personas -------------------------------------------------------------


def test_four_engineers_ship():
    assert len(personas.BUILTIN) == 4
    assert len({persona.id for persona in personas.BUILTIN}) == 4
    assert len({persona.name for persona in personas.BUILTIN}) == 4


def test_an_unknown_persona_falls_back_rather_than_failing():
    """A config from a newer build, or a typo. Neither is worth a silent
    engineer."""
    assert personas.by_id("nobody").id == personas.DEFAULT


def test_a_persona_prefers_a_voice_in_the_right_language():
    """A German voice reading English is unintelligible in a way the wrong
    gender simply is not, so language wins."""
    voices = [tts.Voice("Hans", "de-DE", "Male"),
              tts.Voice("George", "en-GB", "Male")]
    assert personas.pick_voice(personas.by_id("chief"), voices, "en") == "George"


def test_two_personas_of_the_same_gender_get_different_voices():
    """Two engineers with the same voice are one engineer with two names."""
    voices = [tts.Voice("Zira", "en-US", "Female"),
              tts.Voice("Hazel", "en-GB", "Female")]
    chosen = {personas.pick_voice(personas.by_id(pid), voices, "en")
              for pid in ("ada", "vic")}
    assert len(chosen) == 2


def test_no_voices_installed_means_let_windows_choose():
    assert personas.pick_voice(personas.by_id("chief"), [], "en") == ""

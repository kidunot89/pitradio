"""Turn any recording into baseline clips `crew-chief-autovoicepack` will accept.

Its README is unambiguous about what it wants and blunt about what happens
otherwise — "the source of your issues is very likely from imperfections in the
input audio recordings". The requirements are:

* 32-bit float PCM WAV, mono, 22.05 kHz (the README says "22.5 kHz", which is
  not a rate anything uses; XTTS's reference rate is 22050 and that is what is
  written here)
* ten seconds or less per clip — longer is silently truncated, so a long clip
  is not an error, it is nine wasted seconds
* between three and twenty-five clips
* normalised, with all silence trimmed from both ends
* any internal pause over about half a second split into two clips

Every one of those is mechanical, and doing them by hand in an audio editor for
twenty clips is an afternoon nobody should spend. So this does them.

**Decoding goes through PyAV**, which is already a dependency — faster-whisper
imports it eagerly and the build has fought native dependencies enough times
that adding another for this would be a poor trade. It also means anything
FFmpeg reads works: a phone recording, an `.m4a`, a `yt-dlp` download.

Run it:

    python packaging/prepare_voice.py --name Bono recordings/*.mp3

which writes `baseline/Bono/1.wav`, `2.wav`, … ready to mount into the
container.

The signal processing is pure and lives in functions, so it can be tested
without an audio file — see [tests/test_prepare_voice.py](../tests/test_prepare_voice.py).
"""

from __future__ import annotations

import argparse
import struct
import sys
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

#: What XTTS wants its reference audio at.
TARGET_RATE = 22050

#: The generator ignores anything past this in a clip.
MAX_CLIP_SECONDS = 10.0

#: And a clip shorter than this carries too little of a voice to be worth one
#: of the twenty-five slots.
MIN_CLIP_SECONDS = 2.0

#: How many clips to write. The README's mandatory list says twenty-five or
#: fewer and "10 is fine"; more than that is diminishing returns against a
#: model that only reads the first ten seconds of each anyway.
MAX_CLIPS = 12

#: Below this a sample is silence. Not zero: a real recording has a noise floor,
#: and trimming only exact zeros trims nothing at all.
SILENCE_LEVEL = 0.01

#: A pause longer than this is a join between two things somebody said, and the
#: README asks for it to become two clips.
SILENCE_SECONDS = 0.5

#: Peak to normalise to. Just under full scale, because a clip that touches 1.0
#: is a clip that may already have clipped.
PEAK = 0.97


def decode(path: Path) -> tuple[np.ndarray, int]:
    """Any audio file as mono float32, at whatever rate it was stored at.

    Through PyAV rather than a new dependency — see the module docstring.
    Returns an empty array for anything unreadable, so one bad file in a folder
    costs that file.
    """
    try:
        import av
    except ImportError:  # pragma: no cover - PyAV ships with faster-whisper
        raise SystemExit(
            "PyAV is needed to read audio files. It comes with faster-whisper:\n"
            "    pip install faster-whisper") from None

    try:
        with av.open(str(path)) as container:
            streams = [s for s in container.streams if s.type == "audio"]
            if not streams:
                print(f"  {path.name}: no audio track", file=sys.stderr)
                return np.zeros(0, dtype=np.float32), 0
            stream = streams[0]
            rate = int(stream.rate or 0)
            blocks = [frame.to_ndarray() for frame in container.decode(stream)]
    except Exception as exc:
        print(f"  {path.name}: could not read it ({exc})", file=sys.stderr)
        return np.zeros(0, dtype=np.float32), 0

    if not blocks:
        return np.zeros(0, dtype=np.float32), 0
    return to_mono(blocks), rate


def to_mono(blocks: list[np.ndarray]) -> np.ndarray:
    """Decoded frames as one mono float32 track.

    PyAV hands back a (channels, samples) array for planar formats and a
    (1, samples * channels) one for packed — and integer formats for anything
    that was not float to begin with. All three are normalised here, because
    getting it wrong produces audio that plays at the wrong speed or as noise
    rather than raising anything.
    """
    parts: list[np.ndarray] = []
    for block in blocks:
        data = np.asarray(block)
        if data.dtype.kind == "i":
            data = data.astype(np.float32) / float(1 << (data.dtype.itemsize * 8 - 1))
        elif data.dtype.kind == "u":
            data = (data.astype(np.float32) - 128.0) / 128.0
        else:
            data = data.astype(np.float32)
        if data.ndim > 1 and data.shape[0] > 1:
            data = data.mean(axis=0)
        parts.append(np.asarray(data).reshape(-1))
    if not parts:
        return np.zeros(0, dtype=np.float32)
    return np.concatenate(parts).astype(np.float32)


def resample(audio: np.ndarray, source: int, target: int) -> np.ndarray:
    """To the target rate, through the app's own resampler.

    The same one playback uses, so a clip prepared here and a clip played back
    later cannot disagree about what resampling means.
    """
    from pitradio import speech

    return speech.resample(audio, source, target)


def normalise(audio: np.ndarray, peak: float = PEAK) -> np.ndarray:
    """Up to a consistent peak. Silence is left alone rather than amplified."""
    loudest = float(np.max(np.abs(audio))) if audio.size else 0.0
    if loudest <= 0.0:
        return audio
    return (audio * (peak / loudest)).astype(np.float32)


def _loud(audio: np.ndarray, rate: int, level: float) -> np.ndarray:
    """A boolean per sample: is there voice here?

    Smoothed over a short window, because a waveform crosses zero constantly
    and a per-sample test would call the middle of every vowel silence.
    """
    window = max(1, int(rate * 0.02))
    energy = np.convolve(np.abs(audio), np.ones(window) / window, mode="same")
    return energy > level


def trim(audio: np.ndarray, rate: int, level: float = SILENCE_LEVEL) -> np.ndarray:
    """Silence off both ends. The README asks for this "ruthlessly"."""
    if audio.size == 0:
        return audio
    loud = _loud(audio, rate, level)
    if not loud.any():
        return np.zeros(0, dtype=np.float32)
    first, last = int(np.argmax(loud)), int(len(loud) - np.argmax(loud[::-1]))
    return audio[first:last]


def split_on_silence(audio: np.ndarray, rate: int, *,
                     level: float = SILENCE_LEVEL,
                     gap: float = SILENCE_SECONDS) -> list[np.ndarray]:
    """Split where somebody stopped talking for longer than `gap`.

    What the README means by "for any other silence or pauses longer than ~0.5
    seconds, split the clip into two separate clips at that point". A pause in
    the middle of a reference clip teaches the model to pause.
    """
    if audio.size == 0:
        return []
    loud = _loud(audio, rate, level)
    if not loud.any():
        return []

    minimum = max(1, int(rate * gap))
    pieces: list[np.ndarray] = []
    start: int | None = None
    quiet_from: int | None = None

    for index, speaking in enumerate(loud):
        if speaking:
            if start is None:
                start = index
            quiet_from = None
            continue
        if start is None:
            continue
        if quiet_from is None:
            quiet_from = index
        elif index - quiet_from >= minimum:
            pieces.append(audio[start:quiet_from])
            start, quiet_from = None, None

    if start is not None:
        pieces.append(audio[start:quiet_from if quiet_from else len(audio)])
    return [piece for piece in pieces if piece.size]


def chunk(audio: np.ndarray, rate: int,
          seconds: float = MAX_CLIP_SECONDS) -> list[np.ndarray]:
    """Cut anything longer than the limit into pieces at most that long.

    Handing over a two-minute clip is not an error — the generator reads the
    first ten seconds and throws the rest away. Cutting it up turns one clip
    into twelve, which is the whole recommended set out of one recording.
    """
    limit = int(rate * seconds)
    if limit <= 0 or audio.size <= limit:
        return [audio] if audio.size else []
    return [audio[at:at + limit] for at in range(0, audio.size, limit)]


def write_wav(path: Path, audio: np.ndarray, rate: int) -> None:
    """32-bit float PCM, mono. Hand-rolled, for the reason `wave` cannot.

    The standard library's `wave` writes integer PCM only, and the format asked
    for here is IEEE float — the same mismatch that made `speaking.read_wav`
    hand-rolled at the other end of this pipeline.
    """
    samples = np.asarray(audio, dtype="<f4")
    data = samples.tobytes()
    fmt = struct.pack("<HHIIHH", 3, 1, rate, rate * 4, 4, 32)
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "wb") as handle:
        handle.write(b"RIFF")
        handle.write(struct.pack("<I", 4 + 8 + len(fmt) + 8 + len(data)))
        handle.write(b"WAVE")
        handle.write(b"fmt " + struct.pack("<I", len(fmt)) + fmt)
        handle.write(b"data" + struct.pack("<I", len(data)) + data)


def prepare(audio: np.ndarray, rate: int, *, max_clips: int = MAX_CLIPS,
            min_seconds: float = MIN_CLIP_SECONDS) -> list[np.ndarray]:
    """One recording, as clips that meet every requirement.

    Split, trimmed, chunked, filtered and normalised, in that order — and the
    order matters. Normalising last means each clip is normalised on its own,
    so a quietly-spoken passage comes back up rather than staying quiet
    relative to a loud one somewhere else in the same recording.
    """
    clips: list[np.ndarray] = []
    for piece in split_on_silence(audio, rate):
        for part in chunk(trim(piece, rate), rate):
            if part.size >= int(rate * min_seconds):
                clips.append(normalise(part))
    # Longest first: more of a voice per slot, and the generator only ever
    # reads the first ten seconds of each.
    clips.sort(key=len, reverse=True)
    return clips[:max_clips]


def inspect(path: Path) -> tuple[str, bool]:
    """(a line describing this clip, whether it is usable).

    Every check is one of the generator's own requirements, measured off the
    file rather than trusted — a clip that went through the wrong path, or was
    dropped in by hand from an audio editor, looks identical in a folder
    listing and produces a clone that is subtly wrong for no visible reason.
    """
    import struct

    from pitradio.engineer import speaking

    raw = path.read_bytes()
    audio, rate = speaking.read_wav(path)
    if audio.size == 0:
        return f"{path.name:<10} unreadable", False

    try:
        tag, channels = struct.unpack("<H", raw[20:22])[0], struct.unpack(
            "<H", raw[22:24])[0]
        bits = struct.unpack("<H", raw[34:36])[0]
    except (struct.error, IndexError):
        tag, channels, bits = 0, 0, 0

    seconds = audio.size / rate
    peak = float(np.max(np.abs(audio)))
    # The quietest tenth of the clip, as a stand-in for the noise floor.
    frame = max(1, rate // 50)
    usable = audio[:len(audio) - len(audio) % frame]
    rms = np.sqrt((usable.reshape(-1, frame) ** 2).mean(axis=1))
    floor = float(np.percentile(rms, 10)) if rms.size else 0.0
    snr = 20 * np.log10(peak / max(floor, 1e-6))

    problems = []
    if tag != 3 or bits != 32:
        problems.append(f"not 32-bit float (tag {tag}, {bits}-bit)")
    if channels != 1:
        problems.append(f"{channels} channels")
    if rate != TARGET_RATE:
        problems.append(f"{rate}Hz, wanted {TARGET_RATE}")
    if peak >= 0.99:
        problems.append("clipped")
    if seconds < MIN_CLIP_SECONDS:
        problems.append("too short")
    if seconds > MAX_CLIP_SECONDS + 0.1:
        problems.append(f"{seconds:.0f}s — only the first 10 will be read")
    if snr < 25:
        problems.append("noisy")

    line = (f"{path.name:<10} {seconds:5.1f}s {rate:6d}Hz  peak {peak:.2f}  "
            f"{snr:3.0f}dB  {', '.join(problems) if problems else 'ok'}")
    return line, not problems


def verify(folder: Path) -> int:
    """Check a baseline folder before anybody spends an hour of GPU on it."""
    clips = sorted(folder.glob("*.wav"),
                   key=lambda p: (len(p.stem), p.stem))
    if not clips:
        print(f"no clips in {folder}", file=sys.stderr)
        return 1

    print(f"{'file':<10} {'length':>6} {'rate':>8}  {'peak':<6} {'SNR':>5}  notes")
    print("-" * 66)
    total, bad = 0.0, 0
    for clip in clips:
        line, fine = inspect(clip)
        print(line)
        bad += not fine
        audio, rate = __import__(
            "pitradio.engineer.speaking", fromlist=["x"]).read_wav(clip)
        total += audio.size / rate if rate else 0
    print("-" * 66)
    print(f"{len(clips)} clips, {total:.0f}s of speech")

    if len(clips) < 3:
        print("The generator wants at least three.", file=sys.stderr)
        return 1
    if total < 30:
        print("Under 30 seconds in total — the README's quickstart figure. "
              "More reference audio gives a better clone.", file=sys.stderr)
    print("Not usable as-is." if bad else "Ready to generate.")
    return 1 if bad else 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Prepare baseline clips for crew-chief-autovoicepack.")
    parser.add_argument("sources", nargs="*", type=Path,
                        help="audio files, in any format FFmpeg reads")
    parser.add_argument("--verify", type=Path, metavar="FOLDER",
                        help="check an existing baseline folder and stop")
    parser.add_argument("--name", default="",
                        help="the voice name; clips go in baseline/<name>/")
    parser.add_argument("--out", type=Path, default=Path("baseline"),
                        help="where the baseline folder lives (default: baseline)")
    parser.add_argument("--max-clips", type=int, default=MAX_CLIPS,
                        help=f"how many clips to write (default: {MAX_CLIPS})")
    args = parser.parse_args(argv)

    if args.verify:
        return verify(args.verify)
    if not args.sources:
        parser.error("give some audio files, or --verify a folder")

    tracks: list[np.ndarray] = []
    for source in args.sources:
        if not source.exists():
            print(f"  {source}: not found", file=sys.stderr)
            continue
        audio, rate = decode(source)
        if audio.size == 0 or rate <= 0:
            continue
        print(f"  read {source.name}: {audio.size / rate:.1f}s at {rate}Hz")
        tracks.append(resample(audio, rate, TARGET_RATE))

    if not tracks:
        print("nothing readable to work with", file=sys.stderr)
        return 1

    if not args.name:
        parser.error("--name is required when preparing clips")
    clips = prepare(np.concatenate(tracks), TARGET_RATE,
                    max_clips=args.max_clips)
    if len(clips) < 3:
        print(f"only {len(clips)} usable clip(s) — the generator wants at "
              f"least three of at least {MIN_CLIP_SECONDS:.0f}s. Record more, "
              f"or check the input is speech.", file=sys.stderr)
        return 1

    target = args.out / args.name
    for index, clip in enumerate(clips, start=1):
        write_wav(target / f"{index}.wav", clip, TARGET_RATE)

    total = sum(clip.size for clip in clips) / TARGET_RATE
    print(f"\nwrote {len(clips)} clips ({total:.0f}s of speech) to {target}")
    print(f"32-bit float PCM, mono, {TARGET_RATE}Hz — mount this as /app/baseline")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

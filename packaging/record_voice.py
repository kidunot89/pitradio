"""Record a voice, either as reference clips or as a finished pack.

Two things somebody might want to record, and they are not the same job:

**Reference clips** — about two minutes of you saying anything at all, which
XTTS clones from. Ten clips, ten seconds each. What you say does not matter;
the model is learning what you *sound* like, not what you said. This is the
input `crew-chief-autovoicepack` wants.

**The phrases themselves** — reading the engineer's 171 lines aloud. Slower,
but it needs no GPU, no Docker and no cloning: what comes out is a finished
voice pack of real human speech, which is better than any synthesiser will
manage. Worth knowing this is an option before renting anything.

Either way the recording problems are the same, and they are the ones the
generator's README warns about: clipping, background noise, and silence left on
the ends. So every take is measured as it is saved and said so at the time —
finding out that forty takes clipped is a session wasted, and it is exactly the
kind of thing a headset mic does when it is too close to somebody's mouth.

    python packaging/record_voice.py --check                 # test the mic
    python packaging/record_voice.py --name Bono --reference # ~2 min, for cloning
    python packaging/record_voice.py --name Bono --phrases   # a pack, no GPU

Interactive, so it is a script rather than part of the app.
"""

from __future__ import annotations

import argparse
import csv
import sys
import time
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from prepare_voice import (  # noqa: E402  (path set above)
    TARGET_RATE,
    normalise,
    resample,
    trim,
    write_wav,
)

#: How long a reference clip runs for. The generator reads ten seconds and
#: ignores the rest, so this is exactly ten.
REFERENCE_SECONDS = 10.0

#: And how many of them. The README's mandatory list says at most 25 and "10 is
#: fine"; ten clips is two minutes of talking, which is not a big ask.
REFERENCE_CLIPS = 10

#: The longest a single phrase can run. "Nobody is in that class" is under two
#: seconds; this is loose enough not to clip anybody's delivery.
PHRASE_SECONDS = 4.0

#: Above this a take has clipped and is not recoverable — the peaks were cut
#: off before the sound card ever saw them.
CLIPPING = 0.99

#: And below this there is not enough signal to normalise without bringing the
#: room up with it.
TOO_QUIET = 0.04

#: What to read for the reference clips. Anything works; these are here so
#: nobody has to think of something ten times in a row, and they are
#: deliberately racing-flavoured so the model hears the words it will be saying.
#:
#: **Each has to fill the ten seconds.** One that runs out after six leaves
#: four seconds of room in the clip, which is the single thing the generator's
#: README is most insistent about. A test counts the words.
PROMPTS = (
    "Right, we're looking at about a two second gap to the car ahead, and "
    "he's struggling for grip through the final sector.",
    "Box this lap, box this lap. We'll go for the medium tyre and a full "
    "load of fuel, and you'll come out just behind the safety car.",
    "That's a good lap, a really good lap. You found two tenths in sector "
    "one and held everything else, which is exactly what we needed.",
    "Careful here, there's a car stopped on the exit of turn six and the "
    "marshals have got yellow flags out through the whole sector.",
    "We're going to need to save some fuel over the next few laps, so lift "
    "and coast into the heavy braking zones and we'll reassess.",
    "The car behind is two seconds back and closing at about three tenths a "
    "lap, so you've got maybe six laps before he's in range.",
    "Track is clear ahead, you've got a good run now. Push for three laps "
    "and then we'll look at where we are.",
    "Understood, we'll take a look at the data. Keep doing what you're "
    "doing and don't worry about the mirrors for now.",
    "Traffic in turn three, a slower car on the racing line. Be patient "
    "through there and you'll get a better exit onto the straight.",
    "Last lap, last lap. Bring it home safely, everything is looking good "
    "on our side and the gap behind you is comfortable enough that you do "
    "not need to take any risks.",
)


def _sd():
    try:
        import sounddevice as sd
    except ImportError:
        raise SystemExit("sounddevice is needed to record:\n"
                         "    pip install sounddevice") from None
    return sd


def devices() -> None:
    """Every input the machine has, for `--list-devices`."""
    from pitradio import speech

    for index, label in speech.list_devices("input"):
        print(f"  {index:3d}  {label}")


def record(seconds: float, device=None) -> tuple[np.ndarray, int]:
    """Capture from the microphone, at whatever rate it prefers.

    The device's own rate, then resampled once at the end — asking a microphone
    for a rate it does not run at is the same refusal that silenced playback,
    and there is no reason to risk it twice.
    """
    sd = _sd()
    from pitradio import speech

    index = speech.resolve_device(device, "input")
    try:
        info = sd.query_devices(index if index is not None else None, "input")
        rate = int(float(info["default_samplerate"]))
    except Exception:
        rate = 44100

    frames = int(rate * seconds)
    audio = sd.rec(frames, samplerate=rate, channels=1, dtype="float32",
                   device=index)
    sd.wait()
    return np.asarray(audio).reshape(-1), rate


def verdict(audio: np.ndarray) -> tuple[str, bool]:
    """(what to tell them, whether to keep it).

    Said at the time rather than discovered later. A headset mic sitting in
    front of somebody's mouth clips on every plosive, and forty clipped takes
    is a session wasted.
    """
    if audio.size == 0:
        return "nothing recorded", False
    peak = float(np.max(np.abs(audio)))
    if peak >= CLIPPING:
        return (f"CLIPPED (peak {peak:.2f}) — move the mic off to the side of "
                f"your mouth, or turn the input gain down"), False
    if peak < TOO_QUIET:
        return (f"too quiet (peak {peak:.2f}) — move the mic closer, or turn "
                f"the input gain up"), False
    return f"ok (peak {peak:.2f})", True


def check(device=None) -> int:
    """Five seconds of the room, then five of speech. For `--check`."""
    print("Testing the microphone.\n")
    print("  Stay quiet for five seconds...")
    time.sleep(0.7)
    room, rate = record(5.0, device)
    floor = float(np.sqrt(np.mean(room ** 2))) if room.size else 0.0
    print(f"  room noise: {floor:.4f} RMS at {rate}Hz")
    if floor > 0.02:
        print("  That is loud for a background. A fan, a PC, or an open "
              "window will end up in every clip.")
    else:
        print("  Quiet enough.")

    print("\n  Now say something for five seconds, normally...")
    time.sleep(0.7)
    speech_audio, _rate = record(5.0, device)
    told, fine = verdict(speech_audio)
    print(f"  your voice: {told}")

    if speech_audio.size and floor > 0:
        ratio = float(np.max(np.abs(speech_audio))) / max(floor, 1e-6)
        print(f"  signal to noise: {20 * np.log10(max(ratio, 1e-6)):.0f} dB "
              f"({'good' if ratio > 30 else 'marginal — try a quieter room'})")
    return 0 if fine else 1


def _take(prompt: str, seconds: float, device, *, attempts: int = 3):
    """One recording, retried while it is unusable."""
    for attempt in range(attempts):
        input(prompt if not attempt else "    again — press Enter: ")
        audio, rate = record(seconds, device)
        audio = normalise(trim(resample(audio, rate, TARGET_RATE), TARGET_RATE))
        told, fine = verdict(audio)
        print(f"    {told}")
        if fine:
            return audio
        if attempt < attempts - 1:
            print("    let's do that one again.")
    return None


def reference(name: str, out: Path, device=None,
              clips: int = REFERENCE_CLIPS) -> int:
    """About two minutes of speech, for a cloner to learn a voice from."""
    target = out / name
    print(f"Recording {clips} reference clips for '{name}'.\n"
          f"Read each line at your normal speaking pace. What you say does "
          f"not matter —\nthe model is learning what you sound like.\n")

    kept = 0
    for index in range(clips):
        line = PROMPTS[index % len(PROMPTS)]
        print(f"[{index + 1}/{clips}] {line}")
        audio = _take("    press Enter, then read it: ",
                      REFERENCE_SECONDS, device)
        if audio is None:
            print("    skipping this one.\n")
            continue
        kept += 1
        write_wav(target / f"{kept}.wav", audio, TARGET_RATE)
        print()

    if kept < 3:
        print(f"Only {kept} usable clip(s). The generator wants at least "
              f"three.", file=sys.stderr)
        return 1
    print(f"Wrote {kept} clips to {target}\n"
          f"32-bit float PCM, mono, {TARGET_RATE}Hz — mount as /app/baseline")
    return 0


def phrases(name: str, inventory: Path, out: Path, device=None,
            takes: int = 1) -> int:
    """Read the engineer's own lines, producing a pack with no GPU involved."""
    if not inventory.exists():
        print(f"No phrase list at {inventory}. The Engineer tab writes one "
              f"with 'Write phrase list'.", file=sys.stderr)
        return 1

    with open(inventory, encoding="utf-8") as handle:
        rows = list(csv.DictReader(handle))
    wanted: dict[str, str] = {}
    for row in rows:
        folder = row["audio_path"].replace("\\", "/").rstrip("/").split("/")[-1]
        wanted.setdefault(folder, row["text_for_tts"])

    print(f"Recording {len(wanted)} phrases for '{name}', {takes} take(s) "
          f"each.\nSay each one plainly, the way it would come over a radio. "
          f"Ctrl-C stops;\nwhat you have recorded is kept and the rest fall "
          f"back to the Windows voice.\n")

    root = out / name / "voice" / "pitradio"
    done = 0
    try:
        for index, (folder, text) in enumerate(sorted(wanted.items()), start=1):
            print(f"[{index}/{len(wanted)}] \"{text}\"")
            for take in range(1, takes + 1):
                audio = _take(f"    take {take} — press Enter: ",
                              PHRASE_SECONDS, device)
                if audio is None:
                    continue
                write_wav(root / folder / f"{take}.wav", audio, TARGET_RATE)
                done += 1
            print()
    except KeyboardInterrupt:
        print("\n  stopped.")

    print(f"\nWrote {done} clips to {out / name}")
    print("Drop that folder into the voice packs directory and pick it in "
          "the Engineer tab.")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Record reference clips, or a whole voice pack.")
    parser.add_argument("--name", help="the voice's name")
    parser.add_argument("--device", help="input device name or index")
    parser.add_argument("--list-devices", action="store_true")
    parser.add_argument("--check", action="store_true",
                        help="test the microphone and stop")
    parser.add_argument("--reference", action="store_true",
                        help=f"record {REFERENCE_CLIPS} clips for cloning")
    parser.add_argument("--phrases", action="store_true",
                        help="read the engineer's lines, making a pack directly")
    parser.add_argument("--takes", type=int, default=1,
                        help="takes per phrase, with --phrases (default: 1)")
    parser.add_argument("--inventory", type=Path,
                        default=ROOT / "voices" / "phrase_inventory.csv")
    parser.add_argument("--out", type=Path, default=Path("baseline"),
                        help="where to write (default: baseline)")
    args = parser.parse_args(argv)

    if args.list_devices:
        devices()
        return 0
    if args.check:
        return check(args.device)
    if not args.name:
        parser.error("--name is required")
    if args.phrases:
        return phrases(args.name, args.inventory, args.out, args.device,
                       max(1, args.takes))
    if args.reference:
        return reference(args.name, args.out, args.device)

    parser.error("choose --reference, --phrases, --check or --list-devices")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())

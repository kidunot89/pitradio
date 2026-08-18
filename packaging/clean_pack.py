"""Throw away the takes a voice generator got wrong.

XTTS rambles on short text. Asked for "fifteen" it will usually say fifteen,
and sometimes say fifteen and then keep going for twelve seconds. A real
example from a real pack:

    fifteen    0.39s   2.54s   12.57s

All three passed the generator's own integrity check with a score of 1.00,
because that check asks whether the audio is *valid speech*, not whether it is
the phrase that was requested. Nothing downstream can tell either: the file is
well-formed, the right length to be a word, and in the right folder.

**And a pack picks between takes at random.** That is the whole reason for
having several — a spotter that says "car left" identically forty times an hour
stops sounding like a person. So one bad take in three is not a rare annoyance,
it is a one-in-three chance on every single call.

The signal that works is **the siblings**. A phrase recorded three times should
take about the same time three times, and when one of them is four times longer
than the shortest, the shortest is the one that said only what was asked. That
holds without knowing anything about the language, the voice or the words —
which is what makes it safe to apply to a pack somebody else generated.

The second check is against **the text itself**, for the case where every take
rambled and there is no good sibling to compare against. Speech runs at roughly
two and a half words a second; a clip several times longer than its words can
account for did not say only those words.

Nothing is deleted. Rejected takes move to a `rejected/` folder beside the
pack, so a pack can be re-checked with different thresholds and so anybody
suspicious of this can listen to what it threw away.

    python packaging/clean_pack.py voices/Geoff
    python packaging/clean_pack.py voices/Geoff --dry-run
"""

from __future__ import annotations

import argparse
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

#: How much longer than its shortest sibling a take may be before it is
#: considered to have said something extra.
#:
#: Generous, deliberately. Real delivery varies — a take with a breath in it,
#: or one said a little more deliberately, is not wrong. Four times as long is
#: not variation, it is a different sentence.
SIBLING_RATIO = 2.2

#: Seconds of speech per word, for the fallback check.
#:
#: Read-aloud English runs about two and a half words a second, so this is
#: generous by roughly half again — the point is to catch a clip that ran away,
#: not to grade somebody's diction.
SECONDS_PER_WORD = 0.65

#: And a floor, because a one-word phrase legitimately carries lead-in and a
#: trailing breath that a per-word figure does not account for.
BASE_SECONDS = 0.9

#: Below this there is no speech at all — the generator produced a click.
MIN_SECONDS = 0.15


def duration(path: Path) -> float:
    """Seconds of audio in a clip, or 0 if it cannot be read."""
    from pitradio.engineer import speaking

    audio, rate = speaking.read_wav(path)
    return audio.size / rate if audio.size and rate else 0.0


def expected(phrase: str) -> float:
    """The longest this phrase could reasonably take to say."""
    words = len([w for w in phrase.replace("_", " ").split() if w])
    return BASE_SECONDS + max(1, words) * SECONDS_PER_WORD * 2.0


def judge(phrase: str, takes: dict[str, float]) -> dict[str, str]:
    """take name -> why it was rejected, for the ones that were.

    Pure, so the rule can be tested against made-up numbers rather than against
    a two-hundred-megabyte pack.
    """
    verdicts: dict[str, str] = {}
    usable = {name: length for name, length in takes.items()
              if length >= MIN_SECONDS}
    for name, length in takes.items():
        if length < MIN_SECONDS:
            verdicts[name] = f"silent ({length:.2f}s)"

    if not usable:
        return verdicts

    shortest = min(usable.values())
    limit = expected(phrase)
    for name, length in usable.items():
        if length > shortest * SIBLING_RATIO:
            verdicts[name] = (f"{length:.2f}s against a {shortest:.2f}s "
                              f"sibling")
        elif length > limit:
            # Every take rambled, so there is no good sibling to measure
            # against. Fall back on what the words themselves can account for.
            verdicts[name] = f"{length:.2f}s for {len(phrase.split('_'))} word(s)"

    # Never reject everything: a phrase with no takes falls back to the
    # synthesiser, which is worse than the least-bad recording of it.
    if len(verdicts) == len(takes):
        keeper = min(usable, key=usable.get)
        verdicts.pop(keeper, None)
    return verdicts


def clean(pack: Path, *, dry_run: bool = False) -> int:
    """Move every bad take out of a pack. Returns how many were rejected."""
    root = pack / "voice"
    root = root if root.is_dir() else pack
    folders = sorted(p for p in root.rglob("*") if p.is_dir()
                     and any(p.glob("*.wav")))
    if not folders:
        print(f"no clips under {pack}", file=sys.stderr)
        return 0

    rejected_root = pack.parent / f"{pack.name}-rejected"
    total, moved = 0, 0
    for folder in folders:
        takes = {w.name: duration(w) for w in sorted(folder.glob("*.wav"))}
        total += len(takes)
        verdicts = judge(folder.name, takes)
        if not verdicts:
            continue
        print(f"{folder.name}:")
        for name, why in sorted(verdicts.items()):
            print(f"    {name}  {why}")
            if dry_run:
                continue
            target = rejected_root / folder.relative_to(root) / name
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(folder / name), str(target))
            moved += 1

    kept = total - moved
    print(f"\n{total} takes, {len(folders)} phrases")
    if dry_run:
        wrong = sum(len(judge(f.name, {w.name: duration(w)
                                       for w in f.glob('*.wav')}))
                    for f in folders)
        print(f"would reject {wrong}; nothing moved (--dry-run)")
    else:
        print(f"kept {kept}, moved {moved} to {rejected_root}")
    return moved


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Reject voice-pack takes that say more than they were asked to.")
    parser.add_argument("pack", type=Path, help="the pack folder")
    parser.add_argument("--dry-run", action="store_true",
                        help="report what would go, and move nothing")
    args = parser.parse_args(argv)

    if not args.pack.is_dir():
        print(f"{args.pack} is not a folder", file=sys.stderr)
        return 1
    clean(args.pack, dry_run=args.dry_run)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

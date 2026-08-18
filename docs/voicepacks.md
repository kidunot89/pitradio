# Generating a voice pack

The engineer sounds like a person only if it has recordings of one. This is how
to make them, using [`crew-chief-autovoicepack`][ccavp] — a Coqui XTTS v2
pipeline, MIT-licensed, whose folder layout PitRadio deliberately shares.

[ccavp]: https://github.com/cktlco/crew-chief-autovoicepack

Before anything else, two facts that decide the rest of the plan:

* **The generator is Docker-only, and its GPU path is CUDA.** `--gpus all`, no
  ROCm and no Metal. On an AMD or Apple machine you are on `--cpu_only`, which
  the README puts at "12 hours or more" against "1 to 2 hours with a modern
  GPU".
* **It only reads the first ten seconds of each reference clip.** A long
  recording is not better input, it is nine wasted seconds — which is what
  [prepare_voice.py](../packaging/prepare_voice.py) exists to fix.

## 1. Get the reference audio right

Its README is blunt about this: "the source of your issues is very likely from
imperfections in the input audio recordings". The requirements are

| | |
| --- | --- |
| Format | 32-bit float PCM WAV, mono, 22.05 kHz |
| Clips | at least 3, at most 25, around 10 is right |
| Length | 10 seconds or less each — longer is silently truncated |
| Level | normalised to full scale |
| Silence | trimmed off both ends, and any pause over ~0.5s split into two clips |

The README writes the rate as "22.5 kHz", which is not a rate anything uses.
XTTS's reference rate is 22050 and that is what the tool below writes.

**Record about two minutes**, reading anything — the README says not to obsess
over emotional range, "as the xtts model tends to diminish those aspects
anyway". Then:

```bash
python packaging/prepare_voice.py --name Bono recordings/*.wav
```

It reads anything FFmpeg reads (`.wav`, `.mp3`, `.m4a`, a `yt-dlp` download),
mixes to mono, resamples, splits on pauses, trims, cuts to ten seconds,
normalises each clip on its own and writes `baseline/Bono/1.wav`… Decoding goes
through PyAV, which is already a dependency, so this costs the project nothing.

It refuses rather than guesses if fewer than three usable clips come out.

**On cloning somebody else.** The generator's README has no ethics or licensing
section at all — I checked — and actively suggests pulling a voice from a
YouTube video or imitating "a professional voice actor you choose". That is its
position, not this project's. A voice is a likeness, and a pack cloned from a
real person is fine to keep on your own machine and is not something to publish
or ship. PitRadio will not distribute one.

## 2. Rent a GPU, if you need to

Renting an hour of an NVIDIA card is cheaper than leaving a CPU running
overnight, and avoids installing WSL2. Any provider works; these are the steps
on RunPod, which is what this was tested against.

1. **Deploy a Pod** — GPU Cloud, any card with **8GB or more**. The model needs
   ~2.6GB of VRAM, so almost anything qualifies; an RTX 4090 gets you the
   README's 8-replicas-in-parallel figure, an A4000 or 3090 is plenty for one.
2. **Template**: anything CUDA with Docker available, or run the image
   directly. Give it **60GB of disk** — the image alone is ~15GB and each pack
   is ~2GB.
3. **Upload the baseline clips.** They are a few megabytes:

   ```bash
   runpodctl send baseline/Bono
   ```

   or `scp -P <port> -r baseline/Bono root@<host>:/workspace/baseline/`.

4. **Run it**, from the pod's shell:

   ```bash
   docker run -it --rm --gpus all \
     -v /workspace/output:/app/output \
     -v /workspace/baseline:/app/baseline \
     -v /workspace/phrase_inventory.csv:/app/phrase_inventory.csv \
     ghcr.io/cktlco/crew-chief-autovoicepack:latest
   ```

   then, at its prompt:

   ```bash
   python3 generate_voice_pack.py --your_name 'Champ' --voice_name 'Bono'
   ```

   `--your_name` is what the engineer calls *you*; keep it generic if the pack
   will ever be shared.

5. **Bring it back**: `runpodctl send /workspace/output/Bono`, and **stop the
   pod**. A pod left running bills whether or not anything is generating.

Budget roughly an hour of GPU time per voice, plus the image pull. The process
is restartable and idempotent — existing files are skipped unless `--overwrite`
— so a pod that dies mid-run costs only what it had not finished.

## 3. Use PitRadio's phrase list, not Crew Chief's

The shipped `phrase_inventory.csv` has ~9,100 rows of Crew Chief's own
vocabulary. PitRadio says a different set of things, so generating theirs would
take hours to produce phrases the engineer never uses and none of the ones it
does.

The Engineer tab writes the right list: **Write phrase list**, which produces
`phrase_inventory.csv` in the voice packs folder — 171 phrases at 3 takes,
about 513 clips, a few minutes of GPU time rather than hours. Mount it over the
container's own, as in the command above.

It is generated from `lines.vocabulary()` rather than kept by hand, so it cannot
drift from what the engineer actually says — and a test asserts that every
fragment of every kind of call appears in it. That check is the reason the list
is worth trusting: a phrase the inventory never asked for is a word the pack
does not have, and the engineer drops into the Windows voice for it in the
middle of a sentence.

**Numbers are in the list**, and that is why lap times are built from fragments
rather than spoken as one phrase — see [engineer.md](engineer.md). Sixty-odd
number clips cover every lap time there is.

## 4. Install it

Drop the folder into the voice packs directory — the Engineer tab's **Open
folder** button goes straight there — and pick it in the Voice list. Its name is
the folder's name.

A pack does not need to be complete. Anything it does not have is synthesised
by the Windows voice, so a half-generated pack works and simply falls back more
often. Driver names always do, in every pack, because no generated set can
contain them.

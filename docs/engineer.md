# The engineer

A named voice that watches the sim and talks to you: your lap times, cars
alongside, and routines you start by saying something.

It shares the push-to-talk key with everything else PitRadio does. Hold the
trigger, say "Chief, target P3", let go — and instead of that going to the chat
box, the engineer answers.

**It is off until you switch it on.** Nothing is spoken until you have been to
Settings → Engineer and ticked the box.

---

## Quick start

1. Open **Settings → Engineer** and tick **Engineer on**.
2. Pick one of the four engineers. That sets its name, its voice and how much
   it talks.
3. Press **Test**. You should hear it say its name back.
4. Set the **Output device** to your headset — the same one you use for voice
   chat, not the sim's output.
5. Drive. It will read your lap time out at the line.
6. Hold the trigger and say **"Chief, target P3"** to start the corner coach
   against whoever is third.

If Test says nothing, see [Nothing is spoken](#nothing-is-spoken) at the bottom.

---

## Talking to it

There are two ways a sentence becomes a command, and both are deliberately
narrow. The same key sends messages to everyone in your session, so a command
the engineer *invents* is a message that silently never arrives.

**Say its name first.** "Chief, target P3." The name comes first, the phrase
comes straight after it. `hey`, `ok` and `right` are allowed in front of the
name.

**Or say a phrase on its own** — but only phrases that do not take a driver.
"initiate corner coaching" works with nothing in front of it. "target
Verstappen" does not, because "target" could start an ordinary sentence and its
argument has no end: *"target time is a twenty three"* would otherwise be eaten
whole and never reach the chat box.

Saying just the name gets "go ahead", the way a real radio would — and keeps a
stray "Chief" out of a message to twenty other people.

**"Stop"** always works, whatever is running and whatever started it. So do
"stand down", "cancel", "that's enough" and "forget it".

Anything the engineer does not recognise is a message, and goes to the chat box
exactly as it did before.

---

## Choosing an engineer

Four ship with the app:

| | Voice | Style |
| --- | --- | --- |
| **Chief** | male | Steady and complete. "Turn four, Tandy was faster on the exit, two tenths." |
| **Ada** | female | Clipped. Drops the corner number: "Tandy, faster exit, two tenths." |
| **Marshall** | male | Slower and fuller, if the others feel rushed. |
| **Vic** | female | Quick and short. The least talking of the four. |

They are **presets, not recordings** — a name, a preferred Windows voice, a
pace, and how much they say. That is worth being plain about, because "four
voices" usually means four sets of audio: a generated voice pack is one to two
gigabytes, and shipping four would be an eight gigabyte download to replace
something already on every Windows machine for free.

Each one picks the best installed Windows speech voice matching its preference
and your language. On a stock Windows 11 install there are usually two or three,
so two engineers may share a voice and differ by pace and phrasing. If you want
a specific one, set **Windows voice** and it overrides the preset.

**Called** is what it answers to. Set it to anything — the name is only used for
addressing it, and "Bob, target P3" works exactly as well.

---

## What it tells you

### Lap times

Reads your lap out as you cross the line, and says when it was your best. On by
default.

### Spotter

Calls cars alongside: "car left", "car right", "cars both sides", then "clear"
once they have gone. Off by default, and there is one thing to know about it.

**Which side is which could not be verified without a car on a track.** The
positions come from the sim's own world coordinates, and whether the maths comes
out as left or right depends on a handedness convention this project could not
check from a development machine. So if it calls "left" for a car on your right,
turn on **Swap spotter sides** in Profiles → the game's plugin settings. It is
one tick, once.

Everything else about the spotter is exact: it uses in-game positions, it drops
height (so a bridge or the Le Mans esses does not put somebody on your door),
and it does not call cars on an adjacent straight.

---

## Routines

A routine is something the engineer does until told to stop. You start it by
saying a phrase, and **you choose the phrase.**

In Settings → Engineer, each routine has a box of trigger phrases, one per line.
Whatever you type replaces the defaults — so if you want your engineer to start
coaching when you say *"initiate build procedures"*, type that, and that is what
starts it. A phrase ending in `{driver}` takes whatever you say next as the
target.

Only one routine runs at a time. Starting one stands down whatever was running,
because two of them commenting on the same lap is what makes people switch the
whole thing off.

### The corner coach

The one that ships. It targets a driver's quickest lap and, at each corner,
tells you whether they were quicker on the entry or the exit and by how much.

> Chief, target Tandy
>
> *Targeting N.Tandy. Best lap, one twenty five point two seven.*
>
> *Turn one, N.Tandy was faster on the exit, six tenths.*
>
> *Turn four, you had a better entry, two tenths.*

Default phrases:

- `target {driver}` — by name, or "target P3", or "target the leader"
- `coach me against {driver}`
- `initiate corner coaching` — no target named, so whoever is quickest

**How it finds corners.** There is no track map and there is not going to be
one — that would need a file per circuit, would go stale with every layout
change, and would leave the feature working on the four tracks somebody got
round to. Instead a corner is a place where the reference lap slowed down and
sped back up. That is true on every circuit in every sim. The cost is that
corners are numbered rather than named: "turn four", not "Arnage". Chicanes
count as one corner.

**What it compares.** Entry is from where the reference lap started braking to
its slowest point; exit is from there to where the speed had recovered. The time
through each is read off the recorded clock at those two points, so it is a
measurement, not an estimate.

**It says nothing when the two laps agree.** Below the corner threshold
(0.08 seconds by default, on the Engineer tab) there is no call. An engineer
that says something at every corner is one you stop listening to by the third
lap.

**It describes, it does not instruct.** "Tandy was faster on the exit" is what
the app actually knows. It does not know whether that was braking, line, tyres
or a tow, and "brake later" would be a guess wearing the clothes of coaching.

### What it will not use as a reference

- A lap with any part of it in the pit lane. A quick lap that was really a
  shortcut through the pits would otherwise become the target everybody is
  measured against, and nothing about it would look wrong.
- A lap you joined halfway through.
- A lap the sim gave no time for — an out lap, or a car that has just arrived.
- Anything from a different track. Changing circuit clears everything.

It also stays quiet while you are spectating. Commenting on a lap you are
watching rather than driving would be nonsense.

---

## Other languages

The engineer speaks whatever language you have set for **transcription**, unless
you pin it on the Engineer tab. That is the right default and not an arbitrary
one: your commands arrive through Whisper, so if Whisper is producing Spanish
then an engineer listening for English phrases will never hear a single one.

Everything it says — including the trigger phrases — goes through the same
translation catalogues as the window. Adding a language is a JSON file in
`src/pitradio/locale/`; see the main README.

**Numbers are spelled out in English and read as digits everywhere else.** Not
laziness: number grammar is genuinely per-language — German inverts the tens and
units, Spanish fuses the twenties — and a half-done implementation would produce
confident nonsense in somebody's own language. Digits hand the problem to the
speech voice for that language, which already solves it correctly. The knock-on
effect is that a non-English voice pack cannot cover numbers, and they come out
synthesised.

---

## Voice packs

A voice pack replaces the synthesiser with recorded audio: a folder of WAV
files, one folder per phrase, several takes each. The engineer picks a take at
random, which is most of why a pack sounds like a person and text-to-speech does
not.

**The layout is Crew Chief's**, on purpose:

```
%APPDATA%\pitradio\voices\
  Ada\
    voice\
      corners\
        two_tenths\
          a.wav
          b.wav
```

A flat `<pack>/<phrase>/*.wav` works too, which is what you get from recording
your own.

That layout means a pack generated by
[crew-chief-autovoicepack](https://github.com/cktlco/crew-chief-autovoicepack)
can be dropped straight in. To generate one for PitRadio's phrases rather than
Crew Chief's:

1. Settings → Engineer → **Write phrase list**. This writes
   `phrase_inventory.csv` into the voices folder, in the engineer's language.
2. Feed that inventory to the generator in place of its own.
3. Put the output folder under `voices\` and pick it in **Voice pack**.

**Names and numbers are never in a pack** and are always spoken by the Windows
voice. There is no way round it — no pack can hold every driver's name or every
lap time — so a call like "turn four, Tandy was faster on the exit" is part
recorded and part synthesised. That seam is audible. It is still the right
trade: the alternative is a pack that goes unused the moment a driver is named,
which is most calls.

Packs are stored beside your config, not in the install directory, so an update
does not delete a gigabyte of audio you chose to install.

---

## How it fits together

The engineer runs on **its own thread**, separate from the four PitRadio already
has, and speaking gets a thread below that. Neither can hold up the keyboard
hook, the worker, or the window.

Everything it does is allowed to fail. The engineer going quiet must never cost
you a trigger, a transcription, or a message in the chat box — so if anything in
here breaks, the words go to the chat box as they always did and the problem is
a line in the log.

It reads the sim ten times a second through the same plugin that supplies driver
names for mentions. There is no second data path and no extra connection to the
game.

---

## Nothing is spoken

**Test does nothing.** The synthesiser runs in a PowerShell host using
`System.Speech`, which is part of the .NET Framework on every Windows 10 and 11
machine. Check the log for `no speech host` — a locked-down machine with
PowerShell blocked is the usual cause.

**You can hear it, but not in your headset.** Set the Output device on the
Engineer tab. It defaults to the system default, which during a race is often
the wheel's speaker.

**It reads lap times but never coaches.** The corner coach needs a reference
lap. Until the driver you targeted has completed one — cleanly, not through the
pits — there is nothing to compare against. The Engineer tab's status line says
how many corners it has mapped.

**It coaches but says nothing at some corners.** That is the design: those
corners were within the threshold. Lower **Corner threshold** if you want more.

**It answers to nothing you say.** Check the name on the Engineer tab, and
remember that any phrase taking a driver needs the name in front of it. Say
"Chief" on its own — if you get "go ahead", it is listening and the phrase is
the problem.

**It ate a message.** It should not. If the engineer took something you meant to
send, the log line says `that was for the engineer` along with what it matched —
please open an issue with that line, because the matcher being too eager is the
one bug in this feature that costs something real.

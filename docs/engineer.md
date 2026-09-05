# The engineer

A named voice that watches the sim and talks to you: your lap times, cars
alongside, and answers when you ask it something.

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

## Who it watches

The engineer keeps a **focus**: one driver it measures you against. You do not
have to set it. By default it is **the car ahead in your class**, or the car
behind when you are leading it — because there is nobody ahead to chase, and
the question becomes whether you are keeping them there.

It only follows a change once the position has **held for eight seconds**.
Positions churn: measured at a live race start, fifteen different drivers were
the car ahead inside ninety seconds, and every switch threw away the laps the
watcher had gathered, so it never had enough to say anything at all.

Say so if you want somebody else:

- `focus on {driver}` — or "keep an eye on", "keep tabs on", "study", "watch"
- `default focus` — back to choosing on its own
- `stop focusing` — off, and it stays off until asked back

A driver you name is never overruled. Wandering back to whoever is ahead two
corners later is the app disagreeing with you.

The Status tab shows who is being watched, and `what are we watching` asks.

---

## Asking it things

Every question has a box of phrases in Settings → Engineer, one per line, and
whatever you type replaces the defaults. Each can be switched off; a question
that is off contributes no phrases at all, so its words reach the chat box like
any others rather than being taken and answered with nothing.

- **Your car** — `what's my best lap`, `how are the tyres`, `what's the damage`,
  `how's the fuel`, `how much fuel do I need to finish the race when I pit on
  the next lap`
- **The session** — `who has the fastest lap`, `who's fastest`,
  `who has the fastest sector`, `who's in the lead`, `who's ahead`
- **Where the time goes** — `where am I slower`, `where am I faster`, either of
  them with `than {driver}` on the end

**Questions jump the queue.** A question asked while the engineer is mid-call
used to wait behind it or be dropped outright — the queue holds six and a busy
lap fills it — so you asked, heard it talking about something else, and got no
answer. An answer now clears the ordinary traffic, cuts what is being said, and
cannot be squeezed out. It still yields to the spotter, because a car alongside
is about not crashing.

**A question it cannot answer stays out of the chat box.** "Who's faster?" is
not a phrase it knows, and it used to fall through and go out to the session.
Anything that reads as a question — it ends in a question mark, or opens with
an interrogative — is answered "say again" instead. There is a tick box on the
Text chat tab if you would rather have the old behaviour, and a question you
have explicitly switched off still reaches chat, because switching it off is
how you say those words are yours.

You can put the engineer's name at either end: "Bono, how are the tyres" and
"how are the tyres, Bono" both work. The comma is what marks it as a name, so
`focus on Bono` still focuses on a driver called Bono.

### Where am I slower

The one worth knowing about. It compares you with your focus **corner by
corner, averaged over every lap this session** rather than read off one — a
single lap says what happened on that lap, and the question is about what keeps
happening.

> Chief, where am I slower
>
> *Turn three, you are slower on entry, two tenths.*
>
> *Turn seven, they have a better exit, a tenth.*

It says *how*, not just where: entry, exit, braking later, or slower through
the whole corner. Where a corner catalogue exists for the circuit it uses the
name — "Eau Rouge" rather than "turn three".

**How it finds corners.** There is no track map and there is not going to be
one — that would need a file per circuit, would go stale with every layout
change, and would work on the four tracks somebody got round to. A corner is a
place where the reference lap slowed down and sped back up, which is true on
every circuit in every sim. Chicanes count as one corner.

**It describes, it does not instruct.** "They have a better exit" is what the
app knows. It does not know whether that was line, tyres or a tow, and "brake
later" would be a guess wearing the clothes of coaching.

**If laps are missing it says whose.** Yours or theirs — otherwise "no laps to
compare" leaves you guessing which.

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

## When your sim cannot answer

Not every sim publishes the same things, and the engineer says so rather than
guessing. Original Assetto Corsa, for instance, publishes **your own car and
nothing about anybody else** — no other driver's name, position or lap time —
so anything comparing you with the grid has no data by any route.

Ask "who's leading" there and it answers **"this game doesn't say"**. That is
deliberate and it is not the same as "nobody to watch", which means the grid
really is empty. Telling a driver sitting seventh that nobody is ahead of them
is not an unhelpful answer, it is a false one.

Behaviours that need data your sim does not supply are skipped with a line in
the log rather than left switched on and silent:

```
Spotter is on but this sim does not publish positions or spotter; it will stay quiet
```

`--telemetry` prints what your sim is actually sending — see the last section.

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

**Set in Settings → Voice**, not on the Engineer tab. The engineer and the
coach speak in the same voice out of the same queue, so it is one setting for
the app rather than one per feature.

Two packs ship with it: **Norman** and **Claudia**. Both were generated with
Piper — see [voicepacks.md](voicepacks.md) — and either can be replaced by a
pack of your own.

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

1. Settings → **Voice** → **Write phrase list**. This writes
   `phrase_inventory.csv` into the voices folder, in the engineer's language.
2. Feed that inventory to the generator in place of its own.
3. Put the output folder under `voices\` and pick it in
   **Settings → Voice**.

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
Audio tab. It defaults to the system default, which during a race is often
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

---

## Checking what your sim is actually sending

Most "the engineer says nothing" problems are not the engineer. Start the game,
get **on track and moving**, then:

```bash
python -m pitradio --telemetry
```

It prints every car as the engineer sees it — lap distance, speed, lap count,
sector, lap times, pit flag, world position — and, more usefully, compares
consecutive reads and tells you whether anything is changing.

That last part matters more than it sounds. A sim that is paused or sitting in
a menu keeps publishing a block that looks completely healthy: cars, positions,
speeds, all plausible. Nothing moves, so the engineer has nothing to say, and
no single snapshot shows that. If it reports

> Nothing changed across 4 reads, including the sim's own clock.

then the game is paused, in a menu, or the session has ended — not broken.

What to look for when it *is* live:

| Column | Feeds |
| --- | --- |
| `lapdist`, `speed` | corner detection, and where the time goes |
| `lap`, `last lap`, `best lap` | lap time and fastest lap calls |
| `sec` — changes three times a lap | every sector call |
| `world x/y/z` — different per car | the spotter |

The `provides:` line at the top says which of those the plugin claims to
supply. A behaviour that needs something absent is skipped rather than left
switched on and silent, and the log says which capability is missing.

## What each sim can do

Sims publish very different things, and a behaviour whose data is missing is
**skipped with a line in the log** rather than left switched on and silent.

| | Le Mans Ultimate | iRacing | Assetto Corsa / Competizione / Evo | Automobilista 2, Project CARS 2 / 3 |
| --- | --- | --- | --- | --- |
| Lap times | yes | yes | yes | derived |
| New fastest lap | yes | yes | — | yes |
| Sector calls | yes | — | yes | — |
| Where am I slower | any driver | any driver | your own best | any driver |
| Who is ahead / leading | yes | yes | — | yes |
| Spotter | geometry | the sim's own call | Competizione only | geometry |
| Driver mentions, "P3" | yes | yes | — | yes |

The gaps are the games, not the app:

- **iRacing** publishes no per-car sector splits, so sector calls have nothing
  to work from. Its spotter is the best of the lot — `CarLeftRight` comes from
  the real car bodies, so it needs no swap setting and no width guess.
- **Assetto Corsa** publishes lap times for your car only and no driver names
  at all. That is why there are no standings and no mentions, and why "where am
  I slower" chases your own best lap — which is what a practice session is for
  anyway.

  The original game goes further: it publishes **no other car at all**, not
  even a position. Checked against a live eight-car race, the coordinate array
  held the player in slot zero and untouched memory in every other — zeros, a
  NaN, a denormal. So the spotter has nothing to work from there either, and
  the plugin says so per session rather than per game: **Competizione does**
  publish the array, and the same plugin reports positions for it.
- **Automobilista 2 and Project CARS** carry lap *counts* rather than lap
  times in the part of their block worth trusting, so lap times are measured
  here with a stopwatch. A lap that spans a pause comes out longer than it was;
  that fails safe, since an inflated lap never becomes the reference a
  comparison chases. Their sector field is an enum that could not be pinned down from
  outside the games, so sector calls are not offered.

Automobilista 2 has its own entry rather than sharing the Project CARS one, so
you can pick the game you are actually running and so the two keep separate
spotter and proximity settings.

**Le Mans Ultimate is the only one verified against the running game.** Every
other reader is tested against shared memory built by hand, which catches a
wrong field width, a mis-decoded name or a padding mistake — and cannot catch a
wrong assumption about what the sim puts where. Run `--telemetry` with the game
on track before trusting any of them, and Assetto Corsa Evo especially, which
is still early access and may move its layout.

**iRacing is marked experimental**, and shows as such in the profile picker.
Not because it is worse code than the others, but because nobody working on
PitRadio owns a copy — so unlike the rest, it will not get checked against the
real thing unless somebody who has it runs `--telemetry` and says what came
back. If that is you, please do; the note in the plugin list asks for exactly
that.

## Per-sim settings

Three of the engineer's numbers live on the **profile**, under the game's
plugin settings, not on the Engineer tab — because they describe the game
rather than your taste:

- **Swap spotter sides** — if "left" means a car on your right
- **Spotter overlap (metres)** — how far apart along the track still counts as
  side by side. A Hypercar is about 5m long
- **Spotter width (metres)** — how far to the side counts, before they are
  simply on another part of the circuit

Car lengths and axis conventions differ between sims, so a number that suits
one game is wrong in the next.

## Flags and incidents

A behaviour of its own, and separate from the spotter deliberately. Crew
Chief's own sound folders draw the line and it is the right one: `car_left`,
`still_there` and `clear_all_round` are in `spotter/`, while
`stopped_car_in_turn_3`, `slow_car_ahead` and `local_yellow_ahead` are in
`flags/`. The spotter answers "who is beside me", which is geometry. Flags
answer "what has happened to the track", which is not.

Deriving the second from the first is what produced a warning in every braking
zone: PitRadio had a rule saying a car much slower than you was a hazard, and
a braking zone is precisely where the car in front is much slower than you.
That rule is gone.

**Three sources, not equally trustworthy.**

*Full-course yellow* and *blue* come from the sim and are reliable — LMU's
`mGamePhase`, `mYellowFlagState` and per-car `mFlag` all read sanely against a
live session.

*Local yellows are derived*, because LMU's `mSectorFlag` is not usable. It is
documented as "whether there are any local yellows at the moment in each
sector" and reads `[11, 11, 1]` under a green flag, with the fields either side
of it correct — so it is not an offset that has slipped, LMU simply publishes
something else there. Read as booleans it would put a permanent yellow on the
whole circuit. So an incident here means what a marshal means by one: a car has
stopped on the road and has been there two seconds. That is a derivation from
data the sim does publish honestly, in the same spirit as finding the corners
in the speed trace rather than shipping a track map.

The cost is that the call cannot precede the incident — a real yellow is out
the moment the marshals see it, and this one waits to be sure. The benefit is
that it is never wrong about a green track, which is the failure that makes
people turn a feature off.

**Incidents are named by corner, not by driver.** At the speed this matters
"turn six" is something a driver can act on and a name is a syllable count they
cannot. The numbering is the lap book's, so a driver hears one set of corner
numbers rather than one feature using one set and the flags another; the
corners are found once per reference lap and cached, because `find_corners`
resamples a whole lap and this runs several times a second. With no reference
lap yet the sector is named instead.

**When you are the incident, the side calls stop.** Describing the cars going
past a spun car back to its driver is noise; the only useful question is
whether there is room to pull out, and [rejoin.py](../src/pitradio/engineer/rejoin.py)
answers it — comparing *time to be safe* against *time until the next car
arrives*, not distance against distance. A stationary car needs its whole
acceleration back before the first arrival. That is why the naive "three
seconds of clear track" answer gets people collected.

Two guards, both learned rather than assumed: nothing is said in the pit lane,
where being stationary is the point, and nothing before the car has ever
moved — sitting on the grid before the lights is stationary, on the racing
line, with the whole field behind, which is every input the rejoin advice looks
at.

See [voicepacks.md](voicepacks.md) for generating a voice.

## Questions

Distinct from routines, and the distinction is not bookkeeping. A routine is
something the engineer *starts doing* and goes on doing until it is stood down;
a question has an answer, and when the answer has been given there is nothing
running. Modelling one as the other would put "who has the fastest lap" in the
routines list, where every entry has a stop phrase and a tick-box, and neither
means anything for a question.

Three of them: the fastest lap, the fastest sector, and your own best.

**The parameter follows the keyword and is never part of the phrase.** What a
driver can ask about depends on the sim they are in — the classes on this grid,
the sectors this circuit has — and none of that belongs in a phrase somebody
typed into a settings box. "Who has the fastest sector" is the phrase; "three
in GT3" is what came after it, parsed against the session. A class is matched
through `mentions.class_aliases`, so LMU's "LMGT3" answers to "GT3" exactly as
it does everywhere else, and "LMP2" still refuses to answer to "P2" because
that is a position.

**A closed argument space is the false-positive defence**, and a better one
than counting words. `phrases.MIN_BARE_WORDS` protects routines by demanding
two words in front of an open-ended parameter; that is not enough here, because
"who has the fastest lap of my life that one" clears it easily and would be
taken as a question about a class called "of my life that one" — swallowing the
message. But a question's argument can only be a class on this grid, a sector
between one and three, or nothing. Anything else was not a question, whatever
it started with. Addressed by name it is one regardless: somebody who said the
engineer's name was talking to it.

**No class named means your own class**, because that is what somebody in a
GT3 car asking "who has the fastest lap" means. A class named that nobody is in
gets told so rather than being quietly answered with the overall figure — a
wrong answer stated confidently is the failure mode with no symptom.

Each has a tick-box on the Engineer tab and nothing else. The phrases a routine
answers to are worth putting in your own words; the questions this can answer
are fixed by what the sim publishes, and an editable box there would imply you
could invent one.

The switch earns its place for a different reason: **every phrase the engineer
listens for is a phrase that can be taken out of a message meant for the whole
session**, and somebody who never asks these has no reason to carry that risk.
Switching one off removes its phrases from the matcher entirely rather than
silencing it downstream — otherwise "who has the fastest lap" would still be
lifted out of the message and then answered with nothing, which is the worst of
both. Missing from the config means on, so adding a question never needs a
migration.

## The spotter, and where its numbers come from

Every threshold in `spotter.py` is Crew Chief's, read out of a local
installation rather than guessed at — its `ui_text/en.txt` names each setting
and `CrewChiefV4.exe.config` ships the defaults:

| Ours | Crew Chief's | Default |
| --- | --- | --- |
| `DEFAULT_CAR_LENGTH` | `lmu_spotter_car_length` | 4.5 (5 for pcars2/ACC, 4.4 for AMS2) |
| `GAP_FOR_CLEAR` | `spotter_gap_for_clear` | 0.5 m |
| `OVERLAP_DELAY` | `spotter_overlap_delay` | 50 ms |
| `CLEAR_DELAY` | `spotter_clear_delay` | 150 ms |
| `MIN_SPEED` | `min_speed_for_spotter` | 10 m/s |
| `MAX_CLOSING_SPEED` | `max_closing_speed_for_spotter` | 12 m/s |
| the repeat interval | `spotter_hold_repeat_frequency` | 3 s |

Three of those were missing here entirely and each was causing a fault the
driver could feel:

**The closing-speed limit is what catches the lapping car.** Something arriving
12 m/s quicker crosses the whole overlap window in well under a second, so by
the time the call has been spoken they have gone — and the driver holds a line
for a car that is no longer there.

**The minimum speed is what stops the pit lane and the grid.** Below 10 m/s the
cars around you are stationary or passing at walking pace, and calling those is
how a spotter ends up switched off.

**The two settling delays are what stop the chatter.** Two cars at the same
corner cross in and out of overlap as they breathe. They are deliberately
different lengths: the overlap delay is short because a warning that is late is
worthless, and the clear delay is longer because it can afford to be sure — a
driver who holds their line a tenth longer than necessary has lost nothing.

The clear range is `car length + gap`, not a second multiple of the length. The
distinction matters at the extremes: for a kart, "a further car length" is two
metres of hysteresis and the call hangs on far too long, while half a metre of
daylight is half a metre whatever you are driving.

**The spotter is silent under a full-course yellow** — Crew Chief's
`fcy_stop_spotter_immediately`, on by default. The field is bunched at a crawl
and permanently overlapping, so every call would be true and useless.

### What it says

The vocabulary is Crew Chief's `Sounds/voice/spotter/` folder, so a voice pack
built for Crew Chief speaks all of it without a mapping: `car_left`,
`car_right`, `still_there`, `hold_your_line`, `in_the_middle`, `clear_left`,
`clear_right`, `clear_all_round`, `three_wide_on_left`, `three_wide_on_right`.

Two of those replaced calls that stated the same fact the harder way round:

* **"Three wide, you're on the right"** was "two cars left". A driver hearing
  the old one has to work out where that leaves them, while they are busy; the
  new one says directly which way there is no room.
* **"In the middle"** was "three wide", for one car either side.

A repeat says `still there` on one side and `hold your line` on both, because
those are different instructions — one means do not move that way, the other
means do not move. The arrival and its repeats share a key derived from the
*counts*, so the repeat interval governs them; a key that changed with the
wording would make the follow-up a new call, due on the very next tick.

**The oval set is deliberately absent** — `car_inside`, `clear_outside`,
`three_wide_on_inside`. Which side is inside is a fact about the banking, which
none of the sims here publish and which Crew Chief keeps per-track. A guess at
it is a call that is confidently the wrong way round.

### Fuel

"How much fuel do I need to finish the race when I pit on the next lap", or
"...when I pit in five laps". **The answer is a percentage**, because that is
the number on the sim's own fuel screen and the driver has about four seconds
on the way to the pit entry to dial it in. Litres are the working.

**Consumption is measured, never assumed.** A car's use depends on the circuit,
the fuel map, the traffic and how the person is driving it, so litres-per-lap
here is what *this* car has been using over *these* laps — a short rolling
average, so it follows a change of fuel map rather than being dragged back by a
whole stint. Until a lap has been completed there is no answer and it says so.
A fuel number invented from nothing is the one wrong answer here that ends
somebody's race.

Three details that would otherwise be rediscovered:

* **The laps before the stop are not fuelled for.** What is in the tank now
  covers those. Only the ones after it are the question, which is why this
  never reads the current level.
* **`mMaxLaps` is `INT_MAX` in a timed session.** Taken at face value it asks
  for two billion laps' worth. `SessionInfo` carries `max_laps` *or* `ends_at`,
  never both, and the plugin decides which — the engineer never guesses the
  missing one. A timed race divides the remaining clock by the driver's own
  best lap and rounds **up**, because the flag falls at the end of the lap you
  are on when the clock runs out.
* **A fill above the tank's capacity is reported, not clipped.** It means the
  stop cannot be the last one, and a driver told "one hundred percent" without
  being told that plans a race that does not work.

Everything else rounds towards more fuel: running out is a retirement and
carrying a spare litre is a tenth a lap.

Fuel reaches `Car` for **the player's car only** — the sims publish tank
telemetry for the car you are driving and nobody else's — and it is attached by
matching `mID`, because LMU's telemetry array is indexed by
`playerVehicleIdx` while the scoring array is not. Attaching by position would
put your tank on whichever car happened to be scored in that slot.

## Being away from the wheel

The engineer says nothing, and **records nothing**, when the driver is not
driving. Three states, and they need three different signals:

* **Paused** — the sim's clock stops while this machine's does not, and the
  difference is the signal. Not `mGamePhase`: that read *green flag* right
  through a session sitting paused in the garage, with `mCurrentET` frozen at
  2218.0. The phase says what kind of session it is, not whether it is running.
* **In the garage** — the clock keeps running here, so the clock cannot be the
  signal. `mInGarageStall` is. Distinct from `in_pits`, which covers the whole
  pit lane: a car serving a stop is racing.
* **Handed to the AI** — `mControl` is 1, which is what spectating looks like.

**Nothing is observed either, not merely nothing said.** A paused sim
republishes the same frame forever, and feeding that to the lap book records a
car covering no ground for as long as somebody leaves the game sitting there —
a corrupt reference lap rather than a missing one. The spotter's state is
dropped on the way in for the same reason: a car that was alongside before the
pause is a fact about a moment that has gone.

**Pausing an online race is not detected, and cannot be.** The clock keeps
running there, because the race keeps running — the menu is open on this
machine and the cars are still going. Nothing in the shared memory separates
that from ordinary racing, and inventing a signal for it would silence the
engineer during a real race. Which is the worse mistake of the two.

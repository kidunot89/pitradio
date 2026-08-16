# Voice on the radio

PitRadio types what you said into the game's chat box. This adds the other half:
the people you are racing send you the *audio* as well, and you hear it.

It is deliberately not Discord. Discord already exists, works, and everyone is
already in one. What Discord cannot do is put you in a room with **whoever is in
this session**, without arranging it beforehand, and shut up the ones who are
four kilometres away.

## What travels

**The push-to-talk clip, on release. Not a live stream.**

The trigger cycle already records a clip while the key is held and hands it to
Whisper on release. Voice reuses that exact clip: on release it goes to Whisper
*and* to the relay, and peers play it back. Nothing about the recording path
changes.

A live stream would be a different application. It needs 20ms framing, a jitter
buffer, a mixer and a playback clock, all on the audio path, and the reward is
that people hear you 1.5 seconds sooner. The clip is what a pit radio is anyway:
you hold the button, you say a thing, it arrives.

The consequence worth knowing: **a clip is atomic.** It cannot be interrupted,
it arrives whole or not at all, and two people talking at once produce two clips
that queue rather than talk over each other. That is better than a race, not
worse.

## Who hears it

The relay is dumb. It fans a clip out to everyone in the room and makes no
decision about who should get it, because it cannot — it has no idea where
anyone is on the track, and giving it that information would be worse than
useless.

**Proximity is decided on the listener's machine.** LMU's shared memory carries
the world position of *every* car, not just yours, so each client already knows
exactly how far away every other driver is. Nothing positional is ever published
to the relay, and the feature works even if the relay operator is hostile.

**The listener's own view of where the speaker is wins.** A clip arrives after
its speaker stopped talking, so the position it carries is a second or two old
— at racing speed a hundred metres, which against a 200m radius decides the
answer. The scoring block has every car as of *now*, and the question is who is
near the target car when the message plays.

The clip carries the speaker's position anyway, as a fallback for somebody the
listener's block has not caught up with: a driver who just joined, or whose
entry has gone. A stale position beats none.

Preferring the local view also means a clip cannot talk its way past the filter.
A client claiming to be alongside you when it is a kilometre away is simply
measured where it actually is. That is a consequence of using the fresher
number, not a security mechanism — a speaker nobody can place at all is still
audible, because silence nobody can explain is the worse failure.

`proximity_only` on the LMU plugin turns this on; `proximity_metres` sets the
radius. Off, you hear the whole session, which is what you want in practice and
on a formation lap.

### Spectating

Proximity should be measured from the car on screen: watching a battle you are
in the middle of, while hearing the radio from four kilometres away where your
own car is parked, is not proximity in any sense a viewer would recognise.

It has to be **detected**. Somebody racing cannot reach a dropdown, and somebody
spectating should not have to.

**The shared memory block does not say**, and three plausible sources were
checked against a live spectated session and ruled out — each looks right and
none of them is:

- `telemetry.playerVehicleIdx` is the *player's* vehicle. While spectating
  somebody else it stayed pointed at the watcher's own parked car.
- `appInfo.mOptionsLocation` read 0 throughout.
- `$rFactor2SMMP_Graphics$` is published and would carry both a camera position
  and a viewed slot id — but LMU never populates it. The buffer is entirely
  zeros apart from its version counter, because the game does not call the
  graphics callback the rF2 plugin fills it from. The neighbouring Extended
  block was live at the same moment, so this is LMU's choice and not a broken
  install.

**LMU's own HTTP API does say.** `http://127.0.0.1:6397/rest/watch/standings` is
what the game's own overlays read, and each entry carries `hasFocus` — set on
the car being watched, distinct from `player`, which stays on your own. Its
`slotID` is the same number as `mID` in shared memory, so the two join directly.
It is part of the game rather than any plugin, so it needs nothing installed.

Read with a short timeout and cached for a second: this runs on the trigger
cycle, the response is ~16KB, and a dictation app must never wait on a game
that is mid-load. **Failures are cached too** — otherwise a closed game costs a
timeout on every single press.

Every failure yields None, and `SessionInfo.listener()` then falls back to the
driven car and finally to None, which `audible` reads as audible. Silently
keeping a parked car as the reference would filter the session by a place nobody
is looking at, and no listener could tell that apart from a broken feature.

**"Proximity" means on the track and nowhere else.** It is metres between two
cars in the game, read from the sim, computed locally. It has nothing to do with
where anybody lives, and no physical location is read, derived or transmitted.
Relay *hosting* below talks about distance too, in the network sense — that is a
routing question about servers and is unrelated to who you can hear.

## Which room

The session id is derived, never announced:

    sha256("pitradio/1:{mServerPublicIP}:{mServerPort}")[:32]

Everyone on the same game server computes the same id without anyone publishing
which server that is — the relay learns a hash and nothing else. Offline and
single-player have no server, so they produce no id and no room, which is the
correct behaviour rather than a special case.

The track is deliberately *not* in the key. It changes between sessions on the
same server, and a room that dissolves when the event moves to the next track is
a worse room.

Identity within a room is the driver name from the scoring block. `mSteamID` is
zero in practice, so there is nothing better available.

## The relay

**The relay's code and configuration are not in this repository.** PitRadio is
public; the server, its Terraform and its Ansible are private, along with the
OAuth client secret they need. Terraform and Ansible there exist for one job:
standing up a **racer-provided** voice host reproducibly, from a clean image.

The base relay address is not in this repository either. It is written into
[endpoints.py](../src/pitradio/endpoints.py) **at build time**, so a checkout —
or a fork — has no address at all and voice is simply unavailable. That is a
working state, not a broken one: better than every clone of the source pointing
a microphone at a server whose owner never agreed to carry it.

Nothing else in the app may hardcode an address. One place to overwrite, one
place to look when it is wrong.

    wss://<relay>/chat/{session-id}

One WebSocket per client, TLS, clips as binary frames with a small header. That
is the whole protocol. TLS because a relay is a stranger's machine and audio of
your voice should not cross it in the clear; WebSocket because it survives every
NAT and corporate firewall that raw UDP does not, and audio bandwidth for twenty
racers pressing a button occasionally is nothing.

**Not literally peer-to-peer.** True P2P needs ICE, STUN and a TURN fallback —
and TURN is a relay, so the fallback path is this design anyway, reached after
pulling a WebRTC stack into a Nuitka build that already fights native
dependencies. The relay is one small box and it is honest about being one.

### Community hosts

Relays exist to be *near the people talking*. A grid drawn from three continents
routed through one box in Frankfurt pays the Atlantic twice on every clip; a
relay chosen for the group does not. That is the whole reason racers can host:
not cost, and not decentralisation for its own sake — geography.

Terraform makes the machine; Ansible installs the relay, the systemd unit and
the TLS certificate, so a host is reproducible from a clean Ubuntu image with no
manual steps.

DigitalOcean OAuth first, since that is what exists today. Linode has a real
OAuth app flow and can follow. **AWS cannot**: it has no consumer OAuth for
provisioning — it is IAM keys or Identity Center SSO — so it needs its own path
and is not a matter of adding a button.

#### Choosing one is a group decision, not a personal one

**Every client in a session must pick the same relay, or they pick nothing.**
Left to themselves they would each choose the host closest to *them*, which for
a transatlantic grid means two relays, two rooms, and both halves of the session
sitting in what looks exactly like a working feature with nobody else in it.
This is the same silent failure as a mismatched session key, arrived at by a
different road.

So there is one coordinator, at the fixed base host, and it decides:

1. Clients join the room at the build's configured relay and report their measured
   round-trip to each candidate relay.
2. The coordinator picks the one with the best worst-case across the room —
   minimising the *slowest* racer's latency, not the average, because the point
   is that nobody is stranded.
3. It tells everyone to migrate, and they reconnect there together.

The base host is also the fallback relay, which is what makes this affordable:
the coordinator has to be always-on anyway, so it may as well carry the audio
for sessions too small or too local to be worth moving.

#### Why not bridge every host together

The obvious alternative: let each client connect to whichever relay is nearest
*it*, and have the relays forward clips to each other. It is a real design —
Mumble links servers this way — and it is genuinely more elegant in one respect,
because it deletes the group decision above. There is nothing to agree on if the
room spans every relay, so the split-room failure cannot happen at all.

It is still the wrong trade here, for one reason: **we send clips, not a live
stream.** A clip is dispatched after the speaker has stopped talking, so the
difference between 90ms and 250ms of routing is not a thing anybody can perceive
— which is most of the argument for geography, and all of the argument for
paying two extra hops to improve it.

What bridging costs is not hops, it is state. Relays would have to gossip room
membership, authenticate each other, and guard against loops and duplicate
delivery, and a racer-hosted relay joining that fabric can see traffic for rooms
it has no members in. That is a distributed-systems project bolted to the side
of a dictation app, in service of a latency budget this design does not have.

If PitRadio ever does go live-streaming, this reverses and bridging becomes the
right answer. The shape to build then: a full mesh with a shared secret, room
membership gossiped, and every clip carrying an id with a **one hop** limit
between relays — no transitive forwarding, which kills routing loops outright
and bounds fan-out instead of trusting it.

#### When a host goes away

A relay disappearing must not end the conversation. The coordinator holds the
room, notices the relay stop answering, re-runs the choice over what is left and
migrates the remaining racers — the same mechanism as the initial pick, so there
is no separate failover path to get wrong. Clients keep the coordinator
connection open for exactly this reason: it is the thing that survives.

A racer leaving the session does not take their relay with them mid-race. Their
machine is not the relay — a droplet they provisioned is — and pulling it out
from under the people still driving would be the worst possible moment for it.

#### When the session ends

Rooms are torn down, not left running. LMU reports its game phase, so a client
that sees the session end says so; when the last client leaves, or the room goes
quiet past an idle timeout, the coordinator closes it. A relay with no rooms
left is a candidate for `terraform destroy`, which is the difference between
this costing a racer a few pennies an event and costing them a droplet forever.

The idle timeout matters as much as the explicit signal. A client that crashes,
alt-tabs into oblivion, or loses its network never sends anything — so nothing
may depend on it doing so.

## Consent

Voice is **off until switched on**, per profile, and the window says who can hear
you before it says anything else. A dictation app that quietly opened the
microphone to twenty strangers would be a betrayal, however good the feature is.

Push-to-talk only. There is no open-mic mode and there should not be one: the
key is the consent.

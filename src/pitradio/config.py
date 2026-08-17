"""Config loading, validation and hot-reload.

Pure Python on purpose — no Win32, no tkinter — so `--check-config` runs
anywhere and the config layer stays testable off Windows.

Reload is mtime-based rather than watcher-based: the worker checks before each
trigger, which is the only moment a change can matter. Writes go through a
temporary file and os.replace so a half-written config can never be picked up
by that check.
"""

from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass, field, fields
from pathlib import Path
from typing import Any

from pitradio import endpoints, keys
from pitradio import languages as languages_mod

CONFIG_VERSION = 1

RACING_VOCABULARY = (
    # The app's own name and where to get it, first: initial_prompt weights
    # what comes early, and these are exactly the words a driver says to
    # someone asking about it mid-session. Without them Whisper produces
    # "pit radio", "get hub" and "kid unit 89".
    "PitRadio, GitHub, kidunot89, Le Mans, Hyperpole, Hypercar, LMP2, LMDh, "
    "GT3, Porsche 963, Ferrari 499P, "
    "Toyota GR010, Cadillac V-Series.R, BMW M Hybrid, Peugeot 9X8, Mulsanne, "
    "Tertre Rouge, Arnage, Porsche Curves, Indianapolis, Ford Chicanes, "
    "Eau Rouge, Raidillon, Blanchimont, Pouhon, Les Combes, Maggotts, Becketts, "
    "Copse, Stowe, 130R, Degner, Spoon, box this lap, full course yellow, "
    "safety car, slow zone, double stint, undercut, overcut, out lap, in lap, "
    "apex, understeer, oversteer, dirty air, slipstream, blue flag, penalty, "
    "drive through, stop and go, pit window, tyre pressures, brake bias, "
    "traction control, fuel saving, lift and coast, delta, purple sector."
)


@dataclass
class Profile:
    """Per-sim behaviour. Every field is tunable without restarting the app."""

    pre_keys: list[str] = field(default_factory=lambda: ["enter"])
    post_keys: list[str] = field(default_factory=lambda: ["enter"])
    abort_keys: list[str] = field(default_factory=lambda: ["escape"])
    pre_delay_ms: int = 350
    post_delay_ms: int = 80
    key_hold_ms: int = 40
    key_gap_ms: int = 40
    type_delay_ms: int = 8
    max_chars: int = 200
    text_mode: str = "unicode"
    # Whether to fire post_keys once the text is typed. Off leaves the message
    # sitting in the chat box for the driver to read and send themselves —
    # Whisper does mishear things, and an unreviewed mistake goes out to
    # everyone in the session.
    auto_send: bool = True
    # Which sim plugin supplies session data for this game. Empty means
    # none. Held on the profile rather than the plugin so one plugin can
    # serve several games.
    plugin: str = ""
    # Per-plugin options for this game. Defaults come from the plugin itself,
    # so a setting added later needs no rewrite of existing profiles.
    plugin_settings: dict = field(default_factory=dict)


@dataclass
class ReviewConfig:
    """Trigger gestures for a message left waiting when auto_send is off.

    A press shorter than `tap_ms` is a tap; anything longer is a hold, which
    clears the message and records a replacement. A second tap within
    `double_tap_ms` clears instead of sending.

    The cost of the double-tap gesture is that a single tap cannot act until
    that window closes, so `double_tap_ms` is the delay between tapping and the
    message going out. Set it to 0 to send immediately and give up clearing by
    double tap.
    """

    tap_ms: int = 300
    double_tap_ms: int = 350
    # Dedicated bindings, as an alternative to the gestures. Gestures cost
    # nothing to bind but make you count taps; a wheel with buttons to spare is
    # better served by one button per action. Both work at once — neither
    # disables the other. Empty means unbound.
    send_key: str = ""
    clear_key: str = ""


@dataclass
class MentionConfig:
    """Turning spoken driver names into mentions.

    No styling option, because neither LMU nor rFactor 2 chat supports markup:
    injection sends literal characters, so a prefix is all that can be done.
    """

    enabled: bool = True
    prefix: str = "@"
    # Off by default: a false match rewrites a word the driver actually said
    # into someone else's name, which is worse than leaving it alone.
    fuzzy: bool = False
    threshold: float = 0.85
    # Whether saying only a first name counts. On, because people do — but
    # names that double as racing speech ("max attack", "nick the inside
    # line") are never matched alone regardless.
    match_first_names: bool = True
    # The part that actually improves accuracy — telling Whisper who is in the
    # session so it hears the name correctly to begin with.
    add_names_to_vocabulary: bool = True
    max_names: int = 40


@dataclass
class AudioConfig:
    input_device: Any = None          # index, substring of the name, or null
    #: **Where everything the app plays comes out**: the record and stop cues,
    #: voice chat, and the engineer.
    #:
    #: One setting rather than one per feature, and on the Audio tab rather
    #: than scattered over three. Sound going to the wrong device is silent by
    #: nature — nothing raises, nothing logs, you simply hear nothing — so
    #: three separate pickers meant three chances to get it wrong and three
    #: places to go looking. That is not hypothetical: the engineer was found
    #: pointed at a Steam virtual microphone while the cues played on the
    #: headset, which presents exactly as "the engineer does not work".
    output_device: Any = None
    samplerate: int = 16000           # what Whisper wants; no resampling needed
    channels: int = 1
    min_clip_seconds: float = 0.3     # below this, don't even transcribe
    max_clip_seconds: float = 30.0
    # Software gain applied to captured audio. Raising the Windows device level
    # needs the Core Audio APIs; multiplying the samples achieves the same for
    # Whisper's benefit and works whatever the driver exposes.
    gain: float = 1.0


@dataclass
class WhisperConfig:
    model: str = "small.en"
    device: str = "cpu"               # never cuda: the GPU belongs to the sim
    compute_type: str = "int8"
    cpu_threads: int = 0              # 0 = let ctranslate2 decide
    beam_size: int = 5
    language: str = "en"
    vad_filter: bool = True
    initial_prompt: str = RACING_VOCABULARY
    # language code -> model size. `model` above is derived from the active
    # language and its size, so the worker keeps loading one plain model name
    # and knows nothing about any of this.
    languages: dict[str, str] = field(default_factory=lambda: {"en": "small"})


@dataclass
class CueConfig:
    enabled: bool = True
    start_hz: int = 880
    stop_hz: int = 620
    duration_ms: int = 60
    volume: float = 0.25


@dataclass
class VoiceConfig:
    """Sending the clip to the people you are racing, as well as typing it.

    **Off until switched on, and there is no open-mic mode.** A dictation app
    that quietly opened the microphone to twenty strangers would be a betrayal
    however good the feature is, so the trigger key is the consent: nothing
    leaves the machine that you did not hold a button to record.

    Who you can *hear* is not here — it belongs to the sim, because it depends
    on where the cars are, and only a plugin knows that. See the LMU plugin's
    `proximity_only` setting.
    """

    enabled: bool = False
    #: Where the room is coordinated. Supplied at build time — see
    #: `endpoints.py` — because this repository is public and the relay is not.
    #: Empty in a source checkout, which means voice is unavailable rather than
    #: pointed at a server nobody chose.
    relay: str = field(default_factory=endpoints.default_relay)
    #: Play what other people send. Separate from `enabled` on purpose — muting
    #: the session while still being heard is a normal thing to want mid-stint,
    #: and it is the first thing anyone reaches for.
    playback: bool = True
    volume: float = 0.8
    #: Drop a clip that took longer than this to arrive. Racing information goes
    #: stale: a warning about a car alongside is worse than useless once the
    #: corner is over, and playing it is actively misleading.
    max_age_seconds: float = 20.0
    #: The name other racers see. Empty means the one the sim already shows
    #: them, which is the right default — it is what is on their timing screen.
    display_name: str = ""


@dataclass
class NotificationConfig:
    """One behaviour: whether it speaks, and how often it repeats itself.

    `repeat_seconds` is what separates a call that is *true once* from one that
    stays true. A lap time never needs saying twice; a car alongside stops
    being there without anything happening, so the spotter has to keep saying
    it and then say when the side is clear. Zero means say it once per change
    and then stay quiet.
    """

    enabled: bool = True
    repeat_seconds: float = 0.0


@dataclass
class RoutineConfig:
    """One on-track routine's settings.

    `phrases` empty means "whatever the routine ships with", translated into
    the engineer's language. It is a list rather than a single string because
    people say the same thing several ways and remembering which one you
    configured is not a thing to do at 200km/h. `end_phrases` is the same for
    standing it down, on top of the global stop phrases which always work.
    """

    enabled: bool = True
    phrases: list[str] = field(default_factory=list)
    end_phrases: list[str] = field(default_factory=list)


@dataclass
class EngineerConfig:
    """A named voice that talks back: lap times, a spotter, and routines.

    **Off by default**, like voice chat, and for a related reason: a dictation
    app that started talking over the sim on first launch would be a surprise,
    and this one is a surprise with opinions about your driving.

    **There is one engineer**, and what it sounds like is a *voice pack*: a
    folder of recorded phrases, which is the only route to something that
    sounds like a person. Windows' own synthesiser is a fallback for the
    driver names no pack can contain. See `engineer/packs.py`.
    """

    enabled: bool = False
    #: What it is called, and therefore what it answers to.
    name: str = "Chief"

    # -- what it sounds like ----------------------------------------------
    #
    # **The voice is a pack.** A voice pack is a folder of recorded phrases —
    # see `engineer/packs.py` — and it is the only route to something that
    # sounds like a person. Windows' own synthesiser is a fallback, not a
    # choice: it exists so a driver's name, which no pack can contain, still
    # gets said.
    #
    # Because the engineer renders numbers as *words* rather than digits, a
    # generated pack covers lap times, sector times and deltas as well as the
    # fixed calls. Names are the only real gap.

    #: A folder under `voices/` in the config directory. Empty means every
    #: word comes from the synthesiser, which is worth hearing once and not
    #: worth racing with.
    voice_pack: str = ""
    #: The Windows voice used for anything the pack has never heard — driver
    #: names, and everything if no pack is installed. Empty lets Windows pick.
    fallback_voice: str = ""
    #: Windows' own -10..10, for that fallback voice only — a voice pack is a
    #: set of recordings and cannot be sped up. Empty means the engineer's own
    #: default, which is well above the synthesiser's natural pace: that pace
    #: is tuned for reading prose to somebody sitting still, and a race call
    #: competes with an engine.
    rate: int | None = None
    #: Whether it drops the lead-in: "Tandy, faster exit, two tenths" rather
    #: than "turn four, Tandy was faster on the exit, two tenths". The same
    #: information, for people who want different amounts of it.
    terse: bool = False
    volume: float = 0.9
    #: What language it speaks and listens for. Empty follows the transcription
    #: language, which is the right default by some distance: the commands are
    #: spoken into the same microphone Whisper is transcribing, so they arrive
    #: in that language or not at all.
    language: str = ""

    # -- what it talks about ---------------------------------------------
    #
    # Two kinds, and the split is the point. A **behaviour** is a tick-box that
    # runs for as long as the engineer does. A **routine** is started by saying
    # something and stands down again. They are separate sections of the
    # Engineer tab because they are separate ideas — and the same runner drives
    # both, so a routine is literally a set of behaviours with its own
    # conditions.

    #: notification id -> its settings. Defaults come from the notification
    #: classes, so a behaviour added later appears without any config being
    #: rewritten — the same rule plugin settings follow.
    notifications: dict[str, NotificationConfig] = field(default_factory=dict)
    #: routine id -> its settings.
    routines: dict[str, RoutineConfig] = field(default_factory=dict)

    #: Only talk about cars in your own class.
    #:
    #: On, because on an endurance grid the two facts are different and only
    #: one is about your race: a GT3 driver is not racing the Hypercars, and
    #: being told a Hypercar has taken the fastest lap is noise at best and
    #: misleading at worst. Sims with a single class are unaffected — there is
    #: only one class to be in.
    own_class_only: bool = True

    #: How much time in half a corner is worth interrupting for, in seconds.
    coach_threshold: float = 0.08
    #: The same, for a whole sector. Larger, because a sector is longer and a
    #: hundredth over thirty seconds is not something anybody drove differently.
    sector_threshold: float = 0.15


@dataclass
class GuiConfig:
    start_minimized: bool = False
    geometry: str = ""
    log_lines: int = 400
    # "system" follows the desktop's light/dark setting, which is what almost
    # everyone wants; "light" and "dark" pin it.
    theme: str = "system"
    # The *interface* language, which is not the transcription language — you
    # may well want a Spanish window and English chat, or the reverse.
    # "system" follows the desktop and falls back to English when that
    # language has no catalogue yet.
    language: str = "system"
    # The live controller readout in Settings > Trigger. Off by default: it
    # polls every device's button state, which is cheap but pointless while
    # you are driving.
    debug: bool = False


@dataclass
class UpdateConfig:
    repo: str = "kidunot89/pitradio"
    check_on_start: bool = True
    check_interval_hours: int = 24
    auto_install: bool = False        # opt-in; see README on why


@dataclass
class Config:
    version: int = CONFIG_VERSION
    enabled: bool = True
    trigger_key: str = "f13"
    # Wheel/gamepad equivalents of review.send_key and review.clear_key.
    # Top level rather than nested in ReviewConfig so every controller
    # binding has the same shape and the GUI can treat them alike.
    review: ReviewConfig = field(default_factory=ReviewConfig)
    mentions: MentionConfig = field(default_factory=MentionConfig)
    audio: AudioConfig = field(default_factory=AudioConfig)
    whisper: WhisperConfig = field(default_factory=WhisperConfig)
    cues: CueConfig = field(default_factory=CueConfig)
    voice: VoiceConfig = field(default_factory=VoiceConfig)
    engineer: EngineerConfig = field(default_factory=EngineerConfig)
    gui: GuiConfig = field(default_factory=GuiConfig)
    updates: UpdateConfig = field(default_factory=UpdateConfig)
    default_profile: Profile = field(default_factory=Profile)
    profiles: dict[str, Profile] = field(default_factory=dict)

    # -- construction ----------------------------------------------------

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Config:
        cfg = cls()
        cfg.version = int(data.get("version", CONFIG_VERSION))
        cfg.enabled = bool(data.get("enabled", True))
        cfg.trigger_key = str(data.get("trigger_key", cfg.trigger_key))

        cfg.review = _section(ReviewConfig, data.get("review"))
        cfg.mentions = _section(MentionConfig, data.get("mentions"))
        cfg.audio = _section(AudioConfig, data.get("audio"))
        cfg.whisper = _section(WhisperConfig, data.get("whisper"))
        cfg.cues = _section(CueConfig, data.get("cues"))
        cfg.voice = _section(VoiceConfig, data.get("voice"))
        cfg.engineer = _engineer(data.get("engineer"))
        cfg.gui = _section(GuiConfig, data.get("gui"))
        cfg.updates = _section(UpdateConfig, data.get("updates"))

        default_raw = dict(data.get("default_profile") or {})
        cfg.default_profile = _section(Profile, default_raw)

        cfg.profiles = {}
        for exe, raw in (data.get("profiles") or {}).items():
            # A profile only overrides what it names; everything else falls
            # through to default_profile.
            merged = {**default_raw, **(raw or {})}
            cfg.profiles[str(exe).lower()] = _section(Profile, merged)

        return cfg

    def to_dict(self) -> dict[str, Any]:
        return {
            "version": self.version,
            "enabled": self.enabled,
            "trigger_key": self.trigger_key,
            "review": asdict(self.review),
            "mentions": asdict(self.mentions),
            "audio": asdict(self.audio),
            "whisper": asdict(self.whisper),
            "cues": asdict(self.cues),
            "voice": asdict(self.voice),
            "engineer": asdict(self.engineer),
            "gui": asdict(self.gui),
            "updates": asdict(self.updates),
            "default_profile": asdict(self.default_profile),
            "profiles": {k: asdict(v) for k, v in self.profiles.items()},
        }

    # -- lookup ----------------------------------------------------------

    def profile_for(self, exe_name: str | None) -> tuple[Profile, str]:
        """Returns the profile and the name of what matched, for logging."""
        if exe_name:
            key = exe_name.lower()
            if key in self.profiles:
                return self.profiles[key], key
        return self.default_profile, "default"

    def profile_for_plugin(self, plugin_id: str) -> Profile:
        """The profile carrying a plugin's settings, found by the plugin.

        Voice needs `proximity_only` without knowing which window is focused —
        it runs on its own thread and asking would mean reaching for Win32 from
        a portable module. Going the other way works because the settings
        belong to the plugin's profile whichever game is in front.

        The first match wins. A plugin assigned to two games with different
        proximity settings is ambiguous by construction, and picking one beats
        inventing a rule nobody asked for.
        """
        if plugin_id:
            for profile in self.profiles.values():
                if profile.plugin == plugin_id:
                    return profile
        return self.default_profile

    # -- validation ------------------------------------------------------

    def _voice_problems(self) -> list[str]:
        """Checked even when voice is off, so a bad setting is found on save.

        Reporting it only once it is switched on means finding out at the worst
        possible moment — the first time somebody presses the button in a race.
        """
        problems: list[str] = []
        relay = (self.voice.relay or "").strip()

        if not relay:
            # Only a problem if you are trying to use it. A source build ships
            # without an address on purpose, and reporting that as broken would
            # make `--check-config` fail for everyone working on the app.
            if self.voice.enabled:
                problems.append(
                    "voice.enabled is on but voice.relay is empty; this build "
                    "has no default relay, so set one in Settings > Voice"
                )
        elif not relay.startswith(("ws://", "wss://")):
            problems.append(
                f"voice.relay is {relay!r}; it must be a ws:// or wss:// URL")
        elif relay.startswith("ws://") and not _is_loopback(relay):
            # The relay is somebody else's machine and the payload is a
            # recording of your voice. Plaintext would be a silent downgrade —
            # everything keeps working, and anyone on the path can listen.
            problems.append(
                f"voice.relay is {relay!r}; plaintext ws:// sends your voice in "
                f"the clear. Use wss:// (ws:// is allowed only for localhost)"
            )

        if not 0.0 <= self.voice.volume <= 1.0:
            problems.append("voice.volume must be between 0.0 and 1.0")
        if self.voice.max_age_seconds <= 0:
            problems.append(
                "voice.max_age_seconds must be > 0, or every clip is too old "
                "to play and voice silently does nothing"
            )
        return problems

    def _engineer_problems(self) -> list[str]:
        """Checked whether or not the engineer is switched on.

        A name it cannot answer to is the interesting one. The engineer's name
        is how a command is addressed, so an empty one silently narrows the
        feature to the bare phrases and nothing says why "Chief, target P3"
        stopped working.
        """
        problems: list[str] = []
        engineer = self.engineer

        if not 0.0 <= engineer.volume <= 1.0:
            problems.append("engineer.volume must be between 0.0 and 1.0")
        if engineer.rate is not None and not -10 <= int(engineer.rate) <= 10:
            problems.append("engineer.rate must be between -10 and 10")
        if engineer.coach_threshold <= 0:
            problems.append(
                "engineer.coach_threshold must be > 0, or every corner is worth "
                "a call and the engineer never stops talking"
            )
        if engineer.sector_threshold <= 0:
            problems.append("engineer.sector_threshold must be > 0")

        for notification_id, notification in (engineer.notifications or {}).items():
            if notification.repeat_seconds < 0:
                problems.append(
                    f"engineer.notifications[{notification_id!r}].repeat_seconds "
                    f"must be 0 or more; 0 means say it once")

        if engineer.enabled and not engineer.name.strip():
            problems.append(
                "engineer.name is empty, so the engineer cannot be addressed "
                "by name and only its bare trigger phrases would work")

        for routine_id, routine in (engineer.routines or {}).items():
            for field_name in ("phrases", "end_phrases"):
                spoken = getattr(routine, field_name)
                if not isinstance(spoken, list):
                    problems.append(
                        f"engineer.routines[{routine_id!r}].{field_name} must be "
                        f"a list")
                    continue
                for phrase in spoken:
                    if not str(phrase).strip():
                        problems.append(
                            f"engineer.routines[{routine_id!r}].{field_name} has "
                            f"an empty phrase, which would match nothing")
        return problems

    def validate(self) -> list[str]:
        """Everything wrong with this config, as human-readable lines.

        Returns problems rather than raising: the GUI wants to show all of them
        at once, and a single bad profile shouldn't stop the app starting.
        """
        problems: list[str] = []

        try:
            keys.parse_trigger(self.trigger_key)
        except keys.KeyNameError as exc:
            problems.append(f"trigger_key: {exc}")

        # Optional, so an empty string is "unbound" rather than a bad name.
        for label, spec in (
            ("review.send_key", self.review.send_key),
            ("review.clear_key", self.review.clear_key),
        ):
            if not spec:
                continue
            try:
                keys.parse_trigger(spec)
            except keys.KeyNameError as exc:
                problems.append(f"{label}: {exc}")

        if not isinstance(self.gui.language, str) or not self.gui.language:
            problems.append('gui.language must be "system" or a language code')

        if self.gui.theme not in ("system", "light", "dark"):
            problems.append('gui.theme must be "system", "light" or "dark"')

        if not isinstance(self.review.tap_ms, int) or not 0 <= self.review.tap_ms <= 2000:
            problems.append("review.tap_ms must be between 0 and 2000")
        if (not isinstance(self.review.double_tap_ms, int)
                or not 0 <= self.review.double_tap_ms <= 2000):
            problems.append("review.double_tap_ms must be between 0 and 2000")

        if not self.mentions.prefix:
            problems.append("mentions.prefix cannot be empty")
        if not 0.5 <= self.mentions.threshold <= 1.0:
            problems.append("mentions.threshold must be between 0.5 and 1.0")
        if not 1 <= self.mentions.max_names <= 200:
            problems.append("mentions.max_names must be between 1 and 200")

        if self.audio.samplerate != 16000:
            problems.append(
                f"audio.samplerate is {self.audio.samplerate}; Whisper expects "
                f"16000 and nothing here resamples"
            )
        if self.audio.channels != 1:
            problems.append(f"audio.channels must be 1, got {self.audio.channels}")
        if not 0.1 <= self.audio.gain <= 10.0:
            problems.append("audio.gain must be between 0.1 and 10.0")
        if self.audio.min_clip_seconds < 0:
            problems.append("audio.min_clip_seconds must be >= 0")
        if self.audio.max_clip_seconds <= self.audio.min_clip_seconds:
            problems.append("audio.max_clip_seconds must exceed min_clip_seconds")

        problems.extend(self._voice_problems())
        problems.extend(self._engineer_problems())

        if self.whisper.device != "cpu":
            problems.append(
                f"whisper.device is {self.whisper.device!r}; this app is "
                f"deliberately CPU-only so the GPU stays with the sim"
            )
        if self.whisper.beam_size < 1:
            problems.append("whisper.beam_size must be >= 1")

        # faster-whisper only warns about this and then transcribes English
        # anyway, so without a check here you would get silently wrong output
        # rather than an error. The ".en" models are English-only; multilingual
        # ones drop the suffix.
        for code, size in (self.whisper.languages or {}).items():
            if code not in languages_mod.WHISPER_LANGUAGES:
                problems.append(f"whisper.languages: {code!r} is not a Whisper language")
            if size not in languages_mod.SIZES:
                problems.append(
                    f"whisper.languages[{code!r}]: {size!r} is not one of "
                    f"{', '.join(languages_mod.SIZES)}"
                )

        english_only = self.whisper.model.endswith(".en")
        wanted = (self.whisper.language or "").strip().lower()
        if english_only and wanted not in ("", "en"):
            problems.append(
                f"whisper.model {self.whisper.model!r} is English-only but "
                f"whisper.language is {self.whisper.language!r}; use a "
                f"multilingual model such as {self.whisper.model[:-3]!r}"
            )

        if not 0.0 <= self.cues.volume <= 1.0:
            problems.append("cues.volume must be between 0 and 1")

        if self.updates.check_interval_hours < 1:
            problems.append("updates.check_interval_hours must be >= 1")
        if "/" not in self.updates.repo:
            problems.append(f"updates.repo must be 'owner/name', got {self.updates.repo!r}")

        problems.extend(_validate_profile(self.default_profile, "default_profile"))
        for exe, profile in self.profiles.items():
            problems.extend(_validate_profile(profile, f"profiles[{exe!r}]"))
            if not exe.endswith(".exe"):
                problems.append(
                    f"profiles[{exe!r}]: key should be an executable name such "
                    f"as 'game.exe' — the Status tab logs the real one"
                )

        return problems


def _validate_profile(profile: Profile, label: str) -> list[str]:
    problems: list[str] = []

    for attr in ("pre_keys", "post_keys", "abort_keys"):
        specs = getattr(profile, attr)
        if not isinstance(specs, list):
            problems.append(f"{label}.{attr} must be a list of key names")
            continue
        for spec in specs:
            try:
                keys.parse_combo(spec)
            except keys.KeyNameError as exc:
                problems.append(f"{label}.{attr}: {exc}")

    for attr in ("pre_delay_ms", "post_delay_ms", "key_hold_ms", "key_gap_ms",
                 "type_delay_ms", "max_chars"):
        value = getattr(profile, attr)
        if not isinstance(value, int) or isinstance(value, bool) or value < 0:
            problems.append(f"{label}.{attr} must be a non-negative integer")

    if profile.key_hold_ms < 20:
        problems.append(
            f"{label}.key_hold_ms is {profile.key_hold_ms}ms; games poll input "
            f"once per frame and often miss presses shorter than ~20ms"
        )
    if profile.max_chars < 1:
        problems.append(f"{label}.max_chars must be at least 1")
    if profile.text_mode not in ("unicode", "scancode"):
        problems.append(
            f"{label}.text_mode must be 'unicode' or 'scancode', got "
            f"{profile.text_mode!r}"
        )

    return problems


def _is_loopback(url: str) -> bool:
    """Whether a ws:// URL points at this machine.

    The one place plaintext is reasonable: a relay you are running yourself,
    while developing it. Matched on the host alone, so "wss://localhost.evil.com"
    is not mistaken for one.
    """
    from urllib.parse import urlsplit

    try:
        host = (urlsplit(url).hostname or "").lower()
    except ValueError:
        return False
    return host in {"localhost", "127.0.0.1", "::1"}


def _section(cls, raw: Any):
    """Build a dataclass from a dict, ignoring unknown keys and filling gaps."""
    known = {f.name for f in fields(cls)}
    values = {k: v for k, v in (raw or {}).items() if k in known}
    return cls(**values)


def _engineer(raw: Any) -> EngineerConfig:
    """The engineer section, whose routines are dataclasses rather than dicts.

    `_section` cannot do this on its own: it copies values straight across, so
    `routines` would come back as a dict of plain dicts and every attribute
    access on one would fail at the point of use rather than here.
    """
    cfg = _section(EngineerConfig, raw)
    cfg.routines = {
        str(routine_id): _section(RoutineConfig, settings)
        for routine_id, settings in (cfg.routines or {}).items()
    }
    cfg.notifications = {
        str(notification_id): _section(NotificationConfig, settings)
        for notification_id, settings in (cfg.notifications or {}).items()
    }
    return cfg


# -- persistence ---------------------------------------------------------


def load(path: Path) -> Config:
    with open(path, encoding="utf-8") as handle:
        return Config.from_dict(json.load(handle))


def save(path: Path, cfg: Config) -> None:
    """Atomic write, so a hot-reload can never read a partial file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with open(tmp, "w", encoding="utf-8") as handle:
        json.dump(cfg.to_dict(), handle, indent=2)
        handle.write("\n")
    os.replace(tmp, path)


class ConfigStore:
    """Holds the live config and reloads it when the file changes on disk.

    Both the GUI's editor and a text editor write the same file, so they take
    exactly the same path back into the running app.
    """

    def __init__(self, path: Path):
        self.path = path
        self.config = Config()
        self.problems: list[str] = []
        self._mtime: float | None = None

    def load(self) -> Config:
        try:
            self.config = load(self.path)
            self.problems = self.config.validate()
        except FileNotFoundError:
            self.config = Config()
            self.problems = [f"{self.path} not found; using built-in defaults"]
        except (json.JSONDecodeError, ValueError, TypeError) as exc:
            # Keep whatever was loaded before rather than dying mid-session.
            self.problems = [f"{self.path} could not be parsed: {exc}"]
        self._mtime = self._current_mtime()
        return self.config

    def mark_saved(self) -> None:
        """Accept the file on disk as current, without re-reading it.

        Called after the GUI writes the config it is already holding. Reading
        it back would produce an equal-but-distinct object graph, and anything
        still referencing a sub-object of the old one — a Profile held by the
        Profiles tab, a CueConfig captured when the Audio tab was built —
        would go on mutating an orphan that no later save ever writes. Every
        such edit is then silently lost, which looks exactly like "it doesn't
        persist".

        Only the mtime needs updating, so that this write is not mistaken for
        an external edit on the worker's next trigger.
        """
        self.problems = self.config.validate()
        self._mtime = self._current_mtime()

    def maybe_reload(self) -> bool:
        """Reload if the file changed. Returns True when it did."""
        mtime = self._current_mtime()
        if mtime is not None and mtime != self._mtime:
            self.load()
            return True
        return False

    def _current_mtime(self) -> float | None:
        try:
            return self.path.stat().st_mtime
        except OSError:
            return None

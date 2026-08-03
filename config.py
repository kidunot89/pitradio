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

import keys

CONFIG_VERSION = 1

RACING_VOCABULARY = (
    "Le Mans, Hyperpole, Hypercar, LMP2, LMDh, GT3, Porsche 963, Ferrari 499P, "
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


@dataclass
class JoystickConfig:
    """A wheel or gamepad button as an alternative trigger.

    Works alongside the keyboard trigger rather than replacing it: either fires
    the same cycle. Buttons are 1-based, matching how wheels label them.
    """

    device: Any = None      # joystick id, or null for "not bound"
    button: Any = None      # 1-based button number


@dataclass
class AudioConfig:
    input_device: Any = None          # index, substring of the name, or null
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


@dataclass
class CueConfig:
    enabled: bool = True
    output_device: Any = None
    start_hz: int = 880
    stop_hz: int = 620
    duration_ms: int = 60
    volume: float = 0.25


@dataclass
class GuiConfig:
    start_minimized: bool = False
    geometry: str = ""
    log_lines: int = 400


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
    joystick: JoystickConfig = field(default_factory=JoystickConfig)
    audio: AudioConfig = field(default_factory=AudioConfig)
    whisper: WhisperConfig = field(default_factory=WhisperConfig)
    cues: CueConfig = field(default_factory=CueConfig)
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

        cfg.joystick = _section(JoystickConfig, data.get("joystick"))
        cfg.audio = _section(AudioConfig, data.get("audio"))
        cfg.whisper = _section(WhisperConfig, data.get("whisper"))
        cfg.cues = _section(CueConfig, data.get("cues"))
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
            "joystick": asdict(self.joystick),
            "audio": asdict(self.audio),
            "whisper": asdict(self.whisper),
            "cues": asdict(self.cues),
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

    # -- validation ------------------------------------------------------

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

        joystick_bound = self.joystick.device is not None or self.joystick.button is not None
        if joystick_bound:
            if not isinstance(self.joystick.device, int) or self.joystick.device < 0:
                problems.append("joystick.device must be a device number, or null")
            # 1-based to match the numbering printed on wheels; the legacy API
            # this uses exposes at most 32.
            if not isinstance(self.joystick.button, int) or not 1 <= self.joystick.button <= 32:
                problems.append("joystick.button must be between 1 and 32, or null")

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

        if self.whisper.device != "cpu":
            problems.append(
                f"whisper.device is {self.whisper.device!r}; this app is "
                f"deliberately CPU-only so the GPU stays with the sim"
            )
        if self.whisper.beam_size < 1:
            problems.append("whisper.beam_size must be >= 1")

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


def _section(cls, raw: Any):
    """Build a dataclass from a dict, ignoring unknown keys and filling gaps."""
    known = {f.name for f in fields(cls)}
    values = {k: v for k, v in (raw or {}).items() if k in known}
    return cls(**values)


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

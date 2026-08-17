"""Audio capture, transcription, and the record/stop cue beeps.

sounddevice and faster_whisper are imported lazily so this module stays
importable for `--check-config` on a machine that has neither — `sanitize` in
particular is pure logic worth being able to exercise anywhere.

Capture is 16 kHz mono float32, which is exactly what Whisper wants, so nothing
here resamples.
"""

from __future__ import annotations

import logging
import re
import threading
import time
from collections.abc import Callable
from typing import Any

import numpy as np

log = logging.getLogger(__name__)

_WHITESPACE = re.compile(r"\s+")


def sanitize(text: str, max_chars: int) -> str:
    """Collapse a transcription into a single chat-safe line.

    Newlines matter here: a stray one would submit the message halfway through
    typing it, because in most sims Enter is the send key.
    """
    cleaned = _WHITESPACE.sub(" ", (text or "").replace("\r", " ").replace("\n", " "))
    cleaned = cleaned.strip()
    if max_chars > 0 and len(cleaned) > max_chars:
        cleaned = cleaned[:max_chars].rstrip()
    return cleaned


# -- devices -------------------------------------------------------------


def _sd():
    import sounddevice

    return sounddevice


def device_label(index: int, name: str, channels: int, api: str = "") -> str:
    """How one device is named in a picker.

    **The index leads, and that is the point.** Windows lists the same physical
    device once per host API — MME, DirectSound, WASAPI, WDM-KS — under the
    same name, so a machine with one headset shows four identical rows:

        Speakers (PRO X 2 LIGHTSPEED) (2ch)
        Speakers (PRO X 2 LIGHTSPEED) (2ch)
        ...

    A picker that maps the chosen *text* back to a device then resolves every
    one of them to the first, so choosing the third silently selects the first
    and the sound comes out somewhere the user did not pick. That is not a
    hypothetical: it is what sent the engineer into a Steam virtual microphone
    on the machine this was written on, with everything looking correct.
    """
    detail = f"{channels}ch, {api}" if api else f"{channels}ch"
    return f"[{index}] {name} ({detail})"


def list_devices(kind: str = "input") -> list[tuple[int, str]]:
    """(index, label) for every device with channels of the requested kind.

    Labels are unique, because a caller has to be able to get back from one to
    the device the user actually pointed at. See `device_label`.
    """
    try:
        sd = _sd()
        field = "max_input_channels" if kind == "input" else "max_output_channels"
        apis = {}
        try:
            apis = {index: api["name"]
                    for index, api in enumerate(sd.query_hostapis())}
        except Exception:
            # Costs the host API in the label, not the label itself.
            log.debug("could not enumerate host APIs", exc_info=True)
        return [
            (index, device_label(index, info["name"], info[field],
                                 apis.get(info.get("hostapi"), "")))
            for index, info in enumerate(sd.query_devices())
            if info[field] > 0
        ]
    except Exception as exc:
        log.error("could not enumerate %s devices: %s", kind, exc)
        return []


def device_name(index: int, kind: str = "output") -> str:
    """The bare name of a device, for storing a choice by.

    **Names are stored, not indices.** Windows renumbers audio devices whenever
    the set of them changes — a headset powering off, Steam starting, an HDMI
    display waking — so an index saved on Tuesday points at a different device
    on Wednesday. Nothing errors: the sound simply comes out somewhere else,
    which is indistinguishable from the feature being broken. That is exactly
    how the engineer ended up talking into a Steam virtual microphone.
    """
    try:
        info = _sd().query_devices(index)
        return str(info["name"])
    except Exception:
        log.debug("could not name device %r", index, exc_info=True)
        return ""


#: Which Windows sound API to prefer when a device appears under several.
#:
#: Every endpoint is listed once per host API, and they are not equivalent.
#: **MME is the legacy WaveOut path**: when a game holds the endpoint in WASAPI
#: exclusive mode, writes to the MME view succeed and go nowhere — no error, no
#: sound, and nothing in any log. WASAPI shared mode is built to mix with other
#: applications, which is exactly the situation this app is always in: it talks
#: over a running game.
#:
#: Ordered best first. Anything unlisted sorts last but is still usable.
_HOST_API_PREFERENCE = ("Windows WASAPI", "Windows DirectSound", "MME",
                        "Windows WDM-KS")


def _api_rank(index: int, kind: str) -> int:
    try:
        sd = _sd()
        info = sd.query_devices(index)
        name = sd.query_hostapis()[info["hostapi"]]["name"]
    except Exception:
        return len(_HOST_API_PREFERENCE)
    try:
        return _HOST_API_PREFERENCE.index(name)
    except ValueError:
        return len(_HOST_API_PREFERENCE)


def resolve_device(spec: Any, kind: str = "input") -> Any:
    """A device index for a stored choice, or None for the system default.

    Accepts a name (what is stored now), an index (what older configs hold),
    or None. A name that no longer matches anything falls back to the default
    and **says so**, because a device that has been unplugged is the other way
    this goes quiet without explanation.
    """
    if spec is None or spec == "":
        return None

    devices = list_devices(kind)
    if isinstance(spec, int) and not isinstance(spec, bool):
        # An index from a config written before names were stored. Honoured,
        # but it means whatever it means today.
        return spec

    needle = str(spec).strip().lower()
    exact = [index for index, _label in devices
             if device_name(index, kind).strip().lower() == needle]
    if exact:
        # The same headset appears under every host API. Which one is picked
        # decides whether the engineer can be heard over a game — see
        # _HOST_API_PREFERENCE.
        return min(exact, key=lambda index: _api_rank(index, kind))

    loose = [index for index, label in devices if needle in label.lower()]
    if loose:
        return min(loose, key=lambda index: _api_rank(index, kind))

    log.warning("no %s device named %r any more; using the system default. "
                "Available: %s", kind, spec,
                ", ".join(label for _i, label in devices) or "(none)")
    return None


def device_samplerate(index: Any) -> int:
    """What rate a device is actually running at, or 0 if it will not say."""
    try:
        info = _sd().query_devices(index if index is not None else None, "output")
        return int(float(info["default_samplerate"]))
    except Exception:
        log.debug("could not read the rate of device %r", index, exc_info=True)
        return 0


def resample(audio: np.ndarray, source: int, target: int) -> np.ndarray:
    """Linear resampling between two rates.

    Crude and entirely adequate for speech: the alternative is a resampling
    dependency for a difference nobody can hear over an engine.
    """
    if source <= 0 or target <= 0 or source == target or audio.size == 0:
        return audio
    count = round(audio.size * target / source)
    if count <= 0:
        return np.zeros(0, dtype=np.float32)
    position = np.linspace(0.0, audio.size - 1, count, dtype=np.float64)
    return np.interp(position, np.arange(audio.size), audio).astype(np.float32)


# -- capture -------------------------------------------------------------


class Recorder:
    """Buffers microphone audio between key-down and key-up."""

    def __init__(self, on_level: Callable[[float], None] | None = None):
        self._stream = None
        self._blocks: list[np.ndarray] = []
        self._lock = threading.Lock()
        self._samplerate = 16000
        self._max_samples = 16000 * 30
        self._gain = 1.0
        self._on_level = on_level

    @property
    def active(self) -> bool:
        return self._stream is not None

    def start(self, audio_cfg) -> None:
        if self._stream is not None:
            self.stop()

        self._samplerate = audio_cfg.samplerate
        self._max_samples = int(audio_cfg.samplerate * audio_cfg.max_clip_seconds)
        self._gain = float(getattr(audio_cfg, "gain", 1.0) or 1.0)
        with self._lock:
            self._blocks = []

        sd = _sd()
        self._stream = sd.InputStream(
            samplerate=audio_cfg.samplerate,
            channels=audio_cfg.channels,
            dtype="float32",
            device=resolve_device(audio_cfg.input_device, "input"),
            callback=self._callback,
        )
        self._stream.start()

    def _callback(self, indata, frames, time_info, status) -> None:
        if status:
            log.debug("audio status: %s", status)
        block = indata.copy().reshape(-1)
        if self._gain != 1.0:
            # Clipped, not just scaled: a gain that pushes past full scale would
            # otherwise wrap and turn loud speech into noise.
            block = np.clip(block * self._gain, -1.0, 1.0)
        with self._lock:
            self._blocks.append(block)
        if self._on_level is not None:
            self._on_level(float(np.sqrt(np.mean(block * block))) if block.size else 0.0)

    def stop(self) -> np.ndarray:
        stream, self._stream = self._stream, None
        if stream is not None:
            try:
                stream.stop()
                stream.close()
            except Exception as exc:
                log.error("closing the input stream failed: %s", exc)

        with self._lock:
            blocks, self._blocks = self._blocks, []

        if not blocks:
            return np.zeros(0, dtype=np.float32)

        audio = np.concatenate(blocks).astype(np.float32)
        if audio.size > self._max_samples:
            log.info(
                "clip hit max_clip_seconds; keeping the first %.1fs",
                self._max_samples / self._samplerate,
            )
            audio = audio[: self._max_samples]
        return audio

    def duration(self, audio: np.ndarray) -> float:
        return audio.size / float(self._samplerate) if self._samplerate else 0.0


# -- cues ----------------------------------------------------------------


def play_cue(cue_cfg, frequency: int, device: Any = None) -> None:
    """Short sine beep, fire-and-forget.

    Played on the same tick recording starts, so a faint tone can land at the
    head of the clip. Whisper's VAD discards it; anyone bothered can point cues
    at a different output device or turn them off.
    """
    if not cue_cfg.enabled:
        return
    try:
        sd = _sd()
        rate = 44100
        samples = int(rate * cue_cfg.duration_ms / 1000)
        t = np.linspace(0.0, cue_cfg.duration_ms / 1000, samples, endpoint=False)
        wave = (np.sin(2 * np.pi * frequency * t) * cue_cfg.volume).astype(np.float32)
        # Fade the edges, otherwise the discontinuity clicks louder than the tone.
        fade = max(1, samples // 20)
        wave[:fade] *= np.linspace(0.0, 1.0, fade)
        wave[-fade:] *= np.linspace(1.0, 0.0, fade)
        sd.play(wave, rate, device=resolve_device(device, "output"))
    except Exception as exc:
        log.debug("cue playback failed: %s", exc)


# -- voice clips ---------------------------------------------------------
#
# The wire carries 16-bit PCM: half the size of float32 for audio that was
# captured from a microphone and is going to a headset, and the one encoding
# every sound API on earth accepts without negotiation. Compression would be a
# native dependency, and this build already fights those.


def to_pcm16(audio: np.ndarray) -> bytes:
    """float32 in -1..1 to little-endian 16-bit samples.

    Clipped before scaling. Without that a loud passage wraps around on the
    cast and arrives as a burst of noise rather than a loud voice — quiet
    corruption of exactly the clips somebody most wanted heard.
    """
    if audio is None or audio.size == 0:
        return b""
    clipped = np.clip(np.asarray(audio, dtype=np.float32).ravel(), -1.0, 1.0)
    return (clipped * 32767.0).astype("<i2").tobytes()


def from_pcm16(payload: bytes) -> np.ndarray:
    """The inverse. An odd trailing byte is dropped rather than raising.

    This runs on audio from somebody else's machine: a truncated clip must cost
    its last sample, not the playback thread.
    """
    if not payload:
        return np.zeros(0, dtype=np.float32)
    usable = len(payload) - (len(payload) % 2)
    samples = np.frombuffer(payload[:usable], dtype="<i2")
    return (samples.astype(np.float32) / 32767.0).copy()


def play_clip(audio: np.ndarray, rate: int, volume: float = 1.0,
              device: Any = None) -> None:
    """Play a clip. Blocking, so callers give it its own thread.

    Blocking on purpose: two clips played at once are unintelligible, and
    queueing them is what makes a radio a radio. The caller owning the queue is
    what keeps that decision out of here.

    The device is passed in rather than read off a feature's config, because
    there is one output device for the whole app — see `AudioConfig` for why
    that stopped being per-feature.
    """
    if audio is None or audio.size == 0:
        return
    try:
        sd = _sd()
        level = min(1.0, max(0.0, float(volume)))
        index = resolve_device(device, "output")
        rate = int(rate) or 16000

        # **Resampled to whatever the device actually runs at.** WASAPI shared
        # mode accepts only the endpoint's configured rate and refuses anything
        # else outright; MME accepts any rate and resamples it itself, which is
        # how a mismatch stayed invisible until a game was holding the device.
        # Doing it here means neither host API has to.
        native = device_samplerate(index)
        if native and native != rate:
            audio = resample(audio, rate, native)
            rate = native

        sd.play((audio * level).astype(np.float32), rate, device=index)
        sd.wait()
    except Exception as exc:
        # A missing or busy output device must not end the playback thread;
        # the next clip may well work. **Warned, not debugged**: this is the
        # only signal that the engineer is being played into a device nobody
        # can hear, and at debug level it was invisible.
        log.warning("could not play on device %r: %s", device, exc)


def stop_playback() -> None:
    """Cut whatever is playing, now.

    For one case only: a warning that arrives while something less urgent is
    still talking. A spotter call is about a car that is beside you *at this
    moment*, and waiting politely for a lap time to finish reading is how it
    arrives after the corner — which is worse than not making it, because the
    driver acts on it late.
    """
    try:
        _sd().stop()
    except Exception as exc:
        log.debug("could not stop playback: %s", exc)


# -- transcription -------------------------------------------------------


def download_model(model: str, model_dir, compute_type: str = "int8") -> str | None:
    """Fetch a model into the cache. Returns an error message, or None on success.

    Constructing a WhisperModel is what triggers the download, and it is also
    the only way to know the model actually loads — a downloaded-but-unusable
    model would otherwise only surface on the first trigger, mid-session.
    """
    from faster_whisper import WhisperModel

    started = time.perf_counter()
    try:
        WhisperModel(model, device="cpu", compute_type=compute_type,
                     download_root=str(model_dir))
    except Exception as exc:
        log.error("could not fetch %s: %s", model, exc)
        return f"{type(exc).__name__}: {exc}"

    log.info("%s ready (%.1fs)", model, time.perf_counter() - started)
    return None


def _join_prompt(base: str, extra: str) -> str | None:
    """Combine the configured vocabulary with session-specific names.

    Names go first: initial_prompt is truncated around 224 tokens, and a driver
    list is worth more than the tail of a generic racing glossary.
    """
    parts = [p.strip() for p in (extra, base) if p and p.strip()]
    return ". ".join(parts) if parts else None


class Transcriber:
    """Wraps faster-whisper, holding one loaded model for the session."""

    def __init__(self, model_dir):
        self._model = None
        self._signature: tuple | None = None
        self._model_dir = str(model_dir)
        self._lock = threading.Lock()

    @staticmethod
    def _signature_for(whisper_cfg) -> tuple:
        # Only settings that require rebuilding the model. initial_prompt and
        # beam_size are per-call arguments and deliberately excluded.
        return (
            whisper_cfg.model,
            whisper_cfg.device,
            whisper_cfg.compute_type,
            whisper_cfg.cpu_threads,
        )

    def needs_reload(self, whisper_cfg) -> bool:
        return self._signature != self._signature_for(whisper_cfg)

    def load(self, whisper_cfg) -> None:
        """Load or reload the model. Blocks; the first run downloads ~250MB."""
        from faster_whisper import WhisperModel

        signature = self._signature_for(whisper_cfg)
        started = time.perf_counter()
        with self._lock:
            self._model = WhisperModel(
                whisper_cfg.model,
                device=whisper_cfg.device,
                compute_type=whisper_cfg.compute_type,
                cpu_threads=whisper_cfg.cpu_threads,
                download_root=self._model_dir,
            )
            self._signature = signature
        log.info(
            "whisper %s (%s/%s) ready in %.1fs",
            whisper_cfg.model,
            whisper_cfg.device,
            whisper_cfg.compute_type,
            time.perf_counter() - started,
        )

    def transcribe(self, audio: np.ndarray, whisper_cfg, extra_prompt: str = "") -> str:
        if self._model is None:
            self.load(whisper_cfg)

        with self._lock:
            segments, _info = self._model.transcribe(
                audio,
                language=whisper_cfg.language or None,
                beam_size=whisper_cfg.beam_size,
                vad_filter=whisper_cfg.vad_filter,
                initial_prompt=_join_prompt(whisper_cfg.initial_prompt, extra_prompt),
            )
            # segments is a generator; consuming it is where the work happens.
            return " ".join(segment.text.strip() for segment in segments)

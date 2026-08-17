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


def list_devices(kind: str = "input") -> list[tuple[int, str]]:
    """(index, label) for every device with channels of the requested kind.

    **The host API is part of the label, and has to be.** Windows exposes the
    same physical device once per API — MME, DirectSound, WASAPI, WDM-KS — so a
    single headset appears four times under one name. Without the API in the
    label the picker shows four identical rows, choosing between them is a coin
    toss, and matching one back by name always lands on the first. They are not
    interchangeable either: WASAPI refuses a 16kHz clip on an HDMI output that
    MME plays without complaint.
    """
    try:
        sd = _sd()
        field = "max_input_channels" if kind == "input" else "max_output_channels"
        apis = {index: api["name"] for index, api in enumerate(sd.query_hostapis())}
        return [
            (index,
             f"{info['name']} ({info[field]}ch, {apis.get(info['hostapi'], '?')})")
            for index, info in enumerate(sd.query_devices())
            if info[field] > 0
        ]
    except Exception as exc:
        log.error("could not enumerate %s devices: %s", kind, exc)
        return []


def resolve_device(spec: Any, kind: str = "input") -> Any:
    """Accept an index, a substring of the device name, or None for default."""
    if spec is None or spec == "":
        return None
    if isinstance(spec, int):
        return spec
    needle = str(spec).lower()
    for index, label in list_devices(kind):
        if needle in label.lower():
            return index
    log.warning("no %s device matching %r; falling back to the default", kind, spec)
    return None


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


def play_cue(cue_cfg, frequency: int) -> None:
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
        sd.play(wave, rate, device=resolve_device(cue_cfg.output_device, "output"))
    except Exception as exc:
        log.debug("cue playback failed: %s", exc)


# -- voice clips ---------------------------------------------------------
#
# The wire carries 16-bit PCM: half the size of float32 for audio that was
# captured from a microphone and is going to a headset, and the one encoding
# every sound API on earth accepts without negotiation. Compression would be a
# native dependency, and this build already fights those.


#: What a normalised clip should peak at. Short of 1.0 so that the loudest
#: syllable has somewhere to go rather than clipping.
VOICE_TARGET_PEAK = 0.7

#: Never amplify by more than this. A clip of near-silence has a peak made of
#: room noise, and normalising that to speech level produces a burst of hiss in
#: somebody's headset — the opposite of the problem being solved.
VOICE_MAX_GAIN = 32.0

#: Below this **RMS** the clip is silence, not quiet speech, and is left alone.
#:
#: RMS rather than peak, because peak cannot tell them apart: room noise
#: routinely peaks above any floor low enough to admit a quiet voice, and
#: normalising on that produces a burst of hiss. Speech carries far more energy
#: for the same peak, so its RMS separates cleanly.
VOICE_NOISE_FLOOR_RMS = 0.001


def normalise_voice(audio: np.ndarray) -> np.ndarray:
    """Bring a clip up to a level a human can hear.

    **Transcription and voice need different things from the same recording,
    and only one of them has ever complained.** Whisper normalises internally,
    so PitRadio has always worked at any capture level; a clip peaking at 0.015
    transcribes perfectly and is inaudible over a headset. Every existing user
    has `audio.gain` at its default for exactly that reason — nothing ever gave
    them a reason to touch it.

    So the clip that goes on the wire is levelled here, and the array handed to
    Whisper is left alone.
    """
    if audio is None or audio.size == 0:
        return audio

    peak = float(np.abs(audio).max())
    rms = float(np.sqrt(np.mean(np.square(audio, dtype=np.float64))))
    if rms < VOICE_NOISE_FLOOR_RMS or peak <= 0.0 or peak >= VOICE_TARGET_PEAK:
        # Silence, or already loud enough. Amplifying the first is hiss; the
        # second only risks clipping.
        return audio

    gain = min(VOICE_TARGET_PEAK / peak, VOICE_MAX_GAIN)
    return audio * gain


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


def _device_samplerate(sd, device) -> int:
    """The rate a device actually wants, or 0 if it will not say."""
    try:
        index = device if device is not None else sd.default.device[1]
        return int(sd.query_devices(index)["default_samplerate"])
    except Exception:
        return 0


def _resample(audio: np.ndarray, source: int, target: int) -> np.ndarray:
    """Linear resampling, which is ample for speech.

    Deliberately numpy rather than a resampler library: this is a fallback for
    an output device being fussy, not an audio pipeline, and adding a native
    dependency to a build that already fights them would be a poor trade for
    quality nobody can hear on a headset.
    """
    if source == target or audio.size == 0:
        return audio
    count = round(audio.size * target / source)
    if count <= 0:
        return audio
    source_points = np.linspace(0.0, 1.0, audio.size, endpoint=False)
    target_points = np.linspace(0.0, 1.0, count, endpoint=False)
    return np.interp(target_points, source_points, audio).astype(np.float32)


def play_clip(audio: np.ndarray, rate: int, voice_cfg) -> None:
    """Play a received clip. Blocking, so callers give it its own thread.

    Blocking on purpose: two clips played at once are unintelligible, and
    queueing them is what makes a radio a radio. The caller owning the queue is
    what keeps that decision out of here.
    """
    if audio is None or audio.size == 0:
        return
    try:
        sd = _sd()
        volume = min(1.0, max(0.0, float(voice_cfg.volume)))
        device = resolve_device(voice_cfg.output_device, "output")
        samples = (audio * volume).astype(np.float32)
        rate = int(rate) or 16000

        try:
            sd.play(samples, rate, device=device)
        except Exception:
            # **HDMI outputs generally accept only 48kHz.** A sim rig sending
            # sound to a TV is the common case, and on WASAPI the device
            # refuses a 16kHz clip outright rather than resampling it. Falling
            # back to the device's own rate is the difference between a driver
            # hearing their team mate and hearing nothing, with the failure
            # looking identical to nobody having spoken.
            native = _device_samplerate(sd, device)
            if not native or native == rate:
                raise
            log.info("voice: device refused %dHz; resampling to %dHz", rate, native)
            sd.play(_resample(samples, rate, native), native, device=device)
        sd.wait()
    except Exception as exc:
        # A missing or busy output device must not end the playback thread;
        # the next clip may well work.
        #
        # **Warning, not debug.** At debug this is invisible, and a device that
        # refuses the clip is then indistinguishable from one playing it into a
        # room nobody is in — which is exactly how an afternoon gets spent
        # changing settings instead of reading one line. HDMI outputs are the
        # common case: they generally accept only 48kHz and reject a 16kHz clip
        # outright on WASAPI.
        log.warning("voice: playback failed on device %r: %s",
                    voice_cfg.output_device or "(system default)", exc)


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

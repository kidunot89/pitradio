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
    """(index, label) for every device with channels of the requested kind."""
    try:
        sd = _sd()
        field = "max_input_channels" if kind == "input" else "max_output_channels"
        return [
            (index, f"{info['name']} ({info[field]}ch)")
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


# -- transcription -------------------------------------------------------


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

    def transcribe(self, audio: np.ndarray, whisper_cfg) -> str:
        if self._model is None:
            self.load(whisper_cfg)

        with self._lock:
            segments, _info = self._model.transcribe(
                audio,
                language=whisper_cfg.language or None,
                beam_size=whisper_cfg.beam_size,
                vad_filter=whisper_cfg.vad_filter,
                initial_prompt=whisper_cfg.initial_prompt or None,
            )
            # segments is a generator; consuming it is where the work happens.
            return " ".join(segment.text.strip() for segment in segments)

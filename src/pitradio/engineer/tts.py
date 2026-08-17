"""The synthesised voice: Windows' own speech engine, driven out of process.

Every voice installed on the machine is available, which is what makes "pick
the voice of the engineer" mean something on a fresh install with no downloads.

**It runs in a PowerShell host rather than through a Python binding**, and that
is a packaging decision more than a technical one. The alternatives are
`pywin32` or `comtypes` — two more native dependencies in a build that has
already shipped four releases broken by a native dependency not being
collected. `System.Speech` is part of the .NET Framework that is on every
Windows 10 and 11 machine, needs nothing installed, and cannot go missing from
a Nuitka build because it was never in it.

**It synthesises to a file rather than speaking out loud.** Speaking directly
would put the engineer on whatever Windows considers the default output, which
during a race is usually the wheel's speaker or the sim's own device. Going via
a WAV means the same output-device setting as voice chat and the cues, and the
same volume control — the engineer belongs in the headset with everything else.

Everything is cached by what was said, so the second "two tenths" costs a file
read. In a stint that is nearly all of them.
"""

from __future__ import annotations

import base64
import json
import logging
import queue
import shutil
import subprocess
import sys
import tempfile
import threading
from dataclasses import dataclass
from pathlib import Path

from pitradio.engineer import packs

log = logging.getLogger(__name__)

#: How long to wait for the host to answer before giving up on it. Synthesising
#: a short phrase is tens of milliseconds; anything near this means the host is
#: wedged, and the right response is to kill it rather than to hold the
#: engineer's queue open indefinitely.
REPLY_TIMEOUT = 15.0

#: Windows' own range. 0 is the voice's natural pace.
MIN_RATE, MAX_RATE = -10, 10

#: And what the engineer uses unless told otherwise.
#:
#: Well above natural, because a synthesiser's default pace is tuned for
#: reading prose to somebody sitting still, and a race call competes with an
#: engine. Measured on "turn four, Tandy was faster on the exit, two tenths":
#: 4.9s at rate 0, 3.6s at 4, 2.7s at 5.
#:
#: Four rather than higher is a judgement about *clarity*, and one nobody here
#: has verified by ear — it is set where the wins are still large and left
#: adjustable on the Engineer tab, because how fast is too fast is a thing the
#: person listening decides.
DEFAULT_RATE = 4

#: Don't let a long session's cache grow without bound. Fragments repeat
#: heavily, so this is far more than a stint ever needs.
CACHE_LIMIT = 400

# Hidden, or every utterance flashes a console window over a full-screen sim.
_NO_WINDOW = 0x08000000


@dataclass(frozen=True)
class Voice:
    """One installed speech voice."""

    name: str
    culture: str = ""
    gender: str = ""

    @property
    def label(self) -> str:
        detail = ", ".join(part for part in (self.gender, self.culture) if part)
        return f"{self.name} ({detail})" if detail else self.name


# The host. Written here rather than shipped as a file so there is nothing for
# packaging to lose, and passed as -EncodedCommand so nothing depends on the
# machine's PowerShell execution policy — a locked-down corporate one would
# otherwise refuse to run it and the engineer would be silently mute.
#
# Base64 on the wire in both directions, so a driver name with an accent in it
# and a Windows user folder called "José" both survive whatever code page the
# console happens to be using. That is not hypothetical: the temp path this is
# told to write to contains the user's name.
_HOST_SCRIPT = r"""
$ErrorActionPreference = 'Stop'
Add-Type -AssemblyName System.Speech
$synth = New-Object System.Speech.Synthesis.SpeechSynthesizer
function Dec([string]$s) {
  if (-not $s) { return '' }
  [Text.Encoding]::UTF8.GetString([Convert]::FromBase64String($s))
}
function Enc([string]$s) {
  [Convert]::ToBase64String([Text.Encoding]::UTF8.GetBytes([string]$s))
}
function Reply([string]$s) { [Console]::Out.WriteLine($s); [Console]::Out.Flush() }
$running = $true
while ($running) {
  $line = [Console]::In.ReadLine()
  if ($null -eq $line) { break }
  $parts = $line.Split(' ')
  try {
    if ($parts[0] -eq 'voices') {
      $list = @($synth.GetInstalledVoices() | Where-Object { $_.Enabled } |
        ForEach-Object {
          $info = $_.VoiceInfo
          [pscustomobject]@{
            name = $info.Name
            culture = [string]$info.Culture.Name
            gender = [string]$info.Gender
          }
        })
      $json = if ($list.Count) { ConvertTo-Json -InputObject $list -Compress } else { '[]' }
      Reply ('ok ' + (Enc $json))
    }
    elseif ($parts[0] -eq 'say') {
      $voice = Dec $parts[1]
      $rate = [int]$parts[2]
      $path = Dec $parts[3]
      $text = Dec $parts[4]
      if ($voice) { $synth.SelectVoice($voice) }
      $synth.Rate = $rate
      $synth.SetOutputToWaveFile($path)
      $synth.Speak($text)
      $synth.SetOutputToNull()
      Reply 'ok'
    }
    elseif ($parts[0] -eq 'quit') { $running = $false }
    else { Reply ('err ' + (Enc 'unknown command')) }
  } catch {
    try { $synth.SetOutputToNull() } catch { }
    Reply ('err ' + (Enc $_.Exception.Message))
  }
}
"""


def _encode(text: str) -> str:
    return base64.b64encode((text or "").encode("utf-8")).decode("ascii")


def _decode(payload: str) -> str:
    try:
        return base64.b64decode(payload.encode("ascii")).decode("utf-8")
    except Exception:
        return ""


def available() -> bool:
    """Whether a synthesised voice can exist at all on this machine."""
    return sys.platform == "win32"


class SapiHost:
    """A PowerShell process that turns text into WAV files, kept alive.

    Kept alive because starting one costs the best part of a second — loading
    the speech assembly, not the process — and an engineer that paused for a
    second before every call would be useless. One host serves the whole
    session and is restarted only if it dies.

    Not thread-safe by construction: it is a request/response pipe, so calls
    are serialised on a lock. In practice only the speaking thread uses it,
    plus the window when somebody presses Test.
    """

    def __init__(self, cache_dir: Path | None = None) -> None:
        self._process: subprocess.Popen | None = None
        self._replies: queue.Queue[str] = queue.Queue()
        self._reader: threading.Thread | None = None
        self._lock = threading.Lock()
        self._owns_cache = cache_dir is None
        self._cache_dir = cache_dir
        #: (voice, rate, text) -> the file it was rendered to.
        self._cache: dict[tuple[str, int, str], Path] = {}
        self._failed = False

    # -- process ---------------------------------------------------------

    def _directory(self) -> Path:
        if self._cache_dir is None:
            self._cache_dir = Path(tempfile.mkdtemp(prefix="pitradio-tts-"))
        self._cache_dir.mkdir(parents=True, exist_ok=True)
        return self._cache_dir

    def _start(self) -> bool:
        if self._process is not None and self._process.poll() is None:
            return True
        if not available():
            return False

        encoded = base64.b64encode(_HOST_SCRIPT.encode("utf-16-le")).decode("ascii")
        try:
            self._process = subprocess.Popen(
                ["powershell", "-NoProfile", "-NonInteractive",
                 "-EncodedCommand", encoded],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                text=True,
                encoding="ascii",
                errors="replace",
                bufsize=1,
                creationflags=_NO_WINDOW,
            )
        except OSError as exc:
            # Said once. A machine without PowerShell is not going to grow one
            # mid-session, and repeating it every call would bury the log.
            if not self._failed:
                self._failed = True
                log.warning("no speech host: %s", exc)
            return False

        self._replies = queue.Queue()
        self._reader = threading.Thread(
            target=self._read, args=(self._process,), name="engineer-tts",
            daemon=True)
        self._reader.start()
        return True

    def _read(self, process: subprocess.Popen) -> None:
        """Pump the host's stdout onto a queue.

        A thread rather than a blocking read at the call site, so a wedged host
        can be given up on. Without it the speaking thread would block forever
        and the engineer would go silent with nothing in the log to say why.
        """
        try:
            for line in process.stdout:
                self._replies.put(line.strip())
        except (ValueError, OSError):
            pass
        finally:
            self._replies.put("")

    def _kill(self) -> None:
        process, self._process = self._process, None
        if process is None:
            return
        try:
            process.kill()
        except Exception:
            log.debug("could not stop the speech host", exc_info=True)

    def close(self) -> None:
        with self._lock:
            process = self._process
            if process is not None and process.poll() is None:
                try:
                    process.stdin.write("quit\n")
                    process.stdin.flush()
                    process.wait(timeout=1.0)
                except Exception:
                    log.debug("the speech host did not quit cleanly", exc_info=True)
            self._kill()
            self._cache.clear()
            if self._owns_cache and self._cache_dir is not None:
                shutil.rmtree(self._cache_dir, ignore_errors=True)
                self._cache_dir = None

    # Named `stop` as well, so it can be shut down alongside everything else.
    stop = close

    # -- protocol --------------------------------------------------------

    def _ask(self, command: str) -> tuple[str, str] | None:
        """One request and its reply, or None if the host could not answer."""
        with self._lock:
            if not self._start():
                return None
            process = self._process
            try:
                process.stdin.write(command + "\n")
                process.stdin.flush()
            except (OSError, ValueError, AttributeError):
                log.info("the speech host went away; restarting it next time")
                self._kill()
                return None

            try:
                reply = self._replies.get(timeout=REPLY_TIMEOUT)
            except queue.Empty:
                log.warning("the speech host stopped answering; restarting it")
                self._kill()
                return None

        if not reply:
            self._kill()
            return None
        status, _, payload = reply.partition(" ")
        return status, payload

    def voices(self) -> list[Voice]:
        """Every enabled voice on the machine, or an empty list.

        Empty is an ordinary answer — off Windows, or on a machine where the
        host will not start — and the caller shows "system default" instead of
        treating it as a failure.
        """
        answer = self._ask("voices")
        if answer is None or answer[0] != "ok":
            return []
        try:
            raw = json.loads(_decode(answer[1]) or "[]")
        except json.JSONDecodeError:
            return []
        if isinstance(raw, dict):
            raw = [raw]
        return [
            Voice(str(entry.get("name", "")), str(entry.get("culture", "")),
                  str(entry.get("gender", "")))
            for entry in raw if isinstance(entry, dict) and entry.get("name")
        ]

    def synthesize(self, text: str, *, voice: str = "", rate: int = 0) -> Path | None:
        """Render a phrase to a WAV, reusing the file if it was said before."""
        words = (text or "").strip()
        if not words:
            return None

        rate = max(MIN_RATE, min(MAX_RATE, int(rate)))
        key = (voice, rate, words)
        cached = self._cache.get(key)
        if cached is not None and cached.exists():
            return cached

        target = self._directory() / f"{packs.slug(words)[:60] or 'phrase'}-{abs(hash(key)):x}.wav"
        answer = self._ask(
            f"say {_encode(voice)} {rate} {_encode(str(target))} {_encode(words)}")
        if answer is None:
            return None
        if answer[0] != "ok":
            log.warning("could not say %r: %s", words, _decode(answer[1]))
            return None
        if not target.exists():
            log.warning("the speech host reported success but wrote no file for %r", words)
            return None

        if len(self._cache) >= CACHE_LIMIT:
            self._cache.clear()
        self._cache[key] = target
        return target


def installed_voices() -> list[Voice]:
    """The machine's voices, for the window's picker.

    Starts a host, asks, and shuts it down again. Called once when the tab is
    built, so the second of startup cost lands somewhere nobody is waiting.
    """
    host = SapiHost()
    try:
        return host.voices()
    finally:
        host.close()

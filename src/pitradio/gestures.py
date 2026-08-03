"""What a trigger press means while a message is waiting to be sent.

With `auto_send` off the message is typed into the chat box and left there. The
driver then needs to say what happens to it without letting go of the wheel, so
the trigger they already have does three jobs:

* **tap** — send it
* **tap twice** — clear it
* **hold** — clear it and record a replacement

Kept separate from `worker.py` and free of any Win32 or audio import, because
it is pure timing logic and timing logic is exactly the kind that fails in ways
nobody notices until they are mid-race. The worker supplies the clock.

The one unavoidable cost: a tap cannot be acted on until the double-tap window
closes, because until then it might be the first half of one. That delay is
`double_tap_ms` and is the reason it defaults low.
"""

from __future__ import annotations

SEND = "send"
CLEAR = "clear"
RETRY = "retry"


class Gestures:
    """Classifies presses of the trigger while a message is pending.

    Timings are in milliseconds at the boundary and seconds inside, matching
    the monotonic clock the worker already passes around.
    """

    def __init__(self, tap_ms: int = 300, double_tap_ms: int = 350):
        self.tap = max(0.0, tap_ms / 1000)
        self.double = max(0.0, double_tap_ms / 1000)
        self._pressed_at: float | None = None
        self._tapped_at: float | None = None

    # -- events ----------------------------------------------------------

    def press(self, now: float) -> None:
        self._pressed_at = now

    def release(self, now: float) -> str | None:
        """CLEAR, RETRY, or None while a lone tap waits out its window."""
        pressed, self._pressed_at = self._pressed_at, None
        if pressed is None:
            return None

        if now - pressed >= self.tap:
            # Long enough to be a deliberate hold, so it is a re-record. Any
            # half-finished tap is abandoned rather than combined with it.
            self._tapped_at = None
            return RETRY

        if self._tapped_at is not None and now - self._tapped_at <= self.double:
            self._tapped_at = None
            return CLEAR

        self._tapped_at = now
        return None

    def elapsed(self, now: float) -> str | None:
        """SEND once a lone tap's double-tap window has closed."""
        if self._tapped_at is not None and now - self._tapped_at >= self.double:
            self._tapped_at = None
            return SEND
        return None

    # -- scheduling ------------------------------------------------------

    def deadline(self, now: float) -> float | None:
        """Seconds until `elapsed` has something to say, or None if never.

        The worker blocks on its queue for this long instead of forever, which
        is what turns a tap that is never followed by a second one into a send.
        """
        if self._tapped_at is None:
            return None
        return max(0.0, self._tapped_at + self.double - now)

    def reset(self) -> None:
        self._pressed_at = None
        self._tapped_at = None

    @property
    def waiting(self) -> bool:
        """Whether a tap is being held back to see if it becomes a double."""
        return self._tapped_at is not None

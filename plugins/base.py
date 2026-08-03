"""What a sim plugin provides.

Some things PitRadio can do are specific to one sim, because they need that
sim's own data interface. Reading who is in the session is the first: Le Mans
Ultimate exposes it in shared memory, and nothing about that generalises.

A plugin answers "what is happening in this session" for the executables it
claims. Everything else — the hook, injection, transcription — stays sim-
agnostic and never learns which plugin, if any, is active.

Plugins must degrade quietly. The sim not running, the interface being absent,
the layout having changed after a game update: all of these are normal, and
none of them may raise into the trigger cycle. Returning nothing is always a
valid answer.
"""

from __future__ import annotations

import logging

log = logging.getLogger(__name__)


class SessionPlugin:
    """Base for a per-sim data source. Subclasses override what they can."""

    #: Stable identifier stored in profiles. Never rename one in place —
    #: existing configs reference it.
    id: str = ""
    #: Shown in the GUI.
    name: str = "unnamed"
    #: Executables this plugin is assumed to suit, used only to pick a default
    #: for a profile that has not chosen. The profile is always authoritative,
    #: so one plugin can serve as many games as work with it.
    executables: tuple[str, ...] = ()
    #: One line explaining what it adds, for the plugin list.
    description: str = ""

    def serves(self, executable: str | None) -> bool:
        return (executable or "").strip().lower() in self.executables

    # -- lifecycle -------------------------------------------------------

    def start(self) -> None:
        """Attach to the sim. Called once; must not raise."""

    def stop(self) -> None:
        """Detach. Called on shutdown; must not raise."""

    # -- data ------------------------------------------------------------

    def is_connected(self) -> bool:
        """Whether the sim's data is readable right now."""
        return False

    def drivers(self) -> list[str]:
        """Driver names in the current session, or an empty list.

        Empty is not an error: the sim may be closed, in a menu, or the
        interface may have moved. Callers must treat it as "no information".
        """
        return []

    def status(self) -> str:
        """A line for the GUI's plugin list — connected, idle, or why not."""
        return "not connected"

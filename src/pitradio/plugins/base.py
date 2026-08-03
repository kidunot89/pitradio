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
from dataclasses import dataclass
from typing import Any

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class PluginSetting:
    """One option a plugin exposes, rendered in the profile editor.

    Settings are stored per profile rather than per plugin: a plugin can serve
    several games, and what makes sense in one may not in another.
    """

    key: str
    label: str
    kind: str = "bool"          # bool | int | text
    default: Any = False
    help: str = ""


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
    #: Options shown in the profile editor when this plugin is assigned.
    settings: tuple[PluginSetting, ...] = ()

    def defaults(self) -> dict[str, Any]:
        return {setting.key: setting.default for setting in self.settings}

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

    def vocabulary(self) -> list[str]:
        """Terms to feed Whisper's initial_prompt for this session.

        Separate from drivers() because the two answer different questions.
        Mentions need *names* to match against; the prompt just needs words
        Whisper would otherwise have no reason to expect — which for another
        sim might be car names, teams, tracks or commentators rather than
        people. Defaults to the driver list, which is the common case.
        """
        return self.drivers()

    def positions(self) -> dict[int, str]:
        """Standings position -> driver name, or empty.

        Lets someone say "P3" instead of a name they cannot pronounce or did
        not catch — which on a full grid is most of them.
        """
        return {}

    def status(self) -> str:
        """A line for the GUI's plugin list — connected, idle, or why not."""
        return "not connected"

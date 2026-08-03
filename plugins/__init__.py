"""Sim plugins, and the registry that picks one for the focused game.

**Plugins are registered statically, not discovered.** Nuitka cannot follow an
import it never sees, so a build that scanned a directory at runtime would ship
with no plugins at all and give no indication why — the same class of failure
that has cost this project several releases. Adding a sim means one module and
one line in BUILTIN, which is a small price for the build being predictable.

Dropping user-supplied plugin files into a folder would need separate machinery
to work in a frozen build; it is deliberately not attempted here.
"""

from __future__ import annotations

import logging

from plugins.base import SessionPlugin
from plugins.lmu import LeMansUltimatePlugin

log = logging.getLogger(__name__)

#: Every plugin that ships. Add new sims here.
BUILTIN: tuple[type[SessionPlugin], ...] = (LeMansUltimatePlugin,)


class PluginRegistry:
    """Holds one instance of each plugin and matches them to executables."""

    def __init__(self, plugin_classes=BUILTIN):
        self.plugins: list[SessionPlugin] = []
        for cls in plugin_classes:
            try:
                self.plugins.append(cls())
            except Exception:
                # A plugin that cannot even be constructed must not stop the
                # app; it just does not exist as far as everything else knows.
                log.exception("could not create plugin %s", getattr(cls, "name", cls))

    def start_all(self) -> None:
        for plugin in self.plugins:
            try:
                plugin.start()
            except Exception:
                log.exception("plugin %s failed to start", plugin.name)

    def stop_all(self) -> None:
        for plugin in self.plugins:
            try:
                plugin.stop()
            except Exception:
                log.exception("plugin %s failed to stop", plugin.name)

    # Named `stop` too, so the GUI can shut it down alongside the other
    # components without a special case.
    stop = stop_all

    def by_id(self, plugin_id: str | None) -> SessionPlugin | None:
        if not plugin_id:
            return None
        for plugin in self.plugins:
            if plugin.id == plugin_id:
                return plugin
        return None

    def for_executable(self, executable: str | None) -> SessionPlugin | None:
        """The plugin whose defaults suit this executable, if any.

        Only used to pre-fill a profile that has not chosen one. Resolution at
        trigger time goes through the profile, so a plugin that works for two
        games can be assigned to both.
        """
        for plugin in self.plugins:
            if plugin.serves(executable):
                return plugin
        return None

    def default_id_for(self, executable: str | None) -> str:
        plugin = self.for_executable(executable)
        return plugin.id if plugin else ""

    def choices(self) -> list[tuple[str, str]]:
        """(id, name) for the profile editor's plugin picker."""
        return [("", "(none)")] + [(p.id, p.name) for p in self.plugins]

    def drivers_for(self, plugin_id: str | None) -> list[str]:
        """Driver names from the profile's plugin, or an empty list.

        Swallows everything: this is called from the trigger cycle, and a
        plugin fault must cost the driver list, never the message.
        """
        plugin = self.by_id(plugin_id)
        if plugin is None:
            return []
        try:
            return plugin.drivers()
        except Exception:
            log.exception("plugin %s failed while reading drivers", plugin.name)
            return []

    def describe(self) -> list[tuple[str, str]]:
        """(name, status) for the GUI's plugin list."""
        rows = []
        for plugin in self.plugins:
            try:
                rows.append((plugin.name, plugin.status()))
            except Exception as exc:
                rows.append((plugin.name, f"error: {type(exc).__name__}: {exc}"))
        return rows


__all__ = ["BUILTIN", "LeMansUltimatePlugin", "PluginRegistry", "SessionPlugin"]

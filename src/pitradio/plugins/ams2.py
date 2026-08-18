"""Automobilista 2, in its own right.

Technically this is the Project CARS 2 plugin with a different name on it —
Automobilista 2 is built on the same engine and publishes the same `$pcars2$`
block, so everything it does is inherited and nothing is reimplemented.

**It exists as a separate plugin anyway**, for three reasons that are worth
more than the duplication would cost:

* A profile picks a plugin by name, and somebody running Automobilista 2 should
  be able to choose "Automobilista 2". Being told to pick "Project CARS 2 / 3"
  for a different game reads as a mistake in the app.
* Plugin settings are stored per profile against the plugin's id, so the two
  games get their own spotter geometry and proximity ranges rather than sharing
  one set. Reiza's grids and Slightly Mad's are not the same cars.
* When Automobilista 2 diverges — and it has been diverging from the Project
  CARS API steadily — the override has somewhere to go that does not touch the
  game it forked from.

There is no behaviour here to test beyond that identity, which is what
`tests/test_pcars2.py` checks: that it is a distinct plugin, that it points at
its own executables, and that it inherits the reading rather than copying it.
"""

from __future__ import annotations

from pitradio.plugins.projectcars2 import ProjectCars2Plugin


class Automobilista2Plugin(ProjectCars2Plugin):
    """Reiza's, on Slightly Mad's shared memory."""

    id = "ams2"
    name = "Automobilista 2"
    executables = ("ams2.exe", "ams2avx.exe")
    description = (
        "Reads the driver list, positions and lap distance from Automobilista "
        "2, which publishes the Project CARS 2 shared memory."
    )

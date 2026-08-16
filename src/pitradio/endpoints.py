"""Where this build talks to, filled in when it is built.

PitRadio is a public repository and the relay is not. The base relay and voice
host are supplied at **build time** — CI writes over this module from a secret —
so the addresses never sit in the tree, in the history, or in a fork.

The committed values are therefore **empty on purpose**, and that is a working
state rather than a broken one: a source checkout has no relay, so voice is
unavailable and says so, instead of pointing somebody's microphone at a server
they have never heard of.

Nothing else in the app may hardcode an address. Read it from here, so there is
exactly one thing to overwrite and exactly one place to look when it is wrong.
"""

from __future__ import annotations

#: Base URL of the relay this build defaults to, e.g. "wss://relay.example.com".
#: Empty means this build ships without one; the user can still set their own in
#: Settings > Voice, which is also how a racer points at a host they run.
RELAY = ""


def default_relay() -> str:
    """The relay a fresh config should start with. Empty when unbuilt."""
    return RELAY.strip()


def has_relay() -> bool:
    return bool(default_relay())

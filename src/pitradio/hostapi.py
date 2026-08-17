"""Asking the relay to make, repair, stop or remove a voice host.

The app does none of the work and holds none of the credentials. It has no
DigitalOcean token, no Terraform and no SSH key, because PitRadio self-elevates
to administrator on Windows and that is the last place to keep something which
can spend a stranger's money. It asks; the relay does it.

`urllib` rather than a client library: this is six requests, and the build
already fights the dependencies it has.

**Nothing here raises.** It is called from the window, and a relay that is down
must grey out four buttons — not close somebody's settings.
"""

from __future__ import annotations

import json
import logging
import urllib.error
import urllib.request
from dataclasses import dataclass, field

log = logging.getLogger(__name__)

#: Long enough for a relay under load, short enough that the window does not
#: appear to hang. Provisioning itself is not waited on — it answers 202 and
#: the state is polled.
TIMEOUT = 10.0

#: How often to ask again while something is happening.
POLL_SECONDS = 3.0


@dataclass
class Reply:
    """What came back. `error` is empty when it worked."""

    ok: bool = False
    status: int = 0
    body: dict = field(default_factory=dict)
    error: str = ""


def _http(url: str, *, method: str = "GET", token: str = "",
          payload: dict | None = None, opener=None) -> Reply:
    """One request, with every failure turned into a Reply.

    `opener` is injectable so the flow can be tested without a relay.
    """
    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    headers = {"Accept": "application/json"}
    if data is not None:
        headers["Content-Type"] = "application/json"
    if token:
        headers["Authorization"] = f"Bearer {token}"

    request = urllib.request.Request(url, data=data, headers=headers, method=method)
    send = opener or urllib.request.urlopen
    try:
        with send(request, timeout=TIMEOUT) as response:
            return Reply(True, response.status, _json(response.read()))
    except urllib.error.HTTPError as exc:
        body = _json(exc.read())
        # The relay's own message when it has one: "you already have a host"
        # is worth showing, and it never carries anything upstream.
        return Reply(False, exc.code, body,
                     body.get("error") or f"the relay returned {exc.code}")
    except Exception as exc:
        log.debug("host api %s %s failed", method, url, exc_info=True)
        return Reply(False, 0, {}, f"could not reach the relay ({type(exc).__name__})")


def _json(raw: bytes) -> dict:
    try:
        value = json.loads(raw or b"{}")
    except ValueError:
        return {}
    return value if isinstance(value, dict) else {}


class HostApi:
    """The relay's host endpoints, for one install."""

    def __init__(self, base_url: str, token: str = "", *, opener=None) -> None:
        # The base is a wss:// relay URL; these are ordinary HTTPS requests to
        # the same host.
        self.base = (base_url or "").strip().rstrip("/") \
            .replace("wss://", "https://").replace("ws://", "http://")
        self.token = token or ""
        self._opener = opener

    @property
    def configured(self) -> bool:
        return bool(self.base)

    # -- pairing ---------------------------------------------------------

    def start_pairing(self) -> Reply:
        return _http(f"{self.base}/pair/start", method="POST", payload={},
                     opener=self._opener)

    def pairing_status(self, poll_token: str) -> Reply:
        return _http(f"{self.base}/pair/status/{poll_token}", opener=self._opener)

    # -- hosts -----------------------------------------------------------

    def hosts(self) -> Reply:
        return _http(f"{self.base}/hosts", token=self.token, opener=self._opener)

    def create(self, name: str, region: str) -> Reply:
        return _http(f"{self.base}/hosts", method="POST", token=self.token,
                     payload={"name": name, "region": region}, opener=self._opener)

    def act(self, host_id: str, action: str) -> Reply:
        return _http(f"{self.base}/hosts/{host_id}/{action}", method="POST",
                     token=self.token, opener=self._opener)

    def destroy(self, host_id: str) -> Reply:
        return _http(f"{self.base}/hosts/{host_id}", method="DELETE",
                     token=self.token, opener=self._opener)

    def health(self) -> Reply:
        """Whether the relay is answering at all — for the base host's status."""
        return _http(f"{self.base}/health", opener=self._opener)


#: How long to wait for the browser half of pairing. Shorter than the relay's
#: own expiry, so the window gives up *before* the thing it is asking about
#: stops existing — otherwise the last minutes are spent polling something
#: already gone, and the message would be wrong for all of them.
PAIRING_WAIT_SECONDS = 300.0

LINKED = "linked"
FAILED = "failed"
EXPIRED = "expired"
PENDING = "pending"


def pairing_outcome(reply: Reply) -> str:
    """What a poll of a pairing means.

    Named and separated because getting it wrong is invisible: a window that
    treats "gone" as "still waiting" sits there with every button disabled and
    nothing to say, which is what somebody sees after closing the browser tab
    — by far the most common way a login ends without finishing.

    A relay that cannot be reached is `pending`, not failed: a moment of
    network trouble in the middle of a login should not throw the login away.
    """
    if not reply.ok:
        return PENDING
    status = str(reply.body.get("status") or "")
    if status == "linked":
        return LINKED
    if status == "failed":
        return FAILED
    if status == "unknown":
        # The relay has never heard of it, or has forgotten it. Either way
        # nothing more will arrive, and waiting is only a longer silence.
        return EXPIRED
    return PENDING


def describe(host: dict | None, *, base_ok: bool | None = None) -> str:
    """The status line the window shows.

    Written for somebody who has never heard of Terraform: what is happening,
    and whether they can talk.
    """
    if host:
        state = host.get("state", "")
        name = host.get("name") or "your host"
        if host.get("error"):
            return f"{name} — failed: {host['error']}"
        return {
            "creating": f"{name} — being created, a few minutes",
            "running": f"{name} — running",
            "stopping": f"{name} — stopping",
            "stopped": f"{name} — stopped (still billed for its disk)",
            "starting": f"{name} — starting",
            "resetting": f"{name} — being repaired",
            "destroying": f"{name} — being destroyed",
            "destroyed": f"{name} — destroyed",
        }.get(state, f"{name} — {state}")

    if base_ok is None:
        return "shared host — checking…"
    return ("shared host — connected" if base_ok
            else "shared host — not reachable")

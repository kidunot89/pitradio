"""Talking to the relay's host endpoints.

The part that matters most is `pairing_outcome`: a window that reads "gone" as
"still waiting" sits with every button disabled and nothing to say, which is
exactly what somebody sees after closing the browser tab — by far the commonest
way an OAuth login ends without finishing.
"""

import pytest

from pitradio import hostapi


def reply(status: int = 200, body: dict | None = None, ok: bool = True,
          error: str = "") -> hostapi.Reply:
    return hostapi.Reply(ok, status, body or {}, error)


# -- how a login ends ------------------------------------------------------


def test_a_completed_login_is_linked():
    assert hostapi.pairing_outcome(
        reply(body={"status": "linked", "host_token": "t"})) == hostapi.LINKED


def test_a_refused_login_is_failed():
    """The user pressed Deny; DigitalOcean redirects with an error."""
    assert hostapi.pairing_outcome(
        reply(body={"status": "failed"})) == hostapi.FAILED


def test_a_pairing_the_relay_has_forgotten_is_expired():
    """Nothing more will arrive, so waiting is only a longer silence — and this
    is what a closed browser tab eventually becomes."""
    assert hostapi.pairing_outcome(
        reply(body={"status": "unknown"})) == hostapi.EXPIRED


def test_an_unfinished_login_is_pending():
    assert hostapi.pairing_outcome(
        reply(body={"status": "pending", "interval": 3})) == hostapi.PENDING


def test_an_unreachable_relay_does_not_end_the_login():
    """A moment of network trouble mid-login must not throw the login away."""
    assert hostapi.pairing_outcome(
        reply(ok=False, status=0, error="could not reach")) == hostapi.PENDING


@pytest.mark.parametrize("body", [{}, {"status": ""}, {"status": "nonsense"}])
def test_anything_unrecognised_is_pending_not_an_error(body):
    assert hostapi.pairing_outcome(reply(body=body)) == hostapi.PENDING


def test_the_window_gives_up_before_the_relay_forgets():
    """Otherwise the last minutes are spent polling something already gone,
    and every message shown during them would be wrong."""
    assert hostapi.PAIRING_WAIT_SECONDS < 600


# -- addressing the relay --------------------------------------------------


@pytest.mark.parametrize(("relay", "expected"), [
    ("wss://relay.example.com", "https://relay.example.com"),
    ("wss://relay.example.com/", "https://relay.example.com"),
    ("ws://localhost:8080", "http://localhost:8080"),
])
def test_the_socket_url_becomes_an_http_one(relay, expected):
    assert hostapi.HostApi(relay).base == expected


@pytest.mark.parametrize("relay", ["", "   ", None])
def test_no_relay_means_nothing_to_ask(relay):
    assert not hostapi.HostApi(relay).configured


# -- what the window says --------------------------------------------------


def test_a_running_host_is_described_by_name():
    assert "running" in hostapi.describe({"name": "syd", "state": "running"})


def test_a_stopped_host_says_it_is_still_billed():
    """Stop is not destroy, and somebody reading this needs to know which."""
    assert "billed" in hostapi.describe({"name": "syd", "state": "stopped"})


def test_a_failed_host_shows_why():
    described = hostapi.describe(
        {"name": "syd", "state": "failed", "error": "terraform apply failed"})
    assert "terraform apply failed" in described


def test_with_no_host_of_your_own_the_shared_one_is_described():
    assert "shared host" in hostapi.describe(None, base_ok=True)
    assert "not reachable" in hostapi.describe(None, base_ok=False)


def test_before_anything_is_known_it_says_so():
    """Rather than claiming a state it has not checked."""
    assert "checking" in hostapi.describe(None)


def test_no_description_carries_an_address():
    """The window never shows which machine carries the audio."""
    for state in ("running", "stopped"):
        described = hostapi.describe(
            {"name": "syd", "state": state, "url": "wss://syd.example.com"})
        assert "wss://" not in described
        assert "example.com" not in described

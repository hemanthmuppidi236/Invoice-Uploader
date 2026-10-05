"""
Who may trigger the scheduled jobs (prompt §3, §11).

These endpoints make the app download arbitrary Drive files and send mail, so
none of them is ever open. But "not open" is not the same as "cron only", and
getting that distinction wrong is invisible from the server side: the
endpoint looks correctly locked down, and a button in the UI 401s on every
click while appearing to be a broken integration.

§3 gives the accountant role "run intake" in as many words, and `/invoices`
has a **Run intake now** button for exactly that. The browser sends a bearer
token and has no way to send the shared agent key — that secret lives on
Render and must not be shipped to a frontend.
"""

import inspect

import pytest
from fastapi.routing import APIRoute

from app.main import app


def _route(path: str) -> APIRoute:
    for r in app.routes:
        if isinstance(r, APIRoute) and r.path == path:
            return r
    raise AssertionError(f"no route {path}")


def _auth_source(path: str) -> str:
    source = inspect.getsource(_route(path).endpoint)
    return source[: source.index('"""')] if '"""' in source else source


def test_the_poll_endpoint_accepts_a_signed_in_accountant():
    """The Run intake now button. Requiring the agent key alone made it 401
    on every click, which read as a broken integration rather than a missing
    header — the frontend cannot send that key, and should not be able to."""
    auth = _auth_source("/jobs/poll-drive")
    assert "require_agent_or_role" in auth
    assert '"accountant"' in auth


def test_the_poll_endpoint_still_accepts_the_cron_key():
    """Render cron has no user session. require_agent_or_role takes either."""
    from app.core.auth import require_agent_or_role

    assert "require_agent_or_role" in _auth_source("/jobs/poll-drive")
    # The dependency's whole contract: an agent key short-circuits before any
    # user lookup, so the cron never needs a token.
    assert "X-Agent-Key" in inspect.getsource(require_agent_or_role)


@pytest.mark.parametrize("path", ["/jobs/poll-drive", "/jobs/email-daily", "/jobs/email-eod"])
def test_no_job_endpoint_is_open(path):
    """Each one downloads arbitrary Drive files or sends mail on demand."""
    auth = _auth_source(path)
    assert "require_agent_or_role" in auth or "require_job_key" in auth


@pytest.mark.parametrize("path", ["/jobs/email-daily", "/jobs/email-eod"])
def test_the_email_jobs_stay_cron_only(path):
    """No UI calls these, so the auth surface stays as narrow as the callers
    actually need. Widening it speculatively is how a key ends up accepted
    somewhere nobody checked."""
    assert "require_job_key" in _auth_source(path)


def test_an_unset_agent_key_denies_rather_than_allows():
    """A deploy that forgets AGENT_API_KEY must lock the cron out, not let
    everyone in."""
    from app.core.auth import _agent_key_matches

    assert _agent_key_matches(None) is False
    assert _agent_key_matches("") is False
    assert _agent_key_matches("anything") is False  # key is blank under test

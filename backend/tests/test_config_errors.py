"""
The startup error when configuration is incomplete.

Failing fast is right — a missing SUPABASE_URL should stop the process on
boot, not surface as a 500 on the first request. But on a hosted deploy the
raw pydantic traceback costs a full redeploy per guess: it names the field
and says nothing about where the value comes from, or whether anything else
is missing too. This is the difference between one fix and three.
"""

import pytest

from app.core.config import Settings, _load_settings


def _blank(monkeypatch, *names):
    """Remove variables and the dotenv fallback, so nothing fills them in."""
    for name in names:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(
        Settings, "model_config", {**Settings.model_config, "env_file": None}
    )


def test_a_missing_variable_is_named_with_where_to_get_it(monkeypatch):
    _blank(monkeypatch, "SUPABASE_ANON_KEY")
    monkeypatch.setenv("SUPABASE_URL", "https://x.supabase.co")
    monkeypatch.setenv("SUPABASE_SERVICE_ROLE_KEY", "svc")

    with pytest.raises(RuntimeError) as e:
        _load_settings()

    message = str(e.value)
    assert "SUPABASE_ANON_KEY" in message
    assert "Project Settings" in message      # where to find it
    assert "redeploy" in message              # what to do next


def test_every_missing_variable_is_reported_at_once(monkeypatch):
    """One redeploy, not three. Reporting the first missing value only is
    what turns a hosted deploy into a guessing loop."""
    _blank(
        monkeypatch,
        "SUPABASE_URL",
        "SUPABASE_ANON_KEY",
        "SUPABASE_SERVICE_ROLE_KEY",
    )
    with pytest.raises(RuntimeError) as e:
        _load_settings()

    message = str(e.value)
    for name in ("SUPABASE_URL", "SUPABASE_ANON_KEY", "SUPABASE_SERVICE_ROLE_KEY"):
        assert name in message


def test_the_anon_key_note_warns_about_a_project_mismatch(monkeypatch):
    """Two different Supabase projects is the failure this catches: tokens
    verify against one project while the data lives in another, and every
    symptom looks like a broken login rather than a wrong key."""
    _blank(monkeypatch, "SUPABASE_ANON_KEY")
    monkeypatch.setenv("SUPABASE_URL", "https://x.supabase.co")
    monkeypatch.setenv("SUPABASE_SERVICE_ROLE_KEY", "svc")

    with pytest.raises(RuntimeError) as e:
        _load_settings()
    assert "NEXT_PUBLIC_SUPABASE_ANON_KEY" in str(e.value)


def test_the_service_role_warning_is_in_the_message(monkeypatch):
    """The one configuration mistake with a security consequence, said at the
    moment somebody is copying keys out of the dashboard."""
    _blank(monkeypatch, "SUPABASE_SERVICE_ROLE_KEY")
    monkeypatch.setenv("SUPABASE_URL", "https://x.supabase.co")
    monkeypatch.setenv("SUPABASE_ANON_KEY", "anon")

    with pytest.raises(RuntimeError) as e:
        _load_settings()
    assert "NEXT_PUBLIC" in str(e.value)
    assert "bypasses RLS" in str(e.value)


def test_a_complete_configuration_still_loads(monkeypatch):
    _blank(monkeypatch)
    monkeypatch.setenv("SUPABASE_URL", "https://x.supabase.co")
    monkeypatch.setenv("SUPABASE_ANON_KEY", "anon")
    monkeypatch.setenv("SUPABASE_SERVICE_ROLE_KEY", "svc")

    loaded = _load_settings()
    assert loaded.supabase_url == "https://x.supabase.co"

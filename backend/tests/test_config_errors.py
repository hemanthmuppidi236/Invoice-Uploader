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


def test_a_near_miss_name_is_surfaced(monkeypatch):
    """The error names the pydantic FIELD, not the variable you typed, so a
    typo'd key and no key at all read identically. Somebody who has just
    verified the value they pasted has no way to see that the name beside it
    is wrong — unless the neighbours are printed."""
    _blank(monkeypatch, "SUPABASE_ANON_KEY")
    monkeypatch.setenv("SUPABASE_URL", "https://x.supabase.co")
    monkeypatch.setenv("SUPABASE_SERVICE_ROLE_KEY", "svc")
    monkeypatch.setenv("SUPABASE_ANNON_KEY", "eyJ-the-right-value-wrong-name")

    with pytest.raises(RuntimeError) as e:
        _load_settings()

    message = str(e.value)
    assert "SUPABASE_ANNON_KEY" in message
    assert "the name is wrong" in message


def test_no_value_is_ever_printed(monkeypatch):
    """This lands in a deploy log. Names diagnose the problem; values would
    put a service-role key somewhere it can be read forever."""
    secret = "eyJ-this-must-never-appear-in-a-log"
    _blank(monkeypatch, "SUPABASE_ANON_KEY")
    monkeypatch.setenv("SUPABASE_URL", "https://x.supabase.co")
    monkeypatch.setenv("SUPABASE_SERVICE_ROLE_KEY", secret)
    monkeypatch.setenv("SUPABASE_ANNON_KEY", secret)

    with pytest.raises(RuntimeError) as e:
        _load_settings()
    assert secret not in str(e.value)


def test_unrelated_variables_are_not_listed(monkeypatch):
    """A dump of the whole environment is noise, and noise is what stops the
    one useful line being read."""
    _blank(monkeypatch, "SUPABASE_ANON_KEY")
    monkeypatch.setenv("SUPABASE_URL", "https://x.supabase.co")
    monkeypatch.setenv("SUPABASE_SERVICE_ROLE_KEY", "svc")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-x")

    with pytest.raises(RuntimeError) as e:
        _load_settings()
    assert "ANTHROPIC_API_KEY" not in str(e.value)


# ─── Invisible characters from a dashboard copy-paste ─────────────────


@pytest.mark.parametrize(
    "junk,label",
    [
        ("\n", "a trailing newline"),
        (" ", "a trailing space"),
        (" ", "U+2028 LINE SEPARATOR"),
        (" ", "a non-breaking space"),
        ("﻿", "a byte-order mark"),
        ("​", "a zero-width space"),
    ],
)
def test_an_invisible_character_is_stripped_from_the_keys(monkeypatch, junk, label):
    """None of these are visible in a Render or Vercel settings field, and
    none break anything until the value goes into an HTTP header — at which
    point the runtime refuses it with a message naming a byte offset and no
    variable: "Cannot convert argument to a ByteString because the character
    at index 215 has a value of 8232". Neither a URL nor a JWT ever
    legitimately contains whitespace, so stripping it is safe."""
    _blank(monkeypatch)
    monkeypatch.setenv("SUPABASE_URL", f"https://x.supabase.co{junk}")
    monkeypatch.setenv("SUPABASE_ANON_KEY", f"{junk}eyJanon{junk}")
    monkeypatch.setenv("SUPABASE_SERVICE_ROLE_KEY", f"eyJsvc{junk}")

    loaded = _load_settings()
    assert loaded.supabase_url == "https://x.supabase.co", label
    assert loaded.supabase_anon_key == "eyJanon", label
    assert loaded.supabase_service_role_key == "eyJsvc", label


def test_every_stripped_value_is_header_safe(monkeypatch):
    """The actual invariant: whatever comes out can be put in an HTTP header.
    Latin-1 encodability is exactly the check the runtime was failing."""
    _blank(monkeypatch)
    monkeypatch.setenv("SUPABASE_URL", "https://x.supabase.co ")
    monkeypatch.setenv("SUPABASE_ANON_KEY", "eyJanon ")
    monkeypatch.setenv("SUPABASE_SERVICE_ROLE_KEY", "eyJsvc﻿")

    loaded = _load_settings()
    for value in (
        loaded.supabase_url,
        loaded.supabase_anon_key,
        loaded.supabase_service_role_key,
    ):
        value.encode("latin-1")  # raises if a character is above 255

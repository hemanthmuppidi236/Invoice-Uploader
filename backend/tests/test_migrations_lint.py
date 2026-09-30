"""
Static checks on the SQL migrations.

There is no Postgres in the test environment, so these cannot execute the
migrations — they catch the specific mistakes that are invisible until a real
server rejects them. Every rule here exists because something actually broke.
"""

import re
from pathlib import Path

import pytest

MIGRATIONS = sorted((Path(__file__).resolve().parents[2] / "migrations").glob("*.sql"))


def test_migrations_exist():
    assert MIGRATIONS, "no migration files found"


@pytest.mark.parametrize("path", MIGRATIONS, ids=lambda p: p.name)
def test_no_non_immutable_cast_in_an_index_expression(path: Path):
    """Postgres refuses a STABLE expression in an index.

    `created_at::DATE` on a TIMESTAMPTZ is STABLE — the result depends on the
    session TimeZone — so it fails with 42P17 "functions in index expression
    must be marked IMMUTABLE". The fix is to pin the zone explicitly:
    `(created_at AT TIME ZONE 'UTC')::DATE`, which is immutable.

    This shipped once and was only caught when the migration was first applied
    to a real database.
    """
    sql = path.read_text()

    for statement in _statements(sql):
        if not re.match(r"\s*CREATE\s+(UNIQUE\s+)?INDEX", statement, re.I):
            continue
        # A cast to date/time that is NOT preceded by an explicit AT TIME ZONE.
        for match in re.finditer(r"::\s*(date|time)\b", statement, re.I):
            preceding = statement[: match.start()]
            if not re.search(r"AT\s+TIME\s+ZONE", preceding, re.I):
                pytest.fail(
                    f"{path.name}: index expression casts to "
                    f"{match.group(1)} without pinning a timezone.\n"
                    f"On a TIMESTAMPTZ column that is STABLE, not IMMUTABLE, "
                    f"and Postgres rejects it with 42P17.\n"
                    f"Use (col AT TIME ZONE 'UTC')::DATE instead.\n"
                    f"Statement: {statement.strip()[:220]}"
                )


@pytest.mark.parametrize("path", MIGRATIONS, ids=lambda p: p.name)
def test_no_now_or_current_date_in_an_index_expression(path: Path):
    """Same class of failure: now(), current_date, and localtimestamp are all
    STABLE, and an index built on one would be wrong the next day anyway."""
    for statement in _statements(path.read_text()):
        if not re.match(r"\s*CREATE\s+(UNIQUE\s+)?INDEX", statement, re.I):
            continue
        banned = re.search(
            r"\b(now\s*\(\s*\)|current_date|current_timestamp|localtimestamp)\b",
            statement,
            re.I,
        )
        if banned:
            pytest.fail(
                f"{path.name}: index expression uses {banned.group(1)!r}, "
                "which is not immutable and would also go stale.\n"
                f"Statement: {statement.strip()[:220]}"
            )


@pytest.mark.parametrize("path", MIGRATIONS, ids=lambda p: p.name)
def test_seed_migrations_are_rerunnable(path: Path):
    """A seed has to be safe to re-apply.

    Reference data gets corrected and re-run; an INSERT with no conflict
    handling turns that into a duplicate-key error halfway through, leaving
    the seed half applied.
    """
    if "seed" not in path.name:
        return
    sql = path.read_text()
    if re.search(r"^\s*INSERT\s+INTO", sql, re.I | re.M):
        assert re.search(r"ON\s+CONFLICT", sql, re.I), (
            f"{path.name} inserts seed data with no ON CONFLICT clause, so "
            "re-running it fails partway through."
        )


def _statements(sql: str) -> list[str]:
    """Split on semicolons, with comments stripped first.

    Crude, and deliberately so: it only has to be good enough to isolate
    CREATE INDEX statements. Dollar-quoted function bodies are dropped
    wholesale rather than parsed, since none of them contain an index.
    """
    sql = re.sub(r"\$\$.*?\$\$", "", sql, flags=re.S)
    sql = re.sub(r"--[^\n]*", "", sql)
    return [s for s in sql.split(";") if s.strip()]

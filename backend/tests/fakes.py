"""
A fake Supabase/PostgREST client, shared by the tests that exercise database
logic without a database.

The real client is fluent — `.table().select().eq().limit().execute()` — and
the behaviour under test is almost always a branch on what came back, not the
query itself. Standing up Postgres for that is a poor trade; getting the
filter semantics right here is the cheaper and more honest one, which is why
the operators below model PostgREST rather than Python equality (`is_` takes
a string "null", `in_` takes a list, a NULL never equals anything).
"""

from datetime import datetime, timezone


class FakeQuery:
    """One fluent query against a FakeTable."""

    def __init__(self, table, op, payload=None):
        self.table = table
        self.op = op
        self.payload = payload
        self.filters: list[tuple[str, str, object]] = []

    # Filter builders — each returns self so the chain keeps going.
    def eq(self, col, val):
        self.filters.append(("eq", col, val))
        return self

    def neq(self, col, val):
        self.filters.append(("neq", col, val))
        return self

    def in_(self, col, vals):
        self.filters.append(("in", col, [str(v) for v in vals]))
        return self

    def gte(self, col, val):
        self.filters.append(("gte", col, val))
        return self

    def gt(self, col, val):
        self.filters.append(("gt", col, val))
        return self

    def lte(self, col, val):
        self.filters.append(("lte", col, val))
        return self

    def lt(self, col, val):
        self.filters.append(("lt", col, val))
        return self

    def is_(self, col, val):
        # PostgREST spells IS NULL as the literal string "null".
        self.filters.append(("isnull" if val == "null" else "is", col, val))
        return self

    def ilike(self, col, pattern):
        self.filters.append(("ilike", col, pattern.strip("%")))
        return self

    def contains(self, col, vals):
        self.filters.append(("contains", col, vals))
        return self

    @property
    def not_(self):
        """PostgREST's `.not_.is_(col, "null")` — negates the next filter."""
        return _Negated(self)

    def limit(self, _n):
        return self

    def order(self, *_a, **_k):
        return self

    def select(self, *_a):
        return self

    def execute(self):
        if self.op == "select":
            return FakeResult(self._matching())
        if self.op == "insert":
            rows = (
                self.payload
                if isinstance(self.payload, list)
                else [self.payload]
            )
            created = []
            for row in rows:
                new = dict(row)
                new.setdefault("id", f"{self.table.name}-{len(self.table.rows) + 1}")
                # Every table in migration 001 declares
                # `created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()`, and several
                # queries filter on it (the email send-once check, the EOD
                # window). Modelling the default here keeps the fake faithful
                # to the schema instead of leaving those filters untestable.
                new.setdefault(
                    "created_at", datetime.now(timezone.utc).isoformat()
                )
                self.table.rows.append(new)
                created.append(new)
            return FakeResult(created)
        if self.op == "update":
            hit = self._matching()
            for row in hit:
                row.update(self.payload)
            return FakeResult(hit)
        if self.op == "delete":
            keep, removed = [], []
            for row in self.table.rows:
                (removed if self._match(row) else keep).append(row)
            self.table.rows = keep
            return FakeResult(removed)
        raise AssertionError(f"unsupported op {self.op}")

    def _matching(self):
        return [r for r in self.table.rows if self._match(r)]

    def _match(self, row):
        for kind, col, val in self.filters:
            if not _matches_one(row.get(col), kind, val):
                return False
        return True


class _Negated:
    """Supports `query.not_.is_(col, "null")` by inverting the filter added."""

    def __init__(self, query):
        self.query = query

    def is_(self, col, val):
        self.query.filters.append(
            ("notnull" if val == "null" else "isnot", col, val)
        )
        return self.query

    def in_(self, col, vals):
        self.query.filters.append(("notin", col, [str(v) for v in vals]))
        return self.query


def _matches_one(actual, kind, val):
    """Evaluate one filter against one column value.

    Comparisons are string-based, which is how PostgREST receives them and
    which sorts ISO timestamps and zero-padded decimals correctly. Amounts
    are compared numerically where both sides parse, so "100.00" and "100"
    are the same amount.
    """
    if kind == "eq":
        return _same(actual, val)
    if kind == "neq":
        return not _same(actual, val)
    if kind == "in":
        return str(actual) in val
    if kind == "notin":
        return str(actual) not in val
    if kind == "isnull":
        return actual is None
    if kind == "notnull":
        return actual is not None
    if kind == "ilike":
        return val.lower() in str(actual or "").lower()
    if kind == "contains":
        return all(v in (actual or []) for v in val)
    if kind in ("gte", "gt", "lte", "lt"):
        if actual is None:
            return False
        left, right = str(actual), str(val)
        if kind == "gte":
            return left >= right
        if kind == "gt":
            return left > right
        if kind == "lte":
            return left <= right
        return left < right
    raise AssertionError(f"unsupported filter {kind}")


def _same(actual, expected):
    if str(actual) == str(expected):
        return True
    try:
        from decimal import Decimal

        return Decimal(str(actual)) == Decimal(str(expected))
    except Exception:
        return False


class FakeResult:
    def __init__(self, data):
        self.data = data


class FakeTable:
    def __init__(self, name, rows=None):
        self.name = name
        self.rows = rows or []

    def select(self, *_a):
        return FakeQuery(self, "select")

    def insert(self, payload):
        return FakeQuery(self, "insert", payload)

    def update(self, payload):
        return FakeQuery(self, "update", payload)

    def delete(self):
        return FakeQuery(self, "delete")


class FakeClient:
    def __init__(self):
        self.tables: dict[str, FakeTable] = {}

    def table(self, name):
        return self.tables.setdefault(name, FakeTable(name))

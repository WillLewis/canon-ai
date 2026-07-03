"""COGS metering (P3-OPS): write usage_events rows, price them, roll them up.

The table is `usage_events` from supabase/migrations/20260703060000_identity_
and_access.sql: (user_id uuid, world_id bigint, kind text, tokens_in int,
tokens_out int, cost_usd numeric(10,4), created_at timestamptz default now()).

Two entry points:

    record_usage(cursor, ...)   direct insert, cost computed from PRICING
    meter(kind, ...)            decorator/context-manager that reads
                                response.usage off an Anthropic response

`meter` is designed so an extract/llm.py call site adopts it with a one-line
change (no edits to extract/ here — the extraction stream wires it):

    response = client.messages.create(**request)
becomes
    response = meter("extraction", cursor=cur, user_id=uid, world_id=wid)(
        client.messages.create)(**request)

Metering never breaks the metered call: by the time we record, the tokens are
already spent, so a failed insert logs an alert and returns the response
anyway (availability beats bookkeeping).
"""

from __future__ import annotations

import functools
from decimal import Decimal
from typing import Any

from . import alerts

# Per-Mtok USD (input, output). Update procedure: docs/ops.md ("Updating the
# pricing table"). Keys are the model ids the pipeline passes to the API
# (extract/llm.py DEFAULT_MODEL is one of these).
PRICING: dict[str, tuple[float, float]] = {
    "claude-sonnet-5": (3.00, 15.00),
    "claude-opus-4-8": (5.00, 25.00),
    "claude-haiku-4-5": (1.00, 5.00),
}

# Unknown model -> cost 0, and the kind is suffixed so unpriced rows are
# greppable and per-world COGS is visibly incomplete rather than silently low.
UNPRICED_SUFFIX = ":unpriced"

_MTOK = Decimal(1_000_000)

_INSERT_SQL = (
    "INSERT INTO usage_events (user_id, world_id, kind, tokens_in, tokens_out, cost_usd) "
    "VALUES (%s, %s, %s, %s, %s, %s)"
)


def cost_usd(model: str | None, tokens_in: int, tokens_out: int) -> tuple[Decimal, bool]:
    """(cost, priced). Unknown/None model -> (0, False). Decimal, not float:
    cost_usd is numeric(10,4) in Postgres and money should not accumulate
    binary-float dust across thousands of rows."""
    prices = PRICING.get(model or "")
    if prices is None:
        return Decimal("0"), False
    price_in, price_out = (Decimal(str(p)) for p in prices)
    return (price_in * int(tokens_in) + price_out * int(tokens_out)) / _MTOK, True


def record_usage(cursor, *, user_id, world_id, kind: str,
                 tokens_in: int, tokens_out: int, model: str | None) -> Decimal:
    """Insert one usage_events row; returns the cost recorded."""
    cost, priced = cost_usd(model, tokens_in, tokens_out)
    if not priced:
        kind = f"{kind}{UNPRICED_SUFFIX}"
    cursor.execute(_INSERT_SQL, (user_id, world_id, kind, int(tokens_in), int(tokens_out), cost))
    return cost


class Meter:
    """Wraps a callable returning an Anthropic response object, or acts as a
    context manager whose .record(response) you call yourself. Reads
    response.usage.input_tokens / output_tokens and response.model (falling
    back to the model= passed at construction)."""

    def __init__(self, kind: str, *, cursor, user_id, world_id=None, model: str | None = None):
        self.kind = kind
        self.cursor = cursor
        self.user_id = user_id
        self.world_id = world_id
        self.model = model
        self.cost: Decimal | None = None   # set after a successful record

    def record(self, response: Any) -> Any:
        usage = getattr(response, "usage", None)
        tokens_in = getattr(usage, "input_tokens", 0) or 0
        tokens_out = getattr(usage, "output_tokens", 0) or 0
        model = getattr(response, "model", None) or self.model
        try:
            self.cost = record_usage(
                self.cursor, user_id=self.user_id, world_id=self.world_id,
                kind=self.kind, tokens_in=tokens_in, tokens_out=tokens_out, model=model,
            )
        except Exception as exc:  # never lose a paid-for response to bookkeeping
            alerts.alert("metering_error", {
                "kind": self.kind, "user_id": str(self.user_id),
                "world_id": self.world_id, "error": repr(exc),
            })
        return response

    def __call__(self, fn):
        @functools.wraps(fn)
        def wrapped(*args, **kwargs):
            return self.record(fn(*args, **kwargs))
        return wrapped

    def __enter__(self) -> "Meter":
        return self

    def __exit__(self, exc_type, exc, tb) -> bool:
        return False  # never swallows exceptions from the metered block


def meter(kind: str, *, cursor, user_id, world_id=None, model: str | None = None) -> Meter:
    """See Meter. Factory kept as a function so call sites read as one line."""
    return Meter(kind, cursor=cursor, user_id=user_id, world_id=world_id, model=model)


# --- rollups -----------------------------------------------------------------

def _rollup(row) -> dict:
    row = row or (0, 0, 0, 0)
    return {"cost_usd": row[0], "tokens_in": row[1], "tokens_out": row[2], "events": row[3]}


def per_world_cogs(cursor, world_id) -> dict:
    """Lifetime COGS for one world: {cost_usd, tokens_in, tokens_out, events}."""
    cursor.execute(
        "SELECT COALESCE(SUM(cost_usd), 0), COALESCE(SUM(tokens_in), 0), "
        "COALESCE(SUM(tokens_out), 0), COUNT(*) FROM usage_events WHERE world_id = %s",
        (world_id,),
    )
    return _rollup(cursor.fetchone())


def per_user_cogs(cursor, user_id, since=None) -> dict:
    """COGS for one user, optionally only rows with created_at >= since."""
    sql = (
        "SELECT COALESCE(SUM(cost_usd), 0), COALESCE(SUM(tokens_in), 0), "
        "COALESCE(SUM(tokens_out), 0), COUNT(*) FROM usage_events WHERE user_id = %s"
    )
    params: tuple = (user_id,)
    if since is not None:
        sql += " AND created_at >= %s"
        params += (since,)
    cursor.execute(sql, params)
    return _rollup(cursor.fetchone())

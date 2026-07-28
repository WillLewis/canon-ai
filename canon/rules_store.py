"""world_rules CRUD — pure cursor functions (P3-RULES, Wave 4).

The table does not exist in a migration yet: Wave 5 owns the migration lane,
so the exact DDL (+ RLS) is queued in MIGRATIONS-NEEDED.md. Everything here
takes a plain cursor and never opens connections, commits, or calls anything
beyond SQL — callers own the transaction (ui/rules_ui.py commits per request;
tests drive a fake cursor).

Validation note: this module checks only the kind enum. The typed validation
(closed predicate vocabulary, subject-scope ambiguity, ranges) lives in
canon/rules.py, whose save_rule() is the only write path the UI uses — a rule
that can't be constructed can't reach this table.
"""

from __future__ import annotations

import json

RULE_KINDS = ("cannot", "only", "exception")

_COLUMNS = ("id", "kind", "params", "label", "created_by", "created_at", "disabled_at")
_SELECT = ("select id::text, kind, params, label, created_by::text, created_at, disabled_at"
           " from world_rules")


def _row_to_dict(row) -> dict:
    d = dict(zip(_COLUMNS, row))
    if isinstance(d["params"], str):  # psycopg2 or fakes hand jsonb back as text
        d["params"] = json.loads(d["params"])
    d["enabled"] = d["disabled_at"] is None
    return d


def create_rule(cur, world_id: int, kind: str, params: dict,
                label=None, created_by=None) -> str:
    """Insert one rule; returns its uuid (text)."""
    if kind not in RULE_KINDS:
        raise ValueError(f"kind must be one of {RULE_KINDS}, got {kind!r}")
    cur.execute(
        "insert into world_rules (world_id, kind, params, label, created_by)"
        " values (%(world_id)s, %(kind)s, %(params)s::jsonb, %(label)s, %(created_by)s::uuid)"
        " returning id::text",
        {"world_id": world_id, "kind": kind, "params": json.dumps(params),
         "label": (label or None), "created_by": created_by})
    return cur.fetchone()[0]


def list_rules(cur, world_id: int, enabled_only: bool = False) -> list[dict]:
    """Every rule for a world (or only the enabled ones), oldest first."""
    sql = _SELECT + " where world_id = %(world_id)s"
    if enabled_only:
        sql += " and disabled_at is null"
    sql += " order by created_at, id"
    cur.execute(sql, {"world_id": world_id})
    return [_row_to_dict(r) for r in cur.fetchall()]


def get_rule(cur, world_id: int, rule_id: str) -> dict | None:
    cur.execute(_SELECT + " where world_id = %(world_id)s and id = %(id)s::uuid",
                {"world_id": world_id, "id": rule_id})
    row = cur.fetchone()
    return _row_to_dict(row) if row else None


def set_rule_enabled(cur, world_id: int, rule_id: str, enabled: bool) -> bool:
    """Enable (clear disabled_at) or disable (stamp it). Idempotent."""
    cur.execute(
        "update world_rules set disabled_at = case when %(enabled)s then null else now() end"
        " where world_id = %(world_id)s and id = %(id)s::uuid"
        " returning id",
        {"enabled": enabled, "world_id": world_id, "id": rule_id})
    return cur.fetchone() is not None


def delete_rule(cur, world_id: int, rule_id: str) -> bool:
    cur.execute(
        "delete from world_rules where world_id = %(world_id)s and id = %(id)s::uuid"
        " returning id",
        {"world_id": world_id, "id": rule_id})
    return cur.fetchone() is not None

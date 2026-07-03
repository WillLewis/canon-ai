"""Full export — the leave-anytime contract, made executable (P3-TRUST).

Writers hand Canon unproduced, unregistered material. The trust position is
that leaving must be as easy as arriving: `export_world` returns a zip holding
EVERY row Canon stores about a world (raw scene text, every assertion at every
status with its citations, the writer's own rulings with attribution — their
judgments are theirs), plus the two human-readable artifacts (bible.md,
report.md) rendered by the existing canon.export renderers. `export_account`
wraps a whole account: profile, memberships, usage history, and one world-zip
per owned world.

Everything here is pure deterministic SQL + assembly over a DB cursor. No LLM
calls, no network, no story generation.

================================ COMPLETENESS =================================
COMPLETENESS IS THE CONTRACT. Any migration that adds a world-scoped or
user-scoped table MUST add it to WORLD_TABLES / OPTIONAL_WORLD_TABLES /
ACCOUNT_TABLES below (or, with a written reason, to EXPORT_EXEMPT).
tests/test_trust.py cross-checks these registries against every CREATE TABLE
in supabase/migrations/ + db/schema.sql, so a forgotten table is a test
failure, not a silent hole in someone's export.
===============================================================================
"""

from __future__ import annotations

import io
import json
import re
import zipfile
from datetime import datetime, timezone
from typing import Any

from .bible import build_bible
from .render import render_bible_markdown

FORMAT_VERSION = 1

# ---------------------------------------------------------------------------
# Table registries. Each entry: table name -> SELECT returning every column
# (SELECT <alias>.* so columns added by future migrations export automatically)
# scoped to one world via %(w)s. Ordering inside each stream is deterministic.
# ---------------------------------------------------------------------------

WORLD_TABLES: dict[str, str] = {
    "worlds": "SELECT * FROM worlds WHERE id = %(w)s",
    "works": "SELECT * FROM works WHERE world_id = %(w)s ORDER BY sort_order, id",
    "scenes": (  # full scene records, raw script text included
        "SELECT s.* FROM scenes s JOIN works wk ON wk.id = s.work_id "
        "WHERE wk.world_id = %(w)s ORDER BY s.story_position, s.id"
    ),
    "entities": "SELECT * FROM entities WHERE world_id = %(w)s ORDER BY id",
    "aliases": (
        "SELECT al.* FROM aliases al JOIN entities e ON e.id = al.entity_id "
        "WHERE e.world_id = %(w)s ORDER BY al.entity_id, al.alias"
    ),
    # All statuses (draft/canon/sealed/retconned/rejected) — the writer gets
    # everything, not just what the bible shows. The joined columns carry the
    # citation in human-readable form beside the raw established_in_scene id.
    "assertions": (
        "SELECT a.*, s.slug AS cited_scene_slug, s.story_position AS cited_story_position, "
        "wk.title AS cited_work_title "
        "FROM assertions a JOIN scenes s ON s.id = a.established_in_scene "
        "JOIN works wk ON wk.id = s.work_id "
        "WHERE a.world_id = %(w)s ORDER BY a.id"
    ),
    "character_locations": (
        "SELECT cl.* FROM character_locations cl JOIN assertions a ON a.id = cl.assertion_id "
        "WHERE a.world_id = %(w)s ORDER BY cl.assertion_id"
    ),
    "scene_presence": (
        "SELECT sp.* FROM scene_presence sp JOIN scenes s ON s.id = sp.scene_id "
        "JOIN works wk ON wk.id = s.work_id "
        "WHERE wk.world_id = %(w)s ORDER BY sp.scene_id, sp.entity_id"
    ),
    "findings": "SELECT * FROM findings WHERE world_id = %(w)s ORDER BY id",
    # seals and coverage_notes carry the writer's rulings AND their attribution
    # (ruled_by/ruled_at, status_changed_by/status_changed_at): their judgments
    # are theirs and leave with them.
    "seals": "SELECT * FROM seals WHERE world_id = %(w)s ORDER BY check_name, assertion_a",
    "coverage_notes": (
        "SELECT * FROM coverage_notes WHERE world_id = %(w)s ORDER BY family, note_key"
    ),
    "share_links": "SELECT * FROM share_links WHERE world_id = %(w)s ORDER BY created_at, token",
    "world_members": "SELECT * FROM world_members WHERE world_id = %(w)s ORDER BY user_id",
}

# Tables that land in a parallel workstream: exported when present, silently
# skipped when the migration has not run yet. Feature-detected per export.
OPTIONAL_WORLD_TABLES: dict[str, str] = {
    "world_rules": "SELECT * FROM world_rules WHERE world_id = %(w)s ORDER BY id",  # P3-RULES
    # P3-ENGINE: per-world check-family toggles (20260703090000_report_engine_hardening)
    "world_family_config": (
        "SELECT * FROM world_family_config WHERE world_id = %(w)s ORDER BY family"
    ),
}

# Account-scoped tables, exported by export_account via %(u)s.
ACCOUNT_TABLES: dict[str, str] = {
    "profiles": "SELECT * FROM profiles WHERE user_id = %(u)s::uuid",
    "world_members": (
        "SELECT * FROM world_members WHERE user_id = %(u)s::uuid ORDER BY world_id"
    ),
    "usage_events": "SELECT * FROM usage_events WHERE user_id = %(u)s::uuid ORDER BY id",
    # Billing state (P3-BILLING, landed by 20260703100000_queued_ddl_and_
    # attribution.sql): the writer's data, so it leaves with them too.
    "billing_customers": (
        "SELECT * FROM billing_customers WHERE user_id = %(u)s::uuid"
    ),
    "billing_subscriptions": (
        "SELECT * FROM billing_subscriptions WHERE user_id = %(u)s::uuid"
    ),
}

# Tables that exist in the schema but are deliberately NOT exported. Every
# entry needs a written reason; an empty dict is the goal state.
EXPORT_EXEMPT: dict[str, str] = {}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _rows_as_dicts(cur) -> list[dict[str, Any]]:
    """fetchall() as dicts whether the cursor yields tuples or dict_row dicts."""
    rows = cur.fetchall()
    if rows and isinstance(rows[0], dict):
        return [dict(r) for r in rows]
    cols = [d[0] for d in cur.description]
    return [dict(zip(cols, r)) for r in rows]


def _jsonl(rows: list[dict[str, Any]]) -> str:
    """One JSON object per line. default=str covers timestamps, uuids,
    Decimals, and Postgres range types without inventing anything."""
    return "".join(json.dumps(r, default=str, sort_keys=True) + "\n" for r in rows)


def table_exists(cursor, table: str) -> bool:
    """Feature-detect a table (parallel workstreams land theirs later)."""
    cursor.execute(
        "SELECT 1 FROM information_schema.tables "
        "WHERE table_schema = 'public' AND table_name = %s",
        (table,),
    )
    return cursor.fetchone() is not None


class _CursorConn:
    """Minimal connection facade over a cursor, so the bible/report loaders
    (which take a conn and call .cursor()/.rollback()) run inside the caller's
    transaction instead of opening their own."""

    def __init__(self, cursor):
        self._cursor = cursor

    def cursor(self):
        return self._cursor

    def rollback(self) -> None:
        # The caller owns the transaction; loaders' courtesy rollback is a no-op.
        pass


def render_bible_md(cursor, world_id: int) -> str:
    """bible.md via the existing renderer (module-level seam for tests)."""
    bible, _errors = build_bible(_CursorConn(cursor), world_id)
    return render_bible_markdown(bible)


def render_report_md(cursor, world_id: int, world_name: str) -> str:
    """report.md via the existing Reader's Report renderer, over persisted
    findings + coverage notes (an export never regenerates anything)."""
    from .. import report as report_mod
    from .cli import load_coverage_notes

    conn = _CursorConn(cursor)
    snapshot = report_mod.load_world_snapshot(conn, world_id)
    notes = load_coverage_notes(conn, world_id)
    cursor.execute(
        "SELECT check_name, severity, explanation, sealed FROM findings "
        "WHERE world_id = %(w)s ORDER BY id",
        {"w": world_id},
    )
    findings = _rows_as_dicts(cursor)
    return report_mod.render_markdown_report(world_name, snapshot, findings, notes)


def safe_slug(name: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]+", "_", name or "world").strip("_") or "world"


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


# ---------------------------------------------------------------------------
# World export
# ---------------------------------------------------------------------------

def export_world(cursor, world_id: int) -> bytes:
    """Everything Canon stores about one world, as an in-memory zip.

    Members: manifest.json, <table>.jsonl per registry table (plus
    world_rules.jsonl when that table exists), bible.md, report.md.
    Raises ValueError when the world does not exist.
    """
    cursor.execute("SELECT * FROM worlds WHERE id = %(w)s", {"w": world_id})
    world_rows = _rows_as_dicts(cursor)
    if not world_rows:
        raise ValueError(f"world {world_id} not found")
    world = world_rows[0]
    world_name = str(world.get("name") or world_id)

    tables = dict(WORLD_TABLES)
    for opt, sql in OPTIONAL_WORLD_TABLES.items():
        if table_exists(cursor, opt):
            tables[opt] = sql

    streams: dict[str, str] = {}
    counts: dict[str, int] = {}
    for table, sql in tables.items():
        cursor.execute(sql, {"w": world_id})
        rows = _rows_as_dicts(cursor)
        streams[f"{table}.jsonl"] = _jsonl(rows)
        counts[table] = len(rows)

    streams["bible.md"] = render_bible_md(cursor, world_id)
    streams["report.md"] = render_report_md(cursor, world_id, world_name)

    manifest = {
        "format_version": FORMAT_VERSION,
        "kind": "world_export",
        "exported_at": _now_iso(),
        "world": {k: (v if isinstance(v, (int, str, bool, type(None))) else str(v))
                  for k, v in world.items()},
        "counts": counts,
        "members": sorted(streams) + ["manifest.json"],
        "generator": "canon-ai full_export",
        "note": (
            "Complete export of this world: every stored row (JSONL, one object "
            "per line) plus the rendered bible and Reader's Report. Your rulings "
            "(seals, note statuses, confirmations) and their attribution are "
            "included — your judgments are yours."
        ),
    }
    streams["manifest.json"] = json.dumps(manifest, indent=2, sort_keys=True) + "\n"

    return _zip_bytes(streams)


def _zip_bytes(members: dict[str, str | bytes]) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for name in sorted(members):
            data = members[name]
            zf.writestr(name, data if isinstance(data, bytes) else data.encode("utf-8"))
    return buf.getvalue()


# ---------------------------------------------------------------------------
# Account export
# ---------------------------------------------------------------------------

def export_account(cursor, user_id: str) -> bytes:
    """Everything Canon stores about one account: profile, memberships, usage
    history, and a full world-zip for every world the account owns."""

    streams: dict[str, str | bytes] = {}
    counts: dict[str, int] = {}
    for table, sql in ACCOUNT_TABLES.items():
        cursor.execute(sql, {"u": user_id})
        rows = _rows_as_dicts(cursor)
        streams[f"{table}.jsonl"] = _jsonl(rows)
        counts[table] = len(rows)

    cursor.execute(
        "SELECT id, name FROM worlds WHERE owner_id = %(u)s::uuid ORDER BY id",
        {"u": user_id},
    )
    owned = _rows_as_dicts(cursor)
    world_zips = []
    for w in owned:
        member = f"worlds/{safe_slug(str(w['name']))}-{w['id']}.zip"
        streams[member] = export_world(cursor, w["id"])
        world_zips.append({"id": w["id"], "name": w["name"], "member": member})

    manifest = {
        "format_version": FORMAT_VERSION,
        "kind": "account_export",
        "exported_at": _now_iso(),
        "user_id": str(user_id),
        "counts": counts,
        "owned_worlds": world_zips,
        "members": sorted(streams) + ["manifest.json"],
        "generator": "canon-ai full_export",
        "note": (
            "Complete export of this account: profile, world memberships, usage "
            "history, and one full world export per owned world."
        ),
    }
    streams["manifest.json"] = json.dumps(manifest, indent=2, sort_keys=True) + "\n"
    return _zip_bytes(streams)

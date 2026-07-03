"""Hard deletion — gone means gone (P3-TRUST).

Pure cursor functions, deterministic, no soft-delete ambiguity: `delete_world`
removes every row of a world's material in FK-safe order (children first) and
`delete_account` removes an account — its solely-owned worlds, its memberships
on co-owned worlds (after handing worlds.owner_id on those survivors to the
earliest surviving owner), its usage history, its billing rows, and finally
its profile. Both return per-table deleted counts so the confirmation page can
show the writer exactly what left the database.

Scope notes (deliberate, documented):

- **Object storage is out of scope** — the product has no file/upload storage
  yet (scripts are ingested into `scenes.raw_text`, which IS deleted here).
  When file storage lands, its cleanup MUST be added to these functions and to
  the completeness cross-check in tests/test_trust.py.
- **Attribution uuids on surviving worlds stay.** seals.ruled_by and
  coverage_notes.status_changed_by are plain uuids with no FK by design
  (identity_and_access migration): a ruling on a co-owned world is that
  world's data and survives the ruler's account. The uuid alone identifies no
  one once the profile row is gone.
- **auth.users is Supabase's.** delete_account removes every Canon-owned row;
  deleting the auth.users row itself is done via the Supabase admin API by the
  caller (profiles would cascade anyway — we delete it explicitly first so
  nothing depends on that cascade).
- Trigger-maintained mirrors (none today) and LLM state (none stored) do not
  exist; nothing is retained.

Like full_export.py: any migration that adds a world- or user-scoped table
MUST add it to the ordered lists below — tests/test_trust.py cross-checks the
delete order against the export registries and the schema.
"""

from __future__ import annotations

from canon.export.full_export import table_exists

# ---------------------------------------------------------------------------
# World deletion — FK-safe order, children first.
#
# Dependency map (see db/schema.sql + supabase/migrations):
#   character_locations -> assertions, entities
#   scene_presence      -> scenes, entities
#   findings            -> assertions, scenes, worlds
#   seals               -> assertions, worlds
#   assertions          -> entities, scenes, assertions (self: superseded_by /
#                          object_assertion_id — safe to delete in ONE statement,
#                          FK NO ACTION is checked at end of statement)
#   aliases             -> entities
#   entities            -> worlds
#   scenes              -> works
#   works               -> worlds
#   coverage_notes, share_links, world_members -> worlds
#   usage_events        -> worlds ON DELETE SET NULL (account data, not world
#                          material; deleted with the account, not the world)
# ---------------------------------------------------------------------------

WORLD_DELETE_ORDER: list[tuple[str, str]] = [
    ("character_locations",
     "DELETE FROM character_locations WHERE assertion_id IN "
     "(SELECT id FROM assertions WHERE world_id = %(w)s)"),
    ("scene_presence",
     "DELETE FROM scene_presence WHERE scene_id IN "
     "(SELECT s.id FROM scenes s JOIN works wk ON wk.id = s.work_id "
     "WHERE wk.world_id = %(w)s)"),
    ("seals", "DELETE FROM seals WHERE world_id = %(w)s"),
    ("findings", "DELETE FROM findings WHERE world_id = %(w)s"),
    ("coverage_notes", "DELETE FROM coverage_notes WHERE world_id = %(w)s"),
    ("share_links", "DELETE FROM share_links WHERE world_id = %(w)s"),
    ("assertions", "DELETE FROM assertions WHERE world_id = %(w)s"),
    ("aliases",
     "DELETE FROM aliases WHERE entity_id IN "
     "(SELECT id FROM entities WHERE world_id = %(w)s)"),
    ("entities", "DELETE FROM entities WHERE world_id = %(w)s"),
    ("scenes",
     "DELETE FROM scenes WHERE work_id IN "
     "(SELECT id FROM works WHERE world_id = %(w)s)"),
    ("works", "DELETE FROM works WHERE world_id = %(w)s"),
    ("world_members", "DELETE FROM world_members WHERE world_id = %(w)s"),
    ("worlds", "DELETE FROM worlds WHERE id = %(w)s"),
]

# Tables landing in parallel workstreams: deleted first (they may reference
# assertions/entities) but only when the migration has actually run.
OPTIONAL_WORLD_DELETES: list[tuple[str, str]] = [
    ("world_rules", "DELETE FROM world_rules WHERE world_id = %(w)s"),  # P3-RULES
    # P3-ENGINE: per-world check-family toggles
    ("world_family_config", "DELETE FROM world_family_config WHERE world_id = %(w)s"),
    # P3-FRONTDOOR: ingest-theater runs — events first (FK to runs), then runs.
    ("pipeline_run_events",
     "DELETE FROM pipeline_run_events WHERE run_id IN "
     "(SELECT id FROM pipeline_runs WHERE world_id = %(w)s)"),
    ("pipeline_runs", "DELETE FROM pipeline_runs WHERE world_id = %(w)s"),
]


def delete_world(cursor, world_id: int) -> dict[str, int]:
    """Delete every row of one world, children first. Returns {table: rows
    deleted}. The caller owns the transaction (commit/rollback)."""

    counts: dict[str, int] = {}
    for table, sql in OPTIONAL_WORLD_DELETES:
        if table_exists(cursor, table):
            cursor.execute(sql, {"w": world_id})
            counts[table] = _rowcount(cursor)
    for table, sql in WORLD_DELETE_ORDER:
        cursor.execute(sql, {"w": world_id})
        counts[table] = counts.get(table, 0) + _rowcount(cursor)
    return counts


def _rowcount(cursor) -> int:
    n = getattr(cursor, "rowcount", 0)
    return n if isinstance(n, int) and n > 0 else 0


# ---------------------------------------------------------------------------
# Account deletion
# ---------------------------------------------------------------------------

# Billing tables (P3-BILLING), landed by 20260703100000_queued_ddl_and_
# attribution.sql. Deletion stays feature-detected — any public table named
# billing_* or stripe_* with a user_id column gets this user's rows deleted —
# so environments that have not run the migration yet still work; the explicit
# list below is the completeness contract (tests/test_trust.py cross-checks it
# against the schema, and asserts the feature-detect patterns cover it).
# The two tables are FK-independent, so the detect's alphabetical order
# (customers, then subscriptions) is safe; both go before profiles.
BILLING_DELETE_TABLES: tuple[str, ...] = ("billing_customers", "billing_subscriptions")

_BILLING_TABLE_SQL = (
    "SELECT c.table_name FROM information_schema.columns c "
    "JOIN information_schema.tables t "
    "ON t.table_schema = c.table_schema AND t.table_name = c.table_name "
    "WHERE c.table_schema = 'public' AND c.column_name = 'user_id' "
    "AND (c.table_name LIKE 'billing%%' OR c.table_name LIKE 'stripe%%') "
    "ORDER BY c.table_name"
)


def solely_owned_world_ids(cursor, user_id: str) -> list[int]:
    """Worlds this account owns outright: worlds.owner_id is this user AND no
    other user holds the 'owner' role via world_members. A world with a second
    owner survives account deletion (only the membership rows go);
    delete_account then reassigns its worlds.owner_id to a surviving owner
    (see _REASSIGN_SURVIVING_OWNERS)."""
    cursor.execute(
        "SELECT w.id FROM worlds w WHERE w.owner_id = %(u)s::uuid "
        "AND NOT EXISTS (SELECT 1 FROM world_members m WHERE m.world_id = w.id "
        "AND m.role = 'owner' AND m.user_id <> %(u)s::uuid) ORDER BY w.id",
        {"u": user_id},
    )
    return [row[0] if not isinstance(row, dict) else row["id"] for row in cursor.fetchall()]


# Co-owned worlds survive account deletion, but their worlds.owner_id would
# dangle at the deleted account. Reassign it to the earliest surviving owner
# in world_members — deterministic: min created_at, tie-break user_id. Runs
# after solely-owned worlds are deleted, so every remaining world with this
# owner_id has (by definition of solely_owned_world_ids) another owner to
# take over. RLS is unaffected either way (canon_world_role checks
# world_members too); this keeps the column truthful.
_REASSIGN_SURVIVING_OWNERS = (
    "UPDATE worlds w SET owner_id = ("
    "SELECT m.user_id FROM world_members m "
    "WHERE m.world_id = w.id AND m.role = 'owner' AND m.user_id <> %(u)s::uuid "
    "ORDER BY m.created_at, m.user_id LIMIT 1) "
    "WHERE w.owner_id = %(u)s::uuid"
)


def delete_account(cursor, user_id: str) -> dict[str, int]:
    """Delete one account: solely-owned worlds (full delete_world each), then
    owner_id handover on surviving co-owned worlds, memberships on those
    worlds, usage history, billing rows (when those tables exist), and the
    profile last. Returns aggregate per-table counts (world content counts
    summed across deleted worlds, plus 'worlds_deleted' and
    'worlds_reassigned'). Caller owns the transaction."""

    counts: dict[str, int] = {}

    owned = solely_owned_world_ids(cursor, user_id)
    for wid in owned:
        for table, n in delete_world(cursor, wid).items():
            counts[table] = counts.get(table, 0) + n
    counts["worlds_deleted"] = len(owned)

    # Surviving co-owned worlds: hand owner_id to the earliest surviving owner
    # BEFORE this user's membership rows go (the subquery excludes the user, so
    # ordering is for clarity, not correctness).
    cursor.execute(_REASSIGN_SURVIVING_OWNERS, {"u": user_id})
    counts["worlds_reassigned"] = _rowcount(cursor)

    # Memberships on worlds that survive (co-owned / member-of): the user
    # leaves, the world stays. Solely-owned worlds' rows are already gone.
    cursor.execute("DELETE FROM world_members WHERE user_id = %(u)s::uuid", {"u": user_id})
    counts["world_members"] = counts.get("world_members", 0) + _rowcount(cursor)

    cursor.execute("DELETE FROM usage_events WHERE user_id = %(u)s::uuid", {"u": user_id})
    counts["usage_events"] = _rowcount(cursor)

    cursor.execute(_BILLING_TABLE_SQL)
    billing_tables = [row[0] if not isinstance(row, dict) else row["table_name"]
                      for row in cursor.fetchall()]
    for table in billing_tables:
        # table names come from information_schema, not user input
        cursor.execute(f"DELETE FROM {table} WHERE user_id = %(u)s::uuid", {"u": user_id})  # noqa: S608
        counts[table] = _rowcount(cursor)

    cursor.execute("DELETE FROM profiles WHERE user_id = %(u)s::uuid", {"u": user_id})
    counts["profiles"] = _rowcount(cursor)
    return counts

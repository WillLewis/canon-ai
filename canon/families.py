"""Per-world deterministic family toggles.

Families cover both SQL check names (for db/checks.sql) and Reader's Report
note families (F1-F4). Missing rows mean enabled; disabled rows are explicit.
"""

from __future__ import annotations

CHECK_FAMILIES = (
    "dead_speaker",
    "presence_conflict",
    "premature_knowledge",
    "destroyed_location_use",
    "capability_violation",
    "dangling_reference",
    "idle_setup",
)
NOTE_FAMILIES = ("F1", "F2", "F3", "F4")
ALL_FAMILIES = CHECK_FAMILIES + NOTE_FAMILIES


def normalize_family(family: str) -> str:
    value = (family or "").strip()
    upper = value.upper()
    if upper in NOTE_FAMILIES:
        return upper
    return value.lower().replace("-", "_")


def load_disabled(conn, world_id: int) -> set[str]:
    cur = conn.cursor()
    try:
        cur.execute(
            "SELECT family FROM world_family_config WHERE world_id = %s AND NOT enabled",
            (world_id,),
        )
        return {normalize_family(r[0]) for r in cur.fetchall()}
    except Exception:
        try:
            conn.rollback()
        except Exception:
            pass
        return set()


def enabled_note_families(conn, world_id: int) -> tuple[str, ...]:
    disabled = load_disabled(conn, world_id)
    return tuple(f for f in NOTE_FAMILIES if f not in disabled)


def set_enabled(conn, world_id: int, family: str, enabled: bool) -> None:
    fam = normalize_family(family)
    cur = conn.cursor()
    cur.execute(
        "INSERT INTO world_family_config (world_id, family, enabled, updated_at) "
        "VALUES (%s, %s, %s, now()) "
        "ON CONFLICT (world_id, family) DO UPDATE SET "
        "enabled = EXCLUDED.enabled, updated_at = now()",
        (world_id, fam, bool(enabled)),
    )
    conn.commit()


def list_config(conn, world_id: int) -> list[dict]:
    disabled = load_disabled(conn, world_id)
    return [
        {
            "family": family,
            "enabled": family not in disabled,
            "kind": "note" if family in NOTE_FAMILIES else "check",
        }
        for family in ALL_FAMILIES
    ]

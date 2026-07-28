"""Pure view-model helpers for the note surface — no database, no generation.

Everything here re-shapes coverage_notes rows (docs/readers-report.md anatomy)
for the templates: family grouping with the caps that are product law, scene
anchoring for the script view's gutter, the draft-2 diff summary computed from
run/status fields, and the per-note action URLs (Show me / Ask). Nothing in
this module writes anything or phrases anything — the engine wrote the rows;
this file only arranges them.
"""

from __future__ import annotations

from typing import Any
from urllib.parse import urlencode

FAMILIES = ("F1", "F2", "F3", "F4")
FAMILY_LABELS = {
    "F1": "Open Questions",
    "F2": "Idle Setups",
    "F3": "Dormant Knowledge",
    "F4": "Unmotivated Turns",
}
# Caps are product law (report body <= 2 pages; see docs/readers-report.md).
FAMILY_CAPS = {"F1": 12, "F2": 8, "F3": 6, "F4": 5}
FINDINGS_CAP = 10           # continuity findings shown inline before "see all"
LOAD_BEARING_CAP = 6

OPEN_STATUSES = {"open"}
SETTLED_STATUSES = {"sealed", "dismissed"}


def note_citations(note: dict[str, Any]) -> list[dict[str, Any]]:
    evidence = note.get("evidence") or {}
    return [c for c in (evidence.get("citations") or []) if c.get("scene_id")]


def note_scene_ids(note: dict[str, Any]) -> list[int]:
    """Scenes this note anchors to: cited scenes first, then anchor scenes."""
    evidence = note.get("evidence") or {}
    out: list[int] = []
    for c in note_citations(note):
        sid = c.get("scene_id")
        if sid is not None and sid not in out:
            out.append(sid)
    for sid in evidence.get("anchor_scene_ids") or []:
        if sid is not None and sid not in out:
            out.append(sid)
    return out


def note_entity_ids(note: dict[str, Any]) -> list[int]:
    evidence = note.get("evidence") or {}
    return [i for i in (evidence.get("anchor_entity_ids") or []) if i]


def split_notes(notes: list[dict[str, Any]]) -> dict[str, Any]:
    """Group notes for the report: open by family (salience-ranked + capped),
    settled (sealed/dismissed — the permanent footer), addressed (diff fuel)."""
    open_by_family: dict[str, list[dict[str, Any]]] = {f: [] for f in FAMILIES}
    settled: list[dict[str, Any]] = []
    addressed: list[dict[str, Any]] = []
    overflow = 0
    for n in notes:
        status = n.get("status") or "open"
        if status in SETTLED_STATUSES:
            settled.append(n)
        elif status == "addressed":
            addressed.append(n)
        elif n.get("family") in open_by_family:
            open_by_family[n["family"]].append(n)
    for family, rows in open_by_family.items():
        rows.sort(key=lambda n: (-(n.get("salience") or 0), n.get("note_key") or ""))
        overflow += max(0, len(rows) - FAMILY_CAPS[family])
        open_by_family[family] = rows[: FAMILY_CAPS[family]]
    settled.sort(key=lambda n: (n.get("family") or "", -(n.get("salience") or 0)))
    return {
        "open_by_family": open_by_family,
        "n_open": sum(len(v) for v in open_by_family.values()),
        "overflow": overflow,
        "settled": settled,
        "addressed": addressed,
    }


def notes_by_scene(notes: list[dict[str, Any]]) -> dict[int, list[dict[str, Any]]]:
    """scene_id -> open notes anchored there (gutter markers + right pane)."""
    out: dict[int, list[dict[str, Any]]] = {}
    for n in notes:
        if (n.get("status") or "open") not in OPEN_STATUSES:
            continue
        for sid in note_scene_ids(n):
            out.setdefault(sid, []).append(n)
    for rows in out.values():
        rows.sort(key=lambda n: (-(n.get("salience") or 0), n.get("note_key") or ""))
    return out


def show_me_url(world_id: int, note: dict[str, Any]) -> str | None:
    """Jump to the first cited scene in the script view, quote highlighted."""
    citations = note_citations(note)
    if not citations:
        sids = note_scene_ids(note)
        if not sids:
            return None
        return f"/worlds/{world_id}/script?{urlencode({'scene': sids[0]})}#scene-{sids[0]}"
    c = citations[0]
    q = urlencode({"scene": c["scene_id"], "quote": c.get("quote") or ""})
    return f"/worlds/{world_id}/script?{q}#scene-{c['scene_id']}"


def ask_seed(note: dict[str, Any], names_by_id: dict[int, str]) -> str:
    """Pre-fill for the ask pane, scoped to the note's first anchored entity.

    Uses one of the engine's own Phase 0 templates ("what happened to X") so the
    seed is answerable; the writer edits freely. Nothing is generated — the
    entity name comes straight from the graph.
    """
    for eid in note_entity_ids(note):
        name = names_by_id.get(eid)
        if name:
            return f"what happened to {name}"
    return ""


def decorate_notes(world_id: int, notes: list[dict[str, Any]],
                   names_by_id: dict[int, str] | None = None) -> None:
    """Attach per-note view fields in place: show_url, ask_seed, scene_ids."""
    names_by_id = names_by_id or {}
    for n in notes:
        n["show_url"] = show_me_url(world_id, n)
        n["ask_seed"] = ask_seed(n, names_by_id)
        n["scene_ids"] = note_scene_ids(n)
        n["citations"] = note_citations(n)


def all_entity_ids(notes: list[dict[str, Any]]) -> list[int]:
    seen: list[int] = []
    for n in notes:
        for eid in note_entity_ids(n):
            if eid not in seen:
                seen.append(eid)
    return seen


def diff_summary(notes: list[dict[str, Any]]) -> dict[str, Any]:
    """The draft-2 moment, computed from coverage_notes lifecycle fields.

    - resolved: status='addressed' — a later run found the gap closed (set by
      the engine, never claimed by the writer).
    - new: open notes whose first_seen_run == last_seen_run == the current run
      (the run stamped on the most recently updated note).
    - sealed / dismissed: the permanent rulings, counted since forever — they
      never re-raise, so "since the previous run" and "ever" agree per note_key.
    """
    current_run = None
    latest = None
    for n in notes:
        stamp = n.get("updated_at") or n.get("created_at")
        if n.get("last_seen_run") and (latest is None or (stamp is not None and stamp > latest)):
            latest = stamp
            current_run = n.get("last_seen_run")
    resolved = [n for n in notes if n.get("status") == "addressed"]
    sealed = [n for n in notes if n.get("status") == "sealed"]
    dismissed = [n for n in notes if n.get("status") == "dismissed"]
    new = [
        n for n in notes
        if (n.get("status") or "open") in OPEN_STATUSES
        and current_run is not None
        and n.get("first_seen_run") == current_run
        and n.get("last_seen_run") == current_run
    ]
    banner = (
        f"{len(resolved)} note{'s' if len(resolved) != 1 else ''} resolved (addressed)"
        f" · {len(new)} new · {len(sealed)} sealed"
    )
    return {
        "current_run": current_run,
        "resolved": resolved,
        "new": new,
        "sealed": sealed,
        "dismissed": dismissed,
        "banner": banner,
    }

"""Series bible assembly — assertion graph -> structured bible document.

Pure deterministic SQL + Python. Zero LLM calls. The bible contains headings,
structured facts, and verbatim quotes only — no prose generation, no loglines,
no synopsis (CLAUDE.md: Canon never writes the user's story).

Citation law: every fact line carries its scene citation (work/scene label).
A fact whose establishing scene cannot be resolved is OMITTED and counted in
`omitted_uncited` — it is never rendered without a citation and never invented.

Status policy (documented, tested):
  - 'retconned' and 'rejected' assertions never appear — they are not canon.
  - 'sealed' assertions DO appear: a seal means the writer marked the fact
    intentional, which makes it stronger canon, not suppressed canon. Lines
    are annotated with the status when it is not plain 'canon'.
  - 'draft' assertions appear annotated as unconfirmed, so the paid artifact
    never passes an unreviewed extraction off as settled canon.

Open questions reuse the hole-finder machinery (canon/holes.py + db/holes.sql);
this module only groups the already-cited holes into a bible section.
"""

from __future__ import annotations

from collections import Counter
from typing import Any

from ..holes import group_by_category, run_holes
from ..report import RELATION_PREDICATES

# Statuses that never appear in a bible (not canon).
EXCLUDED_STATUSES = ("retconned", "rejected")

KNOWLEDGE_PREDICATES = ("knows", "believes")
GOAL_PREDICATES = ("goal", "promised")
RULE_PREDICATES = ("cannot", "trait")
LOCATION_FACT_PREDICATES = ("trait", "fact", "cannot")


def _dict_rows(cur) -> list[dict[str, Any]]:
    cols = [d[0] for d in cur.description]
    return [dict(zip(cols, r)) for r in cur.fetchall()]


def _scene_label(scene: dict[str, Any]) -> str:
    label = scene.get("label")
    if label:
        return str(label)
    work = str(scene.get("work_title") or "?").split()[0]
    return f"{work}/sc{scene.get('scene_index')}"


def load_bible_source(conn, world_id: int) -> dict[str, Any]:
    """Read every row the bible needs from Postgres. Deterministic SQL only."""

    cur = conn.cursor()
    cur.execute("SELECT id, name FROM worlds WHERE id = %s", (world_id,))
    row = cur.fetchone()
    world_name = row[1] if row else str(world_id)

    cur.execute(
        "SELECT id, title, sort_order, source_file FROM works "
        "WHERE world_id = %s ORDER BY sort_order, id",
        (world_id,),
    )
    works = _dict_rows(cur)

    cur.execute(
        "SELECT s.id, w.title AS work_title, s.slug, s.story_position, s.is_flashback,"
        "       row_number() OVER (PARTITION BY s.work_id ORDER BY s.story_position) AS scene_index "
        "FROM scenes s JOIN works w ON w.id = s.work_id "
        "WHERE w.world_id = %s ORDER BY s.story_position, s.id",
        (world_id,),
    )
    scenes = _dict_rows(cur)
    for s in scenes:
        s["label"] = _scene_label(s)

    cur.execute(
        "SELECT e.id, e.kind::text AS kind, e.name, e.provisional,"
        "       coalesce(array_agg(al.alias ORDER BY al.alias)"
        "                FILTER (WHERE al.alias IS NOT NULL), '{}') AS aliases "
        "FROM entities e LEFT JOIN aliases al ON al.entity_id = e.id "
        "WHERE e.world_id = %s GROUP BY e.id, e.kind, e.name, e.provisional "
        "ORDER BY e.name",
        (world_id,),
    )
    entities = _dict_rows(cur)

    cur.execute(
        "SELECT a.id, a.subject_id, subj.name AS subject_name, subj.kind::text AS subject_kind,"
        "       a.predicate, a.object_id, obj.name AS object_name, obj.kind::text AS object_kind,"
        "       a.object_value, a.object_assertion_id, a.polarity,"
        "       lower(a.valid_during) AS start_pos, upper(a.valid_during) AS end_pos,"
        "       a.established_in_scene AS scene_id, a.supporting_quote,"
        "       a.confidence, a.status::text AS status, s.story_position "
        "FROM assertions a "
        "JOIN entities subj ON subj.id = a.subject_id "
        "LEFT JOIN entities obj ON obj.id = a.object_id "
        "JOIN scenes s ON s.id = a.established_in_scene "
        "JOIN works w ON w.id = s.work_id "
        "WHERE a.world_id = %s AND a.status::text NOT IN ('retconned','rejected') "
        "ORDER BY s.story_position, a.id",
        (world_id,),
    )
    assertions = _dict_rows(cur)

    cur.execute(
        "SELECT sp.scene_id, sp.entity_id "
        "FROM scene_presence sp "
        "JOIN scenes s ON s.id = sp.scene_id "
        "JOIN works w ON w.id = s.work_id "
        "WHERE w.world_id = %s ORDER BY s.story_position",
        (world_id,),
    )
    presence = _dict_rows(cur)

    try:
        conn.rollback()  # read-only; release any implicit transaction
    except Exception:
        pass
    return {
        "world_id": world_id,
        "world_name": world_name,
        "works": works,
        "scenes": scenes,
        "entities": entities,
        "assertions": assertions,
        "presence": presence,
    }


# ---------------------------------------------------------------------------
# Pure assembly (fake-row testable; no database, no LLM)
# ---------------------------------------------------------------------------

def _assertion_object(a: dict[str, Any]) -> str:
    return str(a.get("object_name") or a.get("object_value") or "").strip()


def _phrase(a: dict[str, Any]) -> str:
    neg = "not " if a.get("polarity") is False else ""
    obj = _assertion_object(a)
    return f"{a.get('subject_name')} — {neg}{a.get('predicate')}" + (f": {obj}" if obj else "")


def _line(a: dict[str, Any], scene_by_id: dict[int, dict], text: str) -> dict[str, Any] | None:
    """One cited fact line, or None when the citation cannot be resolved."""

    scene = scene_by_id.get(a.get("scene_id"))
    if not scene:
        return None  # a fact without a citation is omitted, never invented
    return {
        "text": text,
        "scene_id": scene["id"],
        "label": _scene_label(scene),
        "quote": (a.get("supporting_quote") or "").strip(),
        "status": a.get("status") or "canon",
        "position": a.get("story_position"),
    }


def _appearances(entity_id: int, source: dict, scene_by_id: dict) -> tuple[dict | None, dict | None]:
    """(first, last) appearance scenes from presence rows, falling back to
    the scenes of assertions that mention the entity."""

    scenes = [
        scene_by_id[p["scene_id"]]
        for p in source["presence"]
        if p["entity_id"] == entity_id and p["scene_id"] in scene_by_id
    ]
    if not scenes:
        scenes = [
            scene_by_id[a["scene_id"]]
            for a in source["assertions"]
            if entity_id in (a.get("subject_id"), a.get("object_id"))
            and a.get("scene_id") in scene_by_id
        ]
    if not scenes:
        return None, None
    ordered = sorted(scenes, key=lambda s: (s.get("story_position") or 0, s["id"]))
    fmt = lambda s: {"label": _scene_label(s), "position": s.get("story_position")}  # noqa: E731
    return fmt(ordered[0]), fmt(ordered[-1])


def _learned_at(a: dict[str, Any]) -> int | None:
    if a.get("start_pos") is not None:
        return a["start_pos"]
    return a.get("story_position")


def _knowledge_text(a: dict[str, Any], assertion_by_id: dict[int, dict]) -> str | None:
    fact = _assertion_object(a)
    if not fact and a.get("object_assertion_id") is not None:
        ref = assertion_by_id.get(a["object_assertion_id"])
        if ref is None:
            return None  # target assertion is not canon; never invent the fact
        fact = _phrase(ref)
    if not fact:
        return None
    verb = "knows" if a.get("predicate") == "knows" else "believes"
    if a.get("polarity") is False:
        verb = f"does not {verb.rstrip('s')}" if verb == "knows" else "does not believe"
    learned = _learned_at(a)
    when = f" (learned at pos {learned})" if learned is not None else ""
    return f"{verb.capitalize()}: {fact}{when}"


def assemble_bible(source: dict[str, Any], holes: list | None = None) -> dict[str, Any]:
    """Structured bible from loaded rows. Pure function; deterministic."""

    scene_by_id = {s["id"]: s for s in source["scenes"]}
    entity_by_id = {e["id"]: e for e in source["entities"]}
    assertions = [a for a in source["assertions"] if a.get("status") not in EXCLUDED_STATUSES]
    assertion_by_id = {a["id"]: a for a in assertions}
    omitted = 0

    def cited(a: dict[str, Any], text: str) -> dict[str, Any] | None:
        nonlocal omitted
        line = _line(a, scene_by_id, text)
        if line is None:
            omitted += 1
        return line

    def cited_all(rows: list[dict[str, Any]], text_fn) -> list[dict[str, Any]]:
        out = []
        for a in rows:
            text = text_fn(a)
            if text is None:
                continue
            line = cited(a, text)
            if line is not None:
                out.append(line)
        return out

    by_subject: dict[int, list[dict[str, Any]]] = {}
    for a in assertions:
        by_subject.setdefault(a["subject_id"], []).append(a)

    # --- title page / overview (corpus stats — not story claims) -----------
    positions = [s.get("story_position") for s in source["scenes"] if s.get("story_position") is not None]
    span = None
    if source["scenes"]:
        ordered = sorted(source["scenes"], key=lambda s: (s.get("story_position") or 0, s["id"]))
        span = {
            "first_label": _scene_label(ordered[0]),
            "last_label": _scene_label(ordered[-1]),
            "positions": [min(positions), max(positions)] if positions else None,
        }
    overview = {
        "works": len(source["works"]),
        "work_titles": [w.get("title") for w in source["works"]],
        "scenes": len(source["scenes"]),
        "entities": len(source["entities"]),
        "entity_kinds": dict(Counter(e.get("kind") for e in source["entities"])),
        "assertions": len(assertions),
        "assertion_statuses": dict(Counter(a.get("status") for a in assertions)),
        "span": span,
    }

    # --- characters ---------------------------------------------------------
    characters = []
    for e in sorted(source["entities"], key=lambda e: str(e.get("name") or "")):
        if e.get("kind") != "character" or e.get("provisional"):
            continue  # provisional refs live in Open Questions, never as canon pages
        rows = by_subject.get(e["id"], [])
        first, last = _appearances(e["id"], source, scene_by_id)
        characters.append({
            "name": e["name"],
            "aliases": list(e.get("aliases") or []),
            "first_appearance": first,
            "last_appearance": last,
            "traits": cited_all(
                [a for a in rows if a.get("predicate") == "trait"],
                lambda a: f"Trait: {_assertion_object(a)}" if _assertion_object(a) else None,
            ),
            "occupation": cited_all(
                [a for a in rows if a.get("predicate") == "occupation"],
                lambda a: f"Occupation: {_assertion_object(a)}" if _assertion_object(a) else None,
            ),
            "relationships": cited_all(
                [a for a in rows if a.get("predicate") in RELATION_PREDICATES],
                lambda a: (
                    f"{'Not ' if a.get('polarity') is False else ''}{a['predicate'].replace('_', ' ')}: "
                    f"{_assertion_object(a)}"
                ) if _assertion_object(a) else None,
            ),
            "goals": cited_all(
                [a for a in rows if a.get("predicate") in GOAL_PREDICATES],
                lambda a: f"{a['predicate'].capitalize()}: {_assertion_object(a)}" if _assertion_object(a) else None,
            ),
            "knowledge": cited_all(
                [a for a in rows if a.get("predicate") in KNOWLEDGE_PREDICATES],
                lambda a: _knowledge_text(a, assertion_by_id),
            ),
        })

    # --- locations ----------------------------------------------------------
    locations = []
    for e in sorted(source["entities"], key=lambda e: str(e.get("name") or "")):
        if e.get("kind") != "location" or e.get("provisional"):
            continue
        rows = by_subject.get(e["id"], [])
        destroyed = None
        for a in rows:
            if a.get("predicate") == "destroyed" and a.get("polarity") is not False:
                destroyed = cited(a, f"Destroyed at pos {a.get('story_position')}")
                break
        locations.append({
            "name": e["name"],
            "aliases": list(e.get("aliases") or []),
            "destroyed": destroyed,
            "facts": cited_all(
                [a for a in rows if a.get("predicate") in LOCATION_FACT_PREDICATES],
                lambda a: f"{a['predicate'].capitalize()}: {_assertion_object(a)}" if _assertion_object(a) else None,
            ),
        })

    # --- timeline: every canon assertion in story order ----------------------
    timeline = []
    for a in sorted(assertions, key=lambda a: (a.get("story_position") or 0, a["id"])):
        line = cited(a, _phrase(a))
        if line is not None:
            timeline.append(line)

    # --- world rules: cannot facts + non-character traits --------------------
    rules = []
    for a in assertions:
        pred = a.get("predicate")
        subj_kind = a.get("subject_kind")
        if pred == "cannot" or (pred == "trait" and subj_kind != "character"):
            line = cited(a, _phrase(a))
            if line is not None:
                rules.append(line)

    # --- open questions: hole-finder output, already scene-cited -------------
    open_questions = []
    for _key, label, blurb, items in group_by_category(holes or []):
        cited_items = [
            {"question": h.get("question"), "detail": h.get("detail"), "label": h.get("cite")}
            for h in items
            if h.get("cite")  # citation law applies to questions too
        ]
        omitted += sum(1 for h in items if not h.get("cite"))
        if cited_items:
            open_questions.append({"label": label, "blurb": blurb, "items": cited_items})

    return {
        "world_id": source.get("world_id"),
        "world_name": source.get("world_name"),
        "overview": overview,
        "characters": characters,
        "locations": locations,
        "timeline": timeline,
        "world_rules": rules,
        "open_questions": open_questions,
        "omitted_uncited": omitted,
    }


def build_bible(conn, world_id: int, *, holes_sql: str | None = None) -> tuple[dict[str, Any], list]:
    """Load rows, run the hole-finder (when given its SQL), assemble.

    Returns (bible, hole_errors). No LLM anywhere on this path.
    """

    source = load_bible_source(conn, world_id)
    holes: list = []
    errors: list = []
    if holes_sql:
        holes, errors = run_holes(conn, world_id, holes_sql)
    return assemble_bible(source, holes), errors

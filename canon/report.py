"""Reader's Report engine v0.

The report layer follows docs/readers-report.md: deterministic candidate
queries first, LLM selection/phrasing second, citation validation last. The LLM
is never allowed to create a candidate; it can only return notes tied to
candidate ids produced here.
"""

from __future__ import annotations

import hashlib
import json
import re
import uuid
from collections import Counter, defaultdict
from dataclasses import dataclass
from typing import Any

from . import check
from . import families as family_config
from .extract import DEFAULT_EFFORT, DEFAULT_MODEL, first_text, quote_in_text

FAMILIES = ("F1", "F2", "F3", "F4")
FAMILY_LABELS = {
    "F1": "Open Questions",
    "F2": "Idle Setups",
    "F3": "Dormant Knowledge",
    "F4": "Unmotivated Turns",
}
FAMILY_CAPS = {"F1": 12, "F2": 8, "F3": 6, "F4": 5}
INVALID_ASSERTION_STATUSES = {"sealed", "retconned", "rejected"}
SUPPRESSED_NOTE_STATUSES = {"sealed", "dismissed"}
ACTIVE_NOTE_STATUSES = {"open"}
ADDRESSED_NOTE_STATUS = "addressed"
BODY_LINE_LIMIT = 100

RELATION_PREDICATES = {
    "member_of",
    "married_to",
    "parent_of",
    "sibling_of",
    "romantic_with",
    "allied_with",
    "enemy_of",
}
SETUP_PREDICATES = {"promised", "goal", "secret_of", "created"}
KNOWLEDGE_PREDICATES = {"knows", "believes"}
MOTIVATION_PREDICATES = {
    "goal",
    "promised",
    "trait",
    "cannot",
    "knows",
    "believes",
    "secret_of",
    "enemy_of",
    "allied_with",
    "romantic_with",
    "occupation",
}
TURN_PREDICATES = {
    "promised",
    "goal",
    "created",
    "possesses",
    "dies",
    "destroyed",
    "fact",
    "enemy_of",
    "allied_with",
    "romantic_with",
}
STOP_WORDS = {
    "a",
    "an",
    "and",
    "as",
    "at",
    "by",
    "for",
    "from",
    "he",
    "her",
    "his",
    "in",
    "is",
    "it",
    "of",
    "on",
    "or",
    "she",
    "that",
    "the",
    "their",
    "there",
    "they",
    "this",
    "to",
    "was",
    "with",
    "you",
}


@dataclass(frozen=True)
class ReportLog:
    level: str
    message: str
    note_key: str | None = None
    candidate_id: str | None = None


def norm_text(value: Any) -> str:
    return re.sub(r"\s+", " ", re.sub(r"[^a-z0-9 ]", " ", str(value or "").lower())).strip()


def _tokens(value: Any) -> set[str]:
    return {t for t in norm_text(value).split() if t and t not in STOP_WORDS}


def _dict_rows(cur) -> list[dict[str, Any]]:
    cols = [d[0] for d in cur.description]
    return [dict(zip(cols, r)) for r in cur.fetchall()]


def _scene_label(scene: dict[str, Any]) -> str:
    return scene.get("label") or f"{str(scene.get('work_title') or '?').split()[0]}/sc{scene.get('scene_index')}"


def _assertion_object(a: dict[str, Any]) -> str:
    return str(a.get("object_name") or a.get("object_value") or a.get("object_assertion_id") or "").strip()


def _assertion_phrase(a: dict[str, Any]) -> str:
    obj = _assertion_object(a)
    neg = "not " if a.get("polarity") is False else ""
    return f"{a.get('subject_name')} {neg}{a.get('predicate')} {obj}".strip()


def assertion_signature(a: dict[str, Any]) -> str:
    """Semantic assertion key stable across graph reloads.

    The database assertion id still ships in evidence, but persistence needs a
    key that survives `canon store --reset`, which recreates assertion ids.
    """

    parts = [
        a.get("subject_name"),
        a.get("subject_kind"),
        a.get("predicate"),
        a.get("object_name"),
        a.get("object_kind"),
        a.get("object_value"),
        _scene_label(a.get("scene") or {}),
        norm_text(a.get("supporting_quote")),
    ]
    return "|".join(str(p or "") for p in parts)


def note_key_for(candidate: dict[str, Any], world_id: int | None = None) -> str:
    payload = {
        "world": world_id,
        "family": candidate["family"],
        "assertions": sorted(candidate.get("anchor_assertion_keys") or []),
        "entities": sorted(candidate.get("anchor_entity_keys") or []),
        "scenes": sorted(candidate.get("anchor_scene_keys") or []),
        "discriminator": norm_text(candidate.get("discriminator") or ""),
    }
    digest = hashlib.sha256(json.dumps(payload, sort_keys=True).encode("utf-8")).hexdigest()
    return f"{candidate['family'].lower()}:{digest[:24]}"


def candidate_id_for(candidate: dict[str, Any], world_id: int | None = None) -> str:
    return note_key_for(candidate, world_id=world_id)


def load_world_snapshot(conn, world_id: int) -> dict[str, Any]:
    """Read the graph rows the report engine needs from Postgres."""

    cur = conn.cursor()
    cur.execute(
        "SELECT s.id, w.title AS work_title, w.sort_order, s.slug, s.story_position,"
        "       s.is_flashback, s.raw_text,"
        "       row_number() OVER (PARTITION BY s.work_id ORDER BY s.story_position) AS scene_index "
        "FROM scenes s JOIN works w ON w.id = s.work_id "
        "WHERE w.world_id = %s ORDER BY s.story_position, s.id",
        (world_id,),
    )
    scenes = _dict_rows(cur)
    for s in scenes:
        s["label"] = f"{str(s['work_title'] or '?').split()[0]}/sc{s['scene_index']}"

    cur.execute(
        "SELECT e.id, e.kind::text AS kind, e.name, e.provisional,"
        "       coalesce(array_agg(al.alias ORDER BY al.alias) FILTER (WHERE al.alias IS NOT NULL), '{}') AS aliases "
        "FROM entities e LEFT JOIN aliases al ON al.entity_id = e.id "
        "WHERE e.world_id = %s GROUP BY e.id, e.kind, e.name, e.provisional "
        "ORDER BY e.name",
        (world_id,),
    )
    entities = _dict_rows(cur)

    cur.execute(
        "SELECT a.id, a.world_id, a.subject_id, subj.name AS subject_name, subj.kind::text AS subject_kind,"
        "       a.predicate, a.object_id, obj.name AS object_name, obj.kind::text AS object_kind,"
        "       a.object_value, a.object_assertion_id, a.polarity,"
        "       lower(a.valid_during) AS start_pos, upper(a.valid_during) AS end_pos,"
        "       lower_inf(a.valid_during) AS start_inf, upper_inf(a.valid_during) AS end_inf,"
        "       a.established_in_scene AS scene_id, a.supporting_quote, a.confidence, a.status::text AS status,"
        "       s.story_position, s.slug, w.title AS work_title,"
        "       row_number() OVER (PARTITION BY s.work_id ORDER BY s.story_position) AS scene_index "
        "FROM assertions a "
        "JOIN entities subj ON subj.id = a.subject_id "
        "LEFT JOIN entities obj ON obj.id = a.object_id "
        "JOIN scenes s ON s.id = a.established_in_scene "
        "JOIN works w ON w.id = s.work_id "
        "WHERE a.world_id = %s AND a.status::text NOT IN ('sealed','retconned','rejected') "
        "ORDER BY s.story_position, a.id",
        (world_id,),
    )
    assertions = _dict_rows(cur)
    scene_by_id = {s["id"]: s for s in scenes}
    for a in assertions:
        scene = scene_by_id.get(a["scene_id"])
        if scene:
            a["scene"] = scene
        else:
            a["scene"] = {
                "id": a["scene_id"],
                "work_title": a.get("work_title"),
                "slug": a.get("slug"),
                "story_position": a.get("story_position"),
                "scene_index": a.get("scene_index"),
                "label": f"{str(a.get('work_title') or '?').split()[0]}/sc{a.get('scene_index')}",
            }
        a["signature"] = assertion_signature(a)

    cur.execute(
        "SELECT sp.scene_id, sp.entity_id, e.name, e.kind::text AS kind "
        "FROM scene_presence sp "
        "JOIN scenes s ON s.id = sp.scene_id "
        "JOIN works w ON w.id = s.work_id "
        "JOIN entities e ON e.id = sp.entity_id "
        "WHERE w.world_id = %s ORDER BY s.story_position, e.name",
        (world_id,),
    )
    presence = _dict_rows(cur)

    try:
        conn.rollback()
    except Exception:
        pass
    return {
        "world_id": world_id,
        "scenes": scenes,
        "scene_by_id": scene_by_id,
        "entities": entities,
        "entity_by_id": {e["id"]: e for e in entities},
        "assertions": assertions,
        "assertion_by_id": {a["id"]: a for a in assertions},
        "presence": presence,
    }


def age_threshold(snapshot: dict[str, Any], override: int | None = None) -> int:
    if override is not None:
        return int(override)
    by_work = Counter(s["work_title"] for s in snapshot.get("scenes", []))
    return max(15, max(by_work.values() or [0]))


def _citation_from_assertion(a: dict[str, Any]) -> dict[str, Any]:
    scene = a.get("scene") or {}
    return {
        "assertion_id": a["id"],
        "scene_id": a["scene_id"],
        "quote": a.get("supporting_quote") or "",
        "label": _scene_label(scene),
    }


def _citation_from_scene(scene: dict[str, Any], quote: str) -> dict[str, Any]:
    return {
        "assertion_id": None,
        "scene_id": scene["id"],
        "quote": quote,
        "label": _scene_label(scene),
    }


def _first_scene_quote(scene: dict[str, Any]) -> str:
    for line in (scene.get("raw_text") or "").splitlines():
        stripped = line.strip()
        if len(stripped) >= 8:
            return stripped[:240]
    return (scene.get("raw_text") or "").strip()[:240]


def _base_candidate(
    family: str,
    kind: str,
    *,
    world_id: int | None = None,
    assertions: list[dict[str, Any]] | None = None,
    scenes: list[dict[str, Any]] | None = None,
    entities: list[dict[str, Any]] | None = None,
    discriminator: str = "",
    summary_seed: str,
    body_seed: str,
    salience: int = 0,
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    assertions = assertions or []
    scenes = scenes or []
    entities = entities or []
    candidate = {
        "family": family,
        "kind": kind,
        "candidate_id": "",
        "note_key": "",
        "summary_seed": summary_seed,
        "body_seed": body_seed,
        "salience": int(salience),
        "anchor_assertion_ids": [a["id"] for a in assertions],
        "anchor_assertion_keys": [a.get("signature") or assertion_signature(a) for a in assertions],
        "anchor_scene_ids": [s["id"] for s in scenes],
        "anchor_scene_keys": [_scene_label(s) for s in scenes],
        "anchor_entity_ids": [e["id"] for e in entities],
        "anchor_entity_keys": [f"{e.get('kind')}:{e.get('name')}" for e in entities],
        "discriminator": discriminator,
        "evidence": {
            "citations": [_citation_from_assertion(a) for a in assertions if a.get("supporting_quote")],
        },
    }
    if extra:
        candidate.update(extra)
    key = note_key_for(candidate, world_id=world_id)
    candidate["note_key"] = key
    candidate["candidate_id"] = key
    return candidate


def _dedupe_candidates(candidates: list[dict[str, Any]]) -> list[dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {}
    for c in candidates:
        key = c["note_key"]
        if key not in out or c.get("salience", 0) > out[key].get("salience", 0):
            out[key] = c
    return sorted(out.values(), key=lambda c: (-c.get("salience", 0), c["candidate_id"]))


def _later_assertions(snapshot: dict[str, Any], pos: int, exclude_id: int | None = None) -> list[dict[str, Any]]:
    return [
        a
        for a in snapshot["assertions"]
        if a["id"] != exclude_id and (a.get("story_position") or 0) > pos
    ]


def _mentions_entity_or_value(row: dict[str, Any], entity_ids: set[int], terms: set[str]) -> bool:
    if entity_ids and (row.get("subject_id") in entity_ids or row.get("object_id") in entity_ids):
        return True
    if terms and (_tokens(row.get("object_value")) & terms):
        return True
    if terms and (_tokens(row.get("supporting_quote")) & terms):
        return True
    return False


def _thread_terms(a: dict[str, Any]) -> set[str]:
    terms = _tokens(a.get("object_value"))
    if a.get("object_name"):
        terms |= _tokens(a.get("object_name"))
    # "find Danny" should track Danny, not the generic verb.
    terms -= {"find", "discover", "locate", "learn", "get", "make"}
    return terms


def _has_downstream_reference(snapshot: dict[str, Any], a: dict[str, Any]) -> bool:
    entity_ids = {i for i in (a.get("object_id"),) if i}
    terms = _thread_terms(a)
    for later in _later_assertions(snapshot, a.get("story_position") or 0, exclude_id=a["id"]):
        if _mentions_entity_or_value(later, entity_ids, terms):
            return True
    return False


def collect_idle_setup_candidates(snapshot: dict[str, Any], *, threshold: int | None = None) -> list[dict[str, Any]]:
    """F2: pure SQL-shaped setup candidates with no downstream reference."""

    max_pos = max((s["story_position"] for s in snapshot["scenes"]), default=0)
    n = age_threshold(snapshot, threshold)
    candidates: list[dict[str, Any]] = []
    for a in snapshot["assertions"]:
        if a.get("predicate") not in SETUP_PREDICATES:
            continue
        pos = a.get("story_position") or 0
        if max_pos - pos < n:
            continue
        if _has_downstream_reference(snapshot, a):
            continue
        subject = a.get("subject_name") or "Unknown"
        obj = _assertion_object(a) or a.get("predicate")
        scene = a.get("scene") or {}
        salience = (max_pos - pos) * 10 + (20 if a.get("predicate") in {"promised", "goal"} else 0)
        candidates.append(
            _base_candidate(
                "F2",
                "idle_setup",
                world_id=snapshot.get("world_id"),
                assertions=[a],
                discriminator=f"{a.get('predicate')}:{subject}:{obj}",
                summary_seed=f"{subject}'s {a.get('predicate')} around {obj} has no later reference.",
                body_seed=(
                    f"Set up in {_scene_label(scene)} and untouched after "
                    f"{max_pos - pos} story positions. Last touch is the setup scene."
                ),
                salience=salience,
                extra={
                    "setup_age": max_pos - pos,
                    "last_touch_scene_id": a["scene_id"],
                    "last_touch_label": _scene_label(scene),
                },
            )
        )
    return _dedupe_candidates(candidates)


def _entity_terms(snapshot: dict[str, Any], entity: dict[str, Any]) -> set[str]:
    terms = _tokens(entity.get("name"))
    for alias in entity.get("aliases") or []:
        terms |= _tokens(alias)
    return terms


def _fact_concern_entities(snapshot: dict[str, Any], a: dict[str, Any]) -> list[dict[str, Any]]:
    text_terms = _tokens(a.get("object_value")) | _tokens(a.get("supporting_quote"))
    hits = []
    for e in snapshot["entities"]:
        if e["id"] == a.get("subject_id"):
            continue
        if _entity_terms(snapshot, e) & text_terms:
            hits.append(e)
    return hits


def collect_open_question_candidates(
    snapshot: dict[str, Any],
    *,
    threshold: int | None = None,
    scene_open_questions: list[dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    """F1: extraction open_questions plus conservative structural gaps."""

    n = age_threshold(snapshot, threshold)
    max_pos = max((s["story_position"] for s in snapshot["scenes"]), default=0)
    candidates: list[dict[str, Any]] = []

    for row in scene_open_questions or []:
        scene = snapshot["scene_by_id"].get(row.get("scene_id"))
        question = (row.get("question") or "").strip()
        if not scene or not question:
            continue
        quote = row.get("quote") or _first_scene_quote(scene)
        c = _base_candidate(
            "F1",
            "extraction_open_question",
            world_id=snapshot.get("world_id"),
            scenes=[scene],
            discriminator=question,
            summary_seed=question if question.endswith("?") else question + "?",
            body_seed="Raised by extraction as a scene-level open question.",
            salience=60 + (max_pos - scene["story_position"]),
            extra={"question": question},
        )
        c["evidence"]["citations"].append(_citation_from_scene(scene, quote))
        candidates.append(c)

    # Provisional entities: named references with no established entity.
    first_ref_by_entity: dict[int, dict[str, Any]] = {}
    for a in snapshot["assertions"]:
        for eid in (a.get("subject_id"), a.get("object_id")):
            ent = snapshot["entity_by_id"].get(eid)
            if not ent or not ent.get("provisional"):
                continue
            current = first_ref_by_entity.get(eid)
            if current is None or a["story_position"] < current["story_position"]:
                first_ref_by_entity[eid] = a
    for eid, a in first_ref_by_entity.items():
        ent = snapshot["entity_by_id"][eid]
        candidates.append(
            _base_candidate(
                "F1",
                "unknown_entity",
                world_id=snapshot.get("world_id"),
                assertions=[a],
                entities=[ent],
                discriminator=f"unknown:{ent['name']}",
                summary_seed=f"Who or what is {ent['name']}?",
                body_seed="The graph has a referenced provisional entity but no established canon row resolving it.",
                salience=90,
            )
        )

    # Goals/secrets that are old and not closed by any later reference.
    for a in snapshot["assertions"]:
        pred = a.get("predicate")
        if pred not in {"goal", "promised", "secret_of"}:
            continue
        pos = a.get("story_position") or 0
        if max_pos - pos < n:
            continue
        if _has_downstream_reference(snapshot, a):
            continue
        subject = a.get("subject_name")
        obj = _assertion_object(a)
        candidates.append(
            _base_candidate(
                "F1",
                "open_thread_question",
                world_id=snapshot.get("world_id"),
                assertions=[a],
                discriminator=f"open-thread:{pred}:{subject}:{obj}",
                summary_seed=f"Does {subject}'s {pred} around {obj} ever resolve?",
                body_seed="No adjacent assertion closes or revisits this thread after the setup scene.",
                salience=65 + (max_pos - pos),
            )
        )

    # Relations asserted once, with no corroborating row.
    grouped: dict[tuple, list[dict[str, Any]]] = defaultdict(list)
    for a in snapshot["assertions"]:
        if a.get("predicate") in RELATION_PREDICATES:
            grouped[(a["subject_id"], a["predicate"], a.get("object_id"), norm_text(a.get("object_value")))].append(a)
    for rows in grouped.values():
        if len(rows) != 1:
            continue
        a = rows[0]
        if max_pos - (a.get("story_position") or 0) < n:
            continue
        candidates.append(
            _base_candidate(
                "F1",
                "single_relation",
                world_id=snapshot.get("world_id"),
                assertions=[a],
                discriminator=f"single-relation:{assertion_signature(a)}",
                summary_seed=f"Is {_assertion_phrase(a)} corroborated anywhere else?",
                body_seed="The relation is asserted once and has no second source in the graph.",
                salience=40,
            )
        )

    # Capability/trait facts never exercised.
    for a in snapshot["assertions"]:
        if a.get("predicate") not in {"cannot", "trait"}:
            continue
        pos = a.get("story_position") or 0
        if max_pos - pos < n:
            continue
        terms = _thread_terms(a)
        exercised = any(
            later.get("subject_id") == a.get("subject_id")
            and later.get("predicate") in {"fact", "cannot"}
            and (_tokens(later.get("object_value")) & terms)
            for later in _later_assertions(snapshot, pos, exclude_id=a["id"])
        )
        if exercised:
            continue
        candidates.append(
            _base_candidate(
                "F1",
                "unexercised_fact",
                world_id=snapshot.get("world_id"),
                assertions=[a],
                discriminator=f"unexercised:{assertion_signature(a)}",
                summary_seed=f"Does {_assertion_phrase(a)} matter later?",
                body_seed="The fact is established but no later assertion exercises the same handle.",
                salience=35,
            )
        )
    return _dedupe_candidates(candidates)


def collect_dormant_knowledge_candidates(snapshot: dict[str, Any], *, threshold: int | None = None) -> list[dict[str, Any]]:
    """F3: knowledge/belief that stays asymmetric and unused."""

    n = max(3, age_threshold(snapshot, threshold) // 2)
    max_pos = max((s["story_position"] for s in snapshot["scenes"]), default=0)
    candidates: list[dict[str, Any]] = []
    for a in snapshot["assertions"]:
        if a.get("predicate") not in KNOWLEDGE_PREDICATES or not a.get("object_value"):
            continue
        pos = a.get("story_position") or 0
        if max_pos - pos < n:
            continue
        terms = _thread_terms(a)
        concerns = _fact_concern_entities(snapshot, a)
        concern_ids = {e["id"] for e in concerns}
        subject_acts = any(
            later.get("subject_id") == a.get("subject_id")
            and later.get("predicate") not in KNOWLEDGE_PREDICATES
            and (_tokens(later.get("object_value")) | _tokens(later.get("supporting_quote"))) & terms
            for later in _later_assertions(snapshot, pos, exclude_id=a["id"])
        )
        target_learns = any(
            later.get("subject_id") in concern_ids
            and later.get("predicate") in KNOWLEDGE_PREDICATES
            and (_tokens(later.get("object_value")) & terms)
            for later in _later_assertions(snapshot, pos, exclude_id=a["id"])
        )
        if subject_acts or target_learns:
            continue
        label = a["predicate"]
        subject = a.get("subject_name")
        fact = a.get("object_value")
        span = f"{_scene_label(a['scene'])} through {_scene_label(snapshot['scenes'][-1])}" if snapshot["scenes"] else "the corpus"
        candidates.append(
            _base_candidate(
                "F3",
                "dormant_knowledge",
                world_id=snapshot.get("world_id"),
                assertions=[a],
                entities=concerns,
                discriminator=f"dormant:{assertion_signature(a)}",
                summary_seed=f"{subject}'s {label} of {fact} stays dormant.",
                body_seed=f"The graph shows no later action on this fact and no later correction/exploitation across {span}.",
                salience=55 + (max_pos - pos) + len(concerns) * 5,
                extra={"absence_span": span, "concerns": [e["name"] for e in concerns]},
            )
        )
    return _dedupe_candidates(candidates)


def _prior_motivation_pool(snapshot: dict[str, Any], actor_id: int, before_pos: int) -> list[dict[str, Any]]:
    rows = [
        a
        for a in snapshot["assertions"]
        if a.get("subject_id") == actor_id
        and a.get("predicate") in MOTIVATION_PREDICATES
        and (a.get("story_position") or 0) < before_pos
    ]
    return sorted(rows, key=lambda r: (r.get("story_position") or 0, r["id"]), reverse=True)


def collect_unmotivated_turn_candidates(snapshot: dict[str, Any]) -> list[dict[str, Any]]:
    """F4: plot-relevant character actions plus prior motivation pool."""

    candidates: list[dict[str, Any]] = []
    for a in snapshot["assertions"]:
        if a.get("subject_kind") != "character":
            continue
        pred = a.get("predicate")
        if pred not in TURN_PREDICATES and not (pred == "cannot" and a.get("polarity") is False):
            continue
        if pred in {"dies"}:  # v1 cannot infer the killer from this assertion.
            continue
        pool = _prior_motivation_pool(snapshot, a["subject_id"], a.get("story_position") or 0)
        nearest = pool[0] if pool else None
        citations = [a]
        if nearest:
            citations.append(nearest)
        status = "absent" if nearest is None else "distant"
        salience = 50 if nearest is None else max(10, 45 - ((a.get("story_position") or 0) - (nearest.get("story_position") or 0)))
        subject = a.get("subject_name")
        action = _assertion_phrase(a)
        motivation_text = _assertion_phrase(nearest) if nearest else "none"
        candidates.append(
            _base_candidate(
                "F4",
                "unmotivated_turn",
                world_id=snapshot.get("world_id"),
                assertions=citations,
                discriminator=f"turn:{assertion_signature(a)}",
                summary_seed=f"{subject}'s turn has {status} established motivation.",
                body_seed=(
                    f"Action: {action}. "
                    f"Nearest candidate motivation: {motivation_text}."
                ),
                salience=salience,
                extra={
                    "action_assertion_id": a["id"],
                    "nearest_motivation_assertion_id": nearest["id"] if nearest else None,
                    "motivation_status": status,
                },
            )
        )
    return _dedupe_candidates(candidates)


def collect_candidates(
    snapshot: dict[str, Any],
    *,
    threshold: int | None = None,
    scene_open_questions: list[dict[str, Any]] | None = None,
    suppressed_note_keys: set[str] | None = None,
    enabled_families: tuple[str, ...] | None = None,
) -> dict[str, list[dict[str, Any]]]:
    suppressed_note_keys = suppressed_note_keys or set()
    enabled = set(enabled_families or FAMILIES)
    by_family: dict[str, list[dict[str, Any]]] = {}
    if "F1" in enabled:
        by_family["F1"] = collect_open_question_candidates(
            snapshot,
            threshold=threshold,
            scene_open_questions=scene_open_questions,
        )
    if "F2" in enabled:
        by_family["F2"] = collect_idle_setup_candidates(snapshot, threshold=threshold)
    if "F3" in enabled:
        by_family["F3"] = collect_dormant_knowledge_candidates(snapshot, threshold=threshold)
    if "F4" in enabled:
        by_family["F4"] = collect_unmotivated_turn_candidates(snapshot)
    return {
        family: [c for c in rows if c["note_key"] not in suppressed_note_keys]
        for family, rows in by_family.items()
    }


def load_scene_open_questions(path: str | None, snapshot: dict[str, Any]) -> list[dict[str, Any]]:
    if not path:
        return []
    data = json.loads(open(path, encoding="utf-8").read())
    rows = data.get("scenes", []) if isinstance(data, dict) else data
    by_pos = {s["story_position"]: s for s in snapshot["scenes"]}
    by_label = {_scene_label(s): s for s in snapshot["scenes"]}
    out = []
    for row in rows:
        scene = None
        if isinstance(row, dict):
            scene_id = row.get("scene_id") or row.get("scene")
            if scene_id in by_label:
                scene = by_label[scene_id]
            elif row.get("story_position") in by_pos:
                scene = by_pos[row["story_position"]]
            elif row.get("id") in by_label:
                scene = by_label[row["id"]]
            questions = row.get("open_questions") or row.get("questions") or []
            if isinstance(questions, str):
                questions = [questions]
            for q in questions:
                if scene and str(q).strip():
                    out.append({"scene_id": scene["id"], "question": str(q).strip()})
    return out


def note_schema() -> dict[str, Any]:
    nullable_int = {"anyOf": [{"type": "integer"}, {"type": "null"}]}
    nullable_str = {"anyOf": [{"type": "string"}, {"type": "null"}]}
    citation = {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "assertion_id": nullable_int,
            "scene_id": {"type": "integer"},
            "quote": {"type": "string"},
        },
        "required": ["assertion_id", "scene_id", "quote"],
    }
    note = {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "candidate_id": {"type": "string"},
            "surface": {"type": "boolean"},
            "summary": {"type": "string"},
            "body": {"type": "string"},
            "salience": {"type": "integer"},
            "motivation_status": nullable_str,
            "citations": {"type": "array", "items": citation},
        },
        "required": [
            "candidate_id",
            "surface",
            "summary",
            "body",
            "salience",
            "motivation_status",
            "citations",
        ],
    }
    return {
        "type": "object",
        "additionalProperties": False,
        "properties": {"notes": {"type": "array", "items": note}},
        "required": ["notes"],
    }


REPORT_SYSTEM_PROMPT = """You are Canon AI's Reader's Report note selector.

Canon never writes the user's story. You do not invent plot, dialogue, beats, motives, fixes, or suggestions. SQL/Python has already produced deterministic candidates from the user's canon graph. Your job is only to select salient candidates, rank them, and phrase them.

Return only notes tied to candidate_id values in the input. You may decline any candidate. You may not introduce a new cited fact, scene, quote, assertion, or candidate. Every citation must copy an assertion_id, scene_id, and quote from the candidate's evidence exactly.

Phrasing contract: one short paragraph per note, summary first, evidence second, citations last. No scores. No praise. No hedges. No imperatives aimed at story content. No rewrite examples. Declarative for facts, interrogative for gaps.

For F4 only, motivation_status must be exactly present, distant, or absent. Never say weak.
"""


def _family_prompt(family: str, candidates: list[dict[str, Any]]) -> str:
    contracts = {
        "F1": "F1 open_question: surface questions an exec/reader would ask. Cite the scene that raises the gap and the closest adjacent fact when present.",
        "F2": "F2 idle_setup: surface aging setups without downstream references. Cite setup scene and quote; mention age and last touch.",
        "F3": "F3 dormant_knowledge: surface long-running knowledge/belief asymmetries not acted on or corrected. Cite the knowledge start and absence span.",
        "F4": "F4 unmotivated_turn: judge whether motivation is present, distant, or absent. Name nearest candidate motivation with quote/citation or state none exists. Do not call motivation weak.",
    }
    compact = []
    for c in candidates:
        compact.append(
            {
                "candidate_id": c["candidate_id"],
                "family": c["family"],
                "kind": c["kind"],
                "summary_seed": c["summary_seed"],
                "body_seed": c["body_seed"],
                "salience": c["salience"],
                "motivation_status": c.get("motivation_status"),
                "evidence": c["evidence"],
            }
        )
    return contracts[family] + "\n\nCANDIDATES:\n" + json.dumps(compact, ensure_ascii=False, indent=2)


def phrase_family_with_llm(
    client,
    family: str,
    candidates: list[dict[str, Any]],
    *,
    model: str = DEFAULT_MODEL,
    effort: str = DEFAULT_EFFORT,
    thinking: bool = True,
) -> list[dict[str, Any]]:
    if not candidates:
        return []
    req: dict[str, Any] = {
        "model": model,
        "max_tokens": 4000,
        "system": REPORT_SYSTEM_PROMPT,
        "messages": [{"role": "user", "content": _family_prompt(family, candidates)}],
        "output_config": {"format": {"type": "json_schema", "schema": note_schema()}},
    }
    if effort:
        req["output_config"]["effort"] = effort
    if thinking:
        req["thinking"] = {"type": "adaptive"}
    resp = client.messages.create(**req)
    if getattr(resp, "stop_reason", None) == "refusal":
        return []
    text = first_text(resp)
    if text is None:
        raise RuntimeError(f"no text block in report {family} response")
    payload = json.loads(text)
    return payload.get("notes") or []


def deterministic_draft_notes(family: str, candidates: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Offline fallback for tests/dry runs. It still only phrases candidates."""

    out = []
    for c in candidates[: FAMILY_CAPS[family]]:
        citations = c.get("evidence", {}).get("citations", [])
        body = c["body_seed"]
        if c["family"] == "F4":
            status = c.get("motivation_status") or "absent"
            body = f"{body} Motivation status: {status}."
        out.append(
            {
                "candidate_id": c["candidate_id"],
                "surface": True,
                "summary": c["summary_seed"],
                "body": body,
                "salience": c.get("salience", 0),
                "motivation_status": c.get("motivation_status"),
                "citations": [
                    {
                        "assertion_id": cit.get("assertion_id"),
                        "scene_id": cit.get("scene_id"),
                        "quote": cit.get("quote"),
                    }
                    for cit in citations
                ],
            }
        )
    return out


def _quote_valid_for_scene(scene: dict[str, Any], quote: str) -> bool:
    return bool(quote and quote_in_text(quote, scene.get("raw_text") or ""))


def validate_draft_notes(
    snapshot: dict[str, Any],
    draft_notes: list[dict[str, Any]],
    candidates: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], list[ReportLog]]:
    """Drop notes whose candidate/citations do not verify against the DB snapshot."""

    candidate_by_id = {c["candidate_id"]: c for c in candidates}
    logs: list[ReportLog] = []
    valid: list[dict[str, Any]] = []
    for note in draft_notes:
        if not note.get("surface", True):
            continue
        cid = note.get("candidate_id")
        candidate = candidate_by_id.get(cid)
        if not candidate:
            logs.append(ReportLog("drop", "unknown candidate_id", candidate_id=cid))
            continue
        summary = (note.get("summary") or "").strip()
        body = (note.get("body") or "").strip()
        if not summary or not body:
            logs.append(ReportLog("drop", "empty summary/body", candidate["note_key"], cid))
            continue
        if candidate["family"] == "F4":
            motivation_status = (note.get("motivation_status") or candidate.get("motivation_status") or "").strip()
            if motivation_status not in {"present", "distant", "absent"}:
                logs.append(ReportLog("drop", "invalid F4 motivation status", candidate["note_key"], cid))
                continue
            if "weak" in norm_text(f"{summary} {body}").split():
                logs.append(ReportLog("drop", "F4 used banned weak motivation language", candidate["note_key"], cid))
                continue

        citations = note.get("citations") or []
        if not citations:
            logs.append(ReportLog("drop", "note has no citations", candidate["note_key"], cid))
            continue

        validated_citations = []
        failed = None
        for cit in citations:
            try:
                scene_id = int(cit.get("scene_id"))
            except (TypeError, ValueError):
                failed = "invalid scene id"
                break
            scene = snapshot["scene_by_id"].get(scene_id)
            if not scene:
                failed = f"scene {scene_id} not found"
                break
            quote = (cit.get("quote") or "").strip()
            assertion_id = cit.get("assertion_id")
            if assertion_id is not None:
                try:
                    assertion_id = int(assertion_id)
                except (TypeError, ValueError):
                    failed = "invalid assertion id"
                    break
                assertion = snapshot["assertion_by_id"].get(assertion_id)
                if not assertion:
                    failed = f"assertion {assertion_id} not found"
                    break
                if assertion["scene_id"] != scene_id:
                    failed = f"assertion {assertion_id} scene mismatch"
                    break
                if quote != (assertion.get("supporting_quote") or "").strip():
                    failed = f"assertion {assertion_id} quote mismatch"
                    break
            if not _quote_valid_for_scene(scene, quote):
                failed = f"quote not found in scene {scene_id}"
                break
            validated_citations.append(
                {
                    "assertion_id": assertion_id,
                    "scene_id": scene_id,
                    "quote": quote,
                    "label": _scene_label(scene),
                }
            )
        if failed:
            logs.append(ReportLog("drop", failed, candidate["note_key"], cid))
            continue

        valid.append(
            {
                "note_key": candidate["note_key"],
                "family": candidate["family"],
                "summary": summary,
                "body": body,
                "salience": int(note.get("salience") or candidate.get("salience") or 0),
                "status": "open",
                "evidence": {
                    "candidate_id": cid,
                    "kind": candidate.get("kind"),
                    "citations": validated_citations,
                    "anchor_assertion_ids": candidate.get("anchor_assertion_ids") or [],
                    "anchor_scene_ids": candidate.get("anchor_scene_ids") or [],
                    "anchor_entity_ids": candidate.get("anchor_entity_ids") or [],
                    "motivation_status": note.get("motivation_status") or candidate.get("motivation_status"),
                },
            }
        )
    return valid, logs


def load_suppressed_note_keys(conn, world_id: int) -> set[str]:
    cur = conn.cursor()
    try:
        cur.execute(
            "SELECT note_key FROM coverage_notes "
            "WHERE world_id = %s AND status IN ('sealed','dismissed')",
            (world_id,),
        )
        return {r[0] for r in cur.fetchall()}
    except Exception:
        try:
            conn.rollback()
        except Exception:
            pass
        return set()


def load_existing_note_statuses(conn, world_id: int) -> dict[str, str]:
    cur = conn.cursor()
    try:
        cur.execute("SELECT note_key, status FROM coverage_notes WHERE world_id = %s", (world_id,))
        return {r[0]: r[1] for r in cur.fetchall()}
    except Exception:
        try:
            conn.rollback()
        except Exception:
            pass
        return {}


def filter_suppressed_candidates(candidates: dict[str, list[dict[str, Any]]], suppressed: set[str]) -> dict[str, list[dict[str, Any]]]:
    return {family: [c for c in rows if c["note_key"] not in suppressed] for family, rows in candidates.items()}


def persist_coverage_notes(
    conn,
    world_id: int,
    notes: list[dict[str, Any]],
    *,
    run_id: uuid.UUID | None = None,
    families: tuple[str, ...] = FAMILIES,
) -> None:
    run_id = run_id or uuid.uuid4()
    current_keys = {n["note_key"] for n in notes}
    cur = conn.cursor()
    for family in families:
        family_keys = {n["note_key"] for n in notes if n["family"] == family}
        cur.execute(
            "UPDATE coverage_notes SET status = 'addressed', resolved_at = now(), last_seen_run = %s, updated_at = now() "
            "WHERE world_id = %s AND family = %s AND status = 'open' AND NOT (note_key = ANY(%s))",
            (str(run_id), world_id, family, list(family_keys)),
        )
    for n in notes:
        cur.execute(
            "INSERT INTO coverage_notes "
            "(world_id, note_key, family, summary, body, evidence, salience, status, first_seen_run, last_seen_run) "
            "VALUES (%s, %s, %s, %s, %s, %s::jsonb, %s, 'open', %s, %s) "
            "ON CONFLICT (note_key) DO UPDATE SET "
            "summary = EXCLUDED.summary, body = EXCLUDED.body, evidence = EXCLUDED.evidence, "
            "salience = EXCLUDED.salience, last_seen_run = EXCLUDED.last_seen_run, "
            "status = CASE WHEN coverage_notes.status = 'addressed' THEN 'open' ELSE coverage_notes.status END, "
            "resolved_at = CASE WHEN coverage_notes.status = 'addressed' THEN NULL ELSE coverage_notes.resolved_at END, "
            "updated_at = now() "
            "WHERE coverage_notes.status NOT IN ('sealed','dismissed')",
            (
                world_id,
                n["note_key"],
                n["family"],
                n["summary"],
                n["body"],
                json.dumps(n["evidence"], ensure_ascii=False),
                n["salience"],
                str(run_id),
                str(run_id),
            ),
        )
    conn.commit()
    _ = current_keys


def generate_coverage_notes(
    snapshot: dict[str, Any],
    *,
    client=None,
    use_llm: bool = True,
    model: str = DEFAULT_MODEL,
    effort: str = DEFAULT_EFFORT,
    threshold: int | None = None,
    scene_open_questions: list[dict[str, Any]] | None = None,
    suppressed_note_keys: set[str] | None = None,
    enabled_families: tuple[str, ...] | None = None,
) -> tuple[list[dict[str, Any]], list[ReportLog], dict[str, list[dict[str, Any]]]]:
    enabled_families = enabled_families or FAMILIES
    candidates_by_family = collect_candidates(
        snapshot,
        threshold=threshold,
        scene_open_questions=scene_open_questions,
        suppressed_note_keys=suppressed_note_keys,
        enabled_families=enabled_families,
    )
    notes: list[dict[str, Any]] = []
    logs: list[ReportLog] = []
    for family in enabled_families:
        candidates = candidates_by_family.get(family, [])
        if use_llm:
            if client is None:
                raise RuntimeError("Reader's Report LLM phrasing requires an Anthropic client; pass --no-llm for deterministic dry-run phrasing.")
            draft = phrase_family_with_llm(client, family, candidates, model=model, effort=effort)
        else:
            draft = deterministic_draft_notes(family, candidates)
        valid, dropped = validate_draft_notes(snapshot, draft, candidates)
        notes.extend(valid[: FAMILY_CAPS[family]])
        logs.extend(dropped)
    notes.sort(key=lambda n: (FAMILIES.index(n["family"]), -n["salience"], n["note_key"]))
    return notes, logs, candidates_by_family


def run_report_notes(
    conn,
    world_id: int,
    *,
    client=None,
    use_llm: bool = True,
    model: str = DEFAULT_MODEL,
    effort: str = DEFAULT_EFFORT,
    threshold: int | None = None,
    scene_open_questions_path: str | None = None,
    persist: bool = True,
    run_id: uuid.UUID | None = None,
) -> tuple[list[dict[str, Any]], list[ReportLog], dict[str, list[dict[str, Any]]], dict[str, Any]]:
    suppressed = load_suppressed_note_keys(conn, world_id)
    enabled_families = family_config.enabled_note_families(conn, world_id)
    snapshot = load_world_snapshot(conn, world_id)
    scene_open_questions = load_scene_open_questions(scene_open_questions_path, snapshot)
    notes, logs, candidates = generate_coverage_notes(
        snapshot,
        client=client,
        use_llm=use_llm,
        model=model,
        effort=effort,
        threshold=threshold,
        scene_open_questions=scene_open_questions,
        suppressed_note_keys=suppressed,
        enabled_families=enabled_families,
    )
    notes = [n for n in notes if n["note_key"] not in suppressed]
    if persist:
        persist_coverage_notes(conn, world_id, notes, run_id=run_id, families=enabled_families)
    return notes, logs, candidates, snapshot


def coverage_notes_json(notes: list[dict[str, Any]]) -> dict[str, Any]:
    return {"coverage_notes": notes}


def load_bearing_canon(snapshot: dict[str, Any], limit: int = 6) -> list[str]:
    entity_counts: Counter[int] = Counter()
    scene_counts: dict[int, set[int]] = defaultdict(set)
    for a in snapshot["assertions"]:
        for eid in (a.get("subject_id"), a.get("object_id")):
            if eid:
                entity_counts[eid] += 1
                scene_counts[eid].add(a["scene_id"])
    lines = []
    for eid, count in entity_counts.most_common(limit):
        ent = snapshot["entity_by_id"].get(eid)
        if not ent:
            continue
        lines.append(f"{ent['name']}: {count} assertion(s) across {len(scene_counts[eid])} cited scene(s).")
    return lines


def corpus_appendix(snapshot: dict[str, Any], notes: list[dict[str, Any]]) -> list[str]:
    status_counts = Counter(a.get("status") for a in snapshot["assertions"])
    family_counts = Counter(n["family"] for n in notes)
    return [
        f"Scenes: {len(snapshot['scenes'])}",
        f"Entities: {len(snapshot['entities'])}",
        f"Assertions: {len(snapshot['assertions'])}",
        "Assertion statuses: " + (", ".join(f"{k}={v}" for k, v in sorted(status_counts.items())) or "none"),
        "Coverage notes: " + (", ".join(f"{k}={family_counts.get(k, 0)}" for k in FAMILIES) or "none"),
    ]


def _finding_line(f: dict[str, Any]) -> str:
    severity = f.get("severity") or "note"
    check_name = f.get("check") or f.get("check_name")
    explanation = f.get("explanation") or ""
    return f"- [{severity}] {check_name}: {explanation}"


def _note_line(n: dict[str, Any]) -> str:
    citations = []
    for c in n.get("evidence", {}).get("citations", []):
        label = c.get("label") or f"scene {c.get('scene_id')}"
        if label not in citations:
            citations.append(label)
    cite = "; cites: " + ", ".join(citations) if citations else ""
    return f"- **{n['summary']}** {n['body']}{cite}"


def _append_until(lines: list[str], additions: list[str], limit: int) -> None:
    for line in additions:
        if len(lines) >= limit:
            marker = "- Report body capped at two pages; lower-salience notes omitted."
            if lines and lines[-1] != marker:
                lines[-1] = marker
            return
        lines.append(line)


def render_markdown_report(
    world_name: str,
    snapshot: dict[str, Any],
    findings: list[dict[str, Any]],
    notes: list[dict[str, Any]],
    *,
    body_line_limit: int = BODY_LINE_LIMIT,
) -> str:
    live_findings = [f for f in findings if not f.get("sealed")]
    body: list[str] = [f"# Reader's Report - {world_name}", ""]
    body.extend(["## Continuity Findings"])
    finding_lines = [_finding_line(f) for f in live_findings] or ["- No live continuity findings."]
    _append_until(body, finding_lines + [""], body_line_limit)

    body.extend(["## Load-Bearing Canon"])
    _append_until(body, [f"- {line}" for line in load_bearing_canon(snapshot)] or ["- No load-bearing canon rows yet."], body_line_limit)
    _append_until(body, [""], body_line_limit)

    by_family = defaultdict(list)
    for note in notes:
        if note.get("status", "open") == "open":
            by_family[note["family"]].append(note)
    for family in FAMILIES:
        body.extend([f"## {FAMILY_LABELS[family]}"])
        rows = sorted(by_family.get(family, []), key=lambda n: (-n["salience"], n["note_key"]))
        lines = [_note_line(n) for n in rows] or ["- No open notes."]
        _append_until(body, lines + [""], body_line_limit)

    if len(body) > body_line_limit:
        body = body[:body_line_limit]
        if body:
            body[-1] = "- Report body capped at two pages; lower-salience notes omitted."

    appendix = ["## Corpus Appendix"] + [f"- {line}" for line in corpus_appendix(snapshot, notes)]
    return "\n".join(body).rstrip() + "\n\n---\n\n" + "\n".join(appendix) + "\n"


def body_line_count(markdown: str) -> int:
    body = markdown.split("\n---\n", 1)[0]
    return len(body.splitlines())

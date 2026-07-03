"""Deterministic retcon ripple queries.

Retcon ripple is the inverse of a continuity scan: given a proposed graph
change, report the existing assertions, scenes, and check findings that would be
affected or contradicted. It is read-only and contains no LLM path.
"""

from __future__ import annotations

import json
import re
from typing import Any

from . import families

INVALID_STATUSES = ("sealed", "retconned")
EVENT_PREDICATES = {"dies", "destroyed"}
SCENE_LABELED_SQL = (
    "(select s.*, row_number() over "
    "(partition by s.work_id order by s.story_position) as scene_index from scenes s)"
)


def norm_text(value: Any) -> str:
    return re.sub(r"\s+", " ", re.sub(r"[^a-z0-9 ]", " ", str(value or "").lower())).strip()


def _dict_rows(cur) -> list[dict[str, Any]]:
    cols = [d[0] for d in cur.description]
    return [dict(zip(cols, r)) for r in cur.fetchall()]


def _first_quote(raw_text: str | None) -> str:
    for line in (raw_text or "").splitlines():
        stripped = line.strip()
        if len(stripped) >= 8:
            return stripped[:240]
    return (raw_text or "").strip()[:240]


def _scene_label(row: dict[str, Any]) -> str:
    if row.get("scene_label"):
        return row["scene_label"]
    work = str(row.get("work_title") or "?").split()[0]
    index = row.get("scene_index") or "?"
    return f"{work}/sc{index}"


def _assertion_object(row: dict[str, Any]) -> str:
    return str(row.get("object_name") or row.get("object_value") or row.get("object_assertion_id") or "").strip()


def _assertion_phrase(row: dict[str, Any]) -> str:
    obj = _assertion_object(row)
    neg = "not " if row.get("polarity") is False else ""
    return f"{row.get('subject_name')} {neg}{row.get('predicate')} {obj}".strip()


def _assertion_citation(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "assertion_id": row["id"],
        "scene_id": row["scene_id"],
        "label": _scene_label(row),
        "quote": row.get("supporting_quote") or "",
    }


def _scene_citation(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "scene_id": row["id"],
        "label": _scene_label(row),
        "quote": _first_quote(row.get("raw_text")),
    }


ASSERTION_SELECT = """
select a.id, a.world_id, a.subject_id, subj.name as subject_name, subj.kind::text as subject_kind,
       a.predicate, a.object_id, obj.name as object_name, obj.kind::text as object_kind,
       a.object_value, a.object_assertion_id, a.polarity,
       lower(a.valid_during) as start_pos, upper(a.valid_during) as end_pos,
       lower_inf(a.valid_during) as start_inf, upper_inf(a.valid_during) as end_inf,
       a.valid_during::text as valid_during, a.established_in_scene as scene_id,
       a.supporting_quote, a.status::text as status,
       s.story_position, s.slug, s.raw_text, w.title as work_title, s.scene_index
from assertions a
join entities subj on subj.id = a.subject_id
left join entities obj on obj.id = a.object_id
join """ + SCENE_LABELED_SQL + """ s on s.id = a.established_in_scene
join works w on w.id = s.work_id
"""


def world_id(conn, world_name: str) -> int:
    cur = conn.cursor()
    cur.execute("select id from worlds where name = %s", (world_name,))
    row = cur.fetchone()
    if not row:
        raise RuntimeError(f"world '{world_name}' not found")
    try:
        conn.rollback()
    except Exception:
        pass
    return int(row[0])


def resolve_entity(conn, world_id: int, name: str, kind: str | None = None) -> dict[str, Any]:
    cur = conn.cursor()
    n = norm_text(name)
    kind_clause = ""
    params: list[Any] = [world_id]
    if kind is not None:
        kind_clause = "and e.kind::text = %s "
        params.append(kind)
    params.extend([n, n])
    cur.execute(
        "select distinct e.id, e.name, e.kind::text as kind "
        "from entities e left join aliases al on al.entity_id = e.id "
        "where e.world_id = %s "
        + kind_clause +
        "and (regexp_replace(lower(e.name), '[^a-z0-9 ]', '', 'g') = %s "
        "     or regexp_replace(lower(al.alias), '[^a-z0-9 ]', '', 'g') = %s) "
        "order by e.name",
        params,
    )
    rows = _dict_rows(cur)
    if not rows:
        raise RuntimeError(f"entity '{name}' not found")
    if len(rows) > 1:
        names = ", ".join(f"{r['name']} ({r['kind']})" for r in rows)
        raise RuntimeError(f"entity '{name}' is ambiguous: {names}")
    return rows[0]


def position_for_ref(conn, world_id: int, ref: str | int) -> int:
    if isinstance(ref, int):
        return ref
    text = str(ref).strip()
    if text.isdigit():
        return int(text)
    cur = conn.cursor()
    m = re.fullmatch(r"(.+?)/sc(\d+)", text, re.I)
    if m:
        work_ref, scene_index = m.group(1).strip(), int(m.group(2))
        cur.execute(
            "select story_position from ("
            "  select s.story_position, w.title, row_number() over (partition by s.work_id order by s.story_position) as scene_index "
            "  from scenes s join works w on w.id = s.work_id where w.world_id = %s"
            ") x where lower(split_part(title, ' ', 1)) = lower(%s) and scene_index = %s",
            (world_id, work_ref, scene_index),
        )
        row = cur.fetchone()
        if row:
            return int(row[0])
    episode_refs = [text]
    m = re.fullmatch(r"(\d+)\.(\d+)", text)
    if m:
        season, episode = int(m.group(1)), int(m.group(2))
        episode_refs.extend([f"E{season}{episode:02d}", f"S{season}E{episode:02d}"])
    for candidate in episode_refs:
        cur.execute(
            "select min(s.story_position) "
            "from scenes s join works w on w.id = s.work_id "
            "where w.world_id = %s and lower(split_part(w.title, ' ', 1)) = lower(%s)",
            (world_id, candidate),
        )
        row = cur.fetchone()
        if row and row[0] is not None:
            return int(row[0])
    raise RuntimeError(f"story position or scene ref '{ref}' not found")


def find_assertion(
    conn,
    world_id: int,
    *,
    assertion_id: int | None = None,
    subject: str | None = None,
    predicate: str | None = None,
    from_position: int | None = None,
) -> dict[str, Any]:
    cur = conn.cursor()
    if assertion_id is not None:
        cur.execute(
            ASSERTION_SELECT + " where a.world_id = %s and a.id = %s",
            (world_id, assertion_id),
        )
    else:
        if not subject or not predicate:
            raise RuntimeError("subject and predicate are required when assertion_id is omitted")
        ent = resolve_entity(conn, world_id, subject)
        params: list[Any] = [world_id, ent["id"], predicate]
        extra = ""
        if from_position is not None:
            extra = " and a.valid_during @> %s"
            params.append(from_position)
        cur.execute(
            ASSERTION_SELECT
            + " where a.world_id = %s and a.subject_id = %s and a.predicate = %s"
            + extra
            + " order by lower(a.valid_during) nulls first, a.id limit 2",
            params,
        )
    rows = _dict_rows(cur)
    if not rows:
        raise RuntimeError("matching assertion not found")
    if assertion_id is None and len(rows) > 1:
        ids = ", ".join(str(r["id"]) for r in rows)
        raise RuntimeError(f"assertion is ambiguous; pass --assertion-id (matches: {ids})")
    return rows[0]


def _range_bounds_for_move(assertion: dict[str, Any], to_pos: int) -> tuple[int | None, int | None]:
    return to_pos, assertion.get("end_pos")


def _range_filter_sql(start: int | None, end: int | None) -> tuple[str, list[Any]]:
    return "int4range(%s, %s)", [start, end]


def _load_related_assertions(
    conn,
    world_id: int,
    *,
    entity_ids: list[int],
    start: int | None,
    end: int | None,
    exclude_assertion_ids: list[int] | None = None,
    object_assertion_ids: list[int] | None = None,
    reason: str,
) -> list[dict[str, Any]]:
    cur = conn.cursor()
    range_sql, range_params = _range_filter_sql(start, end)
    params: list[Any] = [world_id, *range_params, entity_ids, entity_ids]
    object_clause = ""
    if object_assertion_ids:
        object_clause = " or a.object_assertion_id = any(%s)"
        params.append(object_assertion_ids)
    exclude_clause = ""
    if exclude_assertion_ids:
        exclude_clause = " and not (a.id = any(%s))"
        params.append(exclude_assertion_ids)
    cur.execute(
        ASSERTION_SELECT
        + f" where a.world_id = %s and a.valid_during && {range_sql} "
        "and (a.subject_id = any(%s) or a.object_id = any(%s)"
        + object_clause
        + ") and a.status::text not in ('sealed','retconned')"
        + exclude_clause
        + " order by s.story_position, a.id",
        params,
    )
    rows = _dict_rows(cur)
    return [_format_assertion(row, reason) for row in rows]


def _load_cut_entity_assertions(conn, world_id: int, entity: dict[str, Any]) -> list[dict[str, Any]]:
    cur = conn.cursor()
    alias_terms = _entity_terms(conn, entity["id"])
    cur.execute(
        ASSERTION_SELECT
        + " where a.world_id = %s and a.status::text not in ('sealed','retconned') "
        "and (a.subject_id = %s or a.object_id = %s "
        "     or exists ("
        "       select 1 from aliases al where al.entity_id = %s "
        "       and position(regexp_replace(lower(al.alias), '[^a-z0-9 ]', '', 'g') "
        "         in regexp_replace(lower(coalesce(a.object_value, '') || ' ' || coalesce(a.supporting_quote, '')), '[^a-z0-9 ]', '', 'g')) > 0"
        "     )) "
        "order by s.story_position, a.id",
        (world_id, entity["id"], entity["id"], entity["id"]),
    )
    _ = alias_terms
    return [_format_assertion(row, "cut_entity_reference") for row in _dict_rows(cur)]


def _entity_terms(conn, entity_id: int) -> set[str]:
    cur = conn.cursor()
    cur.execute(
        "select name from entities where id = %s union select alias from aliases where entity_id = %s",
        (entity_id, entity_id),
    )
    return {norm_text(r[0]) for r in cur.fetchall() if norm_text(r[0])}


def _load_presence_scenes(
    conn,
    world_id: int,
    *,
    entity_ids: list[int],
    start: int | None = None,
    end: int | None = None,
    reason: str,
) -> list[dict[str, Any]]:
    cur = conn.cursor()
    params: list[Any] = [world_id, entity_ids]
    range_clause = ""
    if start is not None or end is not None:
        range_clause = " and int4range(%s, %s) @> s.story_position"
        params.extend([start, end])
    cur.execute(
        "select distinct s.id, s.slug, s.story_position, s.raw_text, w.title as work_title, s.scene_index "
        "from scene_presence sp "
        "join " + SCENE_LABELED_SQL + " s on s.id = sp.scene_id "
        "join works w on w.id = s.work_id "
        "where w.world_id = %s and sp.entity_id = any(%s)"
        + range_clause
        + " order by s.story_position, s.id",
        params,
    )
    rows = []
    for row in _dict_rows(cur):
        rows.append({
            "id": row["id"],
            "reason": reason,
            "label": _scene_label(row),
            "story_position": row["story_position"],
            "citation": _scene_citation(row),
        })
    return rows


def _load_scenes_for_assertions(assertion_rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    seen = set()
    rows = []
    for a in assertion_rows:
        cit = a["citation"]
        sid = cit["scene_id"]
        if sid in seen:
            continue
        seen.add(sid)
        rows.append({
            "id": sid,
            "reason": "cited_by_affected_assertion",
            "label": cit["label"],
            "story_position": a.get("story_position"),
            "citation": {"scene_id": sid, "label": cit["label"], "quote": cit.get("quote") or ""},
        })
    return rows


def _format_assertion(row: dict[str, Any], reason: str) -> dict[str, Any]:
    return {
        "id": row["id"],
        "reason": reason,
        "subject": row["subject_name"],
        "predicate": row["predicate"],
        "object": _assertion_object(row),
        "valid_during": row["valid_during"],
        "story_position": row["story_position"],
        "citation": _assertion_citation(row),
    }


def _existing_findings(
    conn,
    world_id: int,
    *,
    assertion_ids: list[int],
    scene_ids: list[int],
    disabled: set[str],
) -> list[dict[str, Any]]:
    if not assertion_ids and not scene_ids:
        return []
    cur = conn.cursor()
    cur.execute(
        "select f.id, f.check_name, f.severity, f.explanation, f.scene_id, f.assertion_a, f.assertion_b, "
        "       s.slug, s.story_position, s.raw_text, w.title as work_title, s.scene_index "
        "from findings f left join " + SCENE_LABELED_SQL + " s on s.id = f.scene_id "
        "left join works w on w.id = s.work_id "
        "where f.world_id = %s and not f.sealed and ("
        "  f.assertion_a = any(%s) or f.assertion_b = any(%s) or f.scene_id = any(%s)"
        ") order by f.severity, f.check_name, f.id",
        (world_id, assertion_ids or [-1], assertion_ids or [-1], scene_ids or [-1]),
    )
    rows = []
    for row in _dict_rows(cur):
        if families.normalize_family(row["check_name"]) in disabled:
            continue
        rows.append(_format_finding(row, "persisted_finding"))
    return rows


def _format_finding(row: dict[str, Any], reason: str) -> dict[str, Any]:
    label = _scene_label(row) if row.get("scene_id") else None
    return {
        "id": row.get("id"),
        "source": "predicted" if row.get("id") is None else "persisted",
        "reason": reason,
        "check": row["check_name"],
        "severity": row["severity"],
        "explanation": row["explanation"],
        "scene_id": row.get("scene_id"),
        "assertion_a": row.get("assertion_a"),
        "assertion_b": row.get("assertion_b"),
        "citation": {
            "scene_id": row.get("scene_id"),
            "label": label,
            "quote": _first_quote(row.get("raw_text")) if row.get("raw_text") else "",
        },
    }


def _predicted_dead_speaker(
    conn,
    world_id: int,
    assertion: dict[str, Any],
    start: int | None,
    end: int | None,
) -> list[dict[str, Any]]:
    if "dead_speaker" in families.load_disabled(conn, world_id):
        return []
    if start is None:
        return []
    cur = conn.cursor()
    cur.execute(
        "select null::bigint as id, 'dead_speaker' as check_name, 'critical' as severity, "
        "       (%s || ' appears after proposed death at pos ' || %s::text || ' in scene ' || coalesce(s.slug, 'scene ' || s.id::text) || ' (pos ' || s.story_position::text || ').') as explanation, "
        "       s.id as scene_id, %s::bigint as assertion_a, null::bigint as assertion_b, "
        "       s.slug, s.story_position, s.raw_text, w.title as work_title, s.scene_index "
        "from scene_presence sp join " + SCENE_LABELED_SQL + " s on s.id = sp.scene_id join works w on w.id = s.work_id "
        "where w.world_id = %s and sp.entity_id = %s and not s.is_flashback "
        "and int4range(%s, %s) @> s.story_position and s.story_position > %s "
        "and not exists ("
        "  select 1 from assertions r where r.world_id = %s and r.subject_id = %s "
        "    and r.predicate = 'alive' and r.polarity and r.status::text not in ('sealed','retconned') "
        "    and r.valid_during @> s.story_position and lower(r.valid_during) > %s"
        ") order by s.story_position, s.id",
        (
            assertion["subject_name"], start, assertion["id"],
            world_id, assertion["subject_id"], start, end, start,
            world_id, assertion["subject_id"], start,
        ),
    )
    return [_format_finding(row, "proposed_death_conflict") for row in _dict_rows(cur)]


def _predicted_destroyed_use(
    conn,
    world_id: int,
    assertion: dict[str, Any],
    start: int | None,
    end: int | None,
) -> list[dict[str, Any]]:
    if "destroyed_location_use" in families.load_disabled(conn, world_id):
        return []
    if start is None:
        return []
    cur = conn.cursor()
    cur.execute(
        "select null::bigint as id, 'destroyed_location_use' as check_name, 'critical' as severity, "
        "       ('Scene ' || coalesce(s.slug, 'scene ' || s.id::text) || ' (pos ' || s.story_position::text || ') uses ' || %s || ' after proposed destruction at pos ' || %s::text || '.') as explanation, "
        "       s.id as scene_id, %s::bigint as assertion_a, null::bigint as assertion_b, "
        "       s.slug, s.story_position, s.raw_text, w.title as work_title, s.scene_index "
        "from scene_presence sp join " + SCENE_LABELED_SQL + " s on s.id = sp.scene_id join works w on w.id = s.work_id "
        "where w.world_id = %s and sp.entity_id = %s and not s.is_flashback "
        "and int4range(%s, %s) @> s.story_position and s.story_position > %s "
        "order by s.story_position, s.id",
        (
            assertion["subject_name"], start, assertion["id"], world_id,
            assertion["subject_id"], start, end, start,
        ),
    )
    return [_format_finding(row, "proposed_destruction_conflict") for row in _dict_rows(cur)]


def _predicted_presence_conflicts(
    conn,
    world_id: int,
    assertion: dict[str, Any],
    start: int | None,
    end: int | None,
) -> list[dict[str, Any]]:
    if "presence_conflict" in families.load_disabled(conn, world_id):
        return []
    if assertion.get("predicate") != "located_at" or assertion.get("object_id") is None:
        return []
    cur = conn.cursor()
    cur.execute(
        "select null::bigint as id, 'presence_conflict' as check_name, 'critical' as severity, "
        "       (%s || ' would be at ' || %s || ' during ' || int4range(%s, %s)::text || ' and also at ' || obj.name || ' during ' || other.valid_during::text || '.') as explanation, "
        "       other.established_in_scene as scene_id, %s::bigint as assertion_a, other.id as assertion_b, "
        "       s.slug, s.story_position, s.raw_text, w.title as work_title, s.scene_index "
        "from assertions other join entities obj on obj.id = other.object_id "
        "join " + SCENE_LABELED_SQL + " s on s.id = other.established_in_scene join works w on w.id = s.work_id "
        "where other.world_id = %s and other.subject_id = %s and other.predicate = 'located_at' "
        "and other.object_id is not null and other.object_id <> %s and other.id <> %s "
        "and other.status::text not in ('sealed','retconned') "
        "and other.valid_during && int4range(%s, %s) "
        "order by s.story_position, other.id",
        (
            assertion["subject_name"], assertion.get("object_name") or assertion.get("object_value"),
            start, end, assertion["id"], world_id, assertion["subject_id"],
            assertion["object_id"], assertion["id"], start, end,
        ),
    )
    return [_format_finding(row, "proposed_location_overlap") for row in _dict_rows(cur)]


def _predicted_capability_conflicts(
    conn,
    world_id: int,
    assertion: dict[str, Any],
    start: int | None,
    end: int | None,
) -> list[dict[str, Any]]:
    if "capability_violation" in families.load_disabled(conn, world_id):
        return []
    if assertion.get("predicate") != "cannot" or not assertion.get("polarity") or not assertion.get("object_value"):
        return []
    cur = conn.cursor()
    cur.execute(
        "select null::bigint as id, 'capability_violation' as check_name, 'warning' as severity, "
        "       (%s || ' would have cannot(\"' || %s || '\") during ' || int4range(%s, %s)::text || ', but scene ' || coalesce(vs.slug, 'scene ' || vs.id::text) || ' (pos ' || vs.story_position::text || ') shows the act.') as explanation, "
        "       v.established_in_scene as scene_id, %s::bigint as assertion_a, v.id as assertion_b, "
        "       vs.slug, vs.story_position, vs.raw_text, w.title as work_title, vs.scene_index "
        "from assertions v join " + SCENE_LABELED_SQL + " vs on vs.id = v.established_in_scene join works w on w.id = vs.work_id "
        "where v.world_id = %s and v.subject_id = %s and v.id <> %s "
        "and v.status::text not in ('sealed','retconned') and v.valid_during && int4range(%s, %s) "
        "and ((v.polarity and v.predicate <> 'cannot' and position(%s in regexp_replace(lower(coalesce(v.object_value, '') || ' ' || coalesce(v.supporting_quote, '')), '[^a-z0-9 ]', '', 'g')) > 0) "
        "  or (not v.polarity and v.predicate = 'cannot' and regexp_replace(lower(v.object_value), '[^a-z0-9 ]', '', 'g') = %s)) "
        "order by vs.story_position, v.id",
        (
            assertion["subject_name"], assertion["object_value"], start, end,
            assertion["id"], world_id, assertion["subject_id"], assertion["id"],
            start, end, norm_text(assertion["object_value"]), norm_text(assertion["object_value"]),
        ),
    )
    return [_format_finding(row, "proposed_capability_conflict") for row in _dict_rows(cur)]


def _epistemic_assertions(
    conn,
    world_id: int,
    assertion: dict[str, Any],
    start: int | None,
    end: int | None,
) -> list[dict[str, Any]]:
    if assertion.get("predicate") not in {"knows", "believes"} or not assertion.get("object_value"):
        return []
    cur = conn.cursor()
    cur.execute(
        ASSERTION_SELECT
        + " where a.world_id = %s and a.id <> %s and a.predicate in ('knows','believes') "
        "and a.object_value is not null and a.status::text not in ('sealed','retconned') "
        "and regexp_replace(lower(a.object_value), '[^a-z0-9 ]', '', 'g') = %s "
        "and a.valid_during && int4range(%s, %s) "
        "order by s.story_position, a.id",
        (world_id, assertion["id"], norm_text(assertion["object_value"]), start, end),
    )
    return [_format_assertion(row, "epistemic_fact_overlap") for row in _dict_rows(cur)]


def _dedupe(rows: list[dict[str, Any]], key: str = "id") -> list[dict[str, Any]]:
    out = {}
    for row in rows:
        out.setdefault(row.get(key), row)
    return list(out.values())


def move_assertion(conn, world_name: str, assertion: dict[str, Any], to_position: int) -> dict[str, Any]:
    wid = world_id(conn, world_name)
    start, end = _range_bounds_for_move(assertion, to_position)
    entity_ids = [i for i in (assertion.get("subject_id"), assertion.get("object_id")) if i]
    affected_assertions = _load_related_assertions(
        conn,
        wid,
        entity_ids=entity_ids,
        start=start,
        end=end,
        exclude_assertion_ids=[assertion["id"]],
        object_assertion_ids=[assertion["id"]],
        reason="proposed_range_overlap",
    )
    affected_assertions.extend(_epistemic_assertions(conn, wid, assertion, start, end))
    affected_assertions = _dedupe(affected_assertions)

    scenes = _load_presence_scenes(
        conn, wid, entity_ids=entity_ids, start=start, end=end, reason="presence_in_proposed_range"
    )
    scenes.extend(_load_scenes_for_assertions(affected_assertions))
    scenes = _dedupe(scenes)

    disabled = families.load_disabled(conn, wid)
    predicted = []
    if assertion["predicate"] == "dies":
        predicted.extend(_predicted_dead_speaker(conn, wid, assertion, start, end))
    if assertion["predicate"] == "destroyed":
        predicted.extend(_predicted_destroyed_use(conn, wid, assertion, start, end))
    predicted.extend(_predicted_presence_conflicts(conn, wid, assertion, start, end))
    predicted.extend(_predicted_capability_conflicts(conn, wid, assertion, start, end))
    persisted = _existing_findings(
        conn,
        wid,
        assertion_ids=[assertion["id"]] + [a["id"] for a in affected_assertions],
        scene_ids=[s["id"] for s in scenes],
        disabled=disabled,
    )
    findings = _dedupe(predicted + persisted, key="explanation")
    return _report(
        world_name,
        {
            "type": "move_assertion",
            "assertion": _format_assertion(assertion, "changed_assertion"),
            "from_range": assertion["valid_during"],
            "to_range": f"[{start if start is not None else ''},{end if end is not None else ''})",
        },
        affected_assertions,
        scenes,
        findings,
    )


def remove_assertion(conn, world_name: str, assertion: dict[str, Any]) -> dict[str, Any]:
    wid = world_id(conn, world_name)
    entity_ids = [i for i in (assertion.get("subject_id"), assertion.get("object_id")) if i]
    affected_assertions = _load_related_assertions(
        conn,
        wid,
        entity_ids=entity_ids,
        start=assertion.get("start_pos"),
        end=assertion.get("end_pos"),
        exclude_assertion_ids=[assertion["id"]],
        object_assertion_ids=[assertion["id"]],
        reason="removed_assertion_dependency",
    )
    affected_assertions = _dedupe(affected_assertions)
    scenes = _load_presence_scenes(
        conn,
        wid,
        entity_ids=entity_ids,
        start=assertion.get("start_pos"),
        end=assertion.get("end_pos"),
        reason="presence_in_removed_range",
    )
    scenes.extend(_load_scenes_for_assertions(affected_assertions))
    scenes = _dedupe(scenes)
    findings = _existing_findings(
        conn,
        wid,
        assertion_ids=[assertion["id"]] + [a["id"] for a in affected_assertions],
        scene_ids=[s["id"] for s in scenes],
        disabled=families.load_disabled(conn, wid),
    )
    return _report(
        world_name,
        {"type": "remove_assertion", "assertion": _format_assertion(assertion, "removed_assertion")},
        affected_assertions,
        scenes,
        findings,
    )


def cut_entity(conn, world_name: str, entity_name: str, kind: str | None = None) -> dict[str, Any]:
    wid = world_id(conn, world_name)
    entity = resolve_entity(conn, wid, entity_name, kind=kind)
    affected_assertions = _load_cut_entity_assertions(conn, wid, entity)
    scenes = _load_presence_scenes(conn, wid, entity_ids=[entity["id"]], reason="entity_present")
    scenes.extend(_load_scenes_for_assertions(affected_assertions))
    scenes = _dedupe(scenes)
    findings = _existing_findings(
        conn,
        wid,
        assertion_ids=[a["id"] for a in affected_assertions],
        scene_ids=[s["id"] for s in scenes],
        disabled=families.load_disabled(conn, wid),
    )
    return _report(
        world_name,
        {"type": "cut_entity", "entity": entity},
        affected_assertions,
        scenes,
        findings,
    )


def _report(
    world_name: str,
    change: dict[str, Any],
    assertions: list[dict[str, Any]],
    scenes: list[dict[str, Any]],
    findings: list[dict[str, Any]],
) -> dict[str, Any]:
    return {
        "world": world_name,
        "change": change,
        "summary": {
            "assertions": len(assertions),
            "scenes": len(scenes),
            "check_findings": len(findings),
        },
        "conflicts": {
            "assertions": assertions,
            "scenes": scenes,
            "check_findings": findings,
        },
    }


def render_text(report: dict[str, Any]) -> str:
    s = report["summary"]
    lines = [
        f"retcon ripple: {report['world']}",
        f"change: {report['change']['type']}",
        f"conflicts: {s['assertions']} assertion(s), {s['scenes']} scene(s), {s['check_findings']} check finding(s)",
        "",
    ]
    assertions = report["conflicts"]["assertions"]
    lines.append("Assertions")
    if assertions:
        for row in assertions:
            cit = row["citation"]
            lines.append(f"- #{row['id']} {row['reason']}: {row['subject']} {row['predicate']} {row['object']} [{cit['label']}]")
    else:
        lines.append("- none")
    lines.append("")
    lines.append("Scenes")
    scenes = report["conflicts"]["scenes"]
    if scenes:
        for row in scenes:
            lines.append(f"- #{row['id']} {row['reason']}: {row['label']} (pos {row['story_position']})")
    else:
        lines.append("- none")
    lines.append("")
    lines.append("Check Findings")
    findings = report["conflicts"]["check_findings"]
    if findings:
        for row in findings:
            source = row.get("source") or "predicted"
            lines.append(f"- [{row['severity']}] {row['check']} ({source}): {row['explanation']}")
    else:
        lines.append("- none")
    return "\n".join(lines)


def to_json(report: dict[str, Any]) -> str:
    return json.dumps(report, indent=2, ensure_ascii=False)

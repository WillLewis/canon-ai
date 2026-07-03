"""Ask-the-Bible query engine.

Phase 0 deliberately uses conservative SQL templates instead of broad retrieval.
If a natural-language question cannot be mapped to a citable SQL query, the
engine refuses. If SQL returns no cited rows, the answer says canon cannot
support it. It never fills gaps from the question text.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import re
from typing import Any


MAX_ROWS = 50
STATEMENT_TIMEOUT_MS = 8000

_STATUS_EXCLUDE = "a.status NOT IN ('rejected','retconned')"
_WRITE_RE = re.compile(
    r"\b(insert|update|delete|merge|drop|alter|create|truncate|grant|revoke|"
    r"copy|vacuum|call|listen|notify|reset|begin|commit|rollback|lock)\b",
    re.IGNORECASE,
)
_PARAM_RE = re.compile(r"(?<!:):([A-Za-z_][A-Za-z0-9_]*)")


@dataclass(frozen=True)
class QueryPlan:
    intent: str
    sql: str
    params: dict[str, Any] = field(default_factory=dict)
    notes: str = ""


@dataclass
class AskResult:
    question: str
    status: str
    reason: str | None = None
    plan: QueryPlan | None = None
    columns: list[str] = field(default_factory=list)
    rows: list[dict[str, Any]] = field(default_factory=list)
    citations: list[str] = field(default_factory=list)
    truncated: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "question": self.question,
            "status": self.status,
            "reason": self.reason,
            "intent": self.plan.intent if self.plan else None,
            "sql": self.plan.sql if self.plan else None,
            "params": self.plan.params if self.plan else {},
            "columns": self.columns,
            "rows": self.rows,
            "citations": self.citations,
            "truncated": self.truncated,
        }


def world_id_for_name(conn, world_name: str) -> int | None:
    cur = conn.cursor()
    cur.execute("SELECT id FROM worlds WHERE name = %s", (world_name,))
    row = cur.fetchone()
    conn.rollback()
    return row[0] if row else None


def ask_question(conn, world_id: int, question: str, *, limit: int = MAX_ROWS) -> AskResult:
    plan = build_plan(question)
    if plan is None:
        return AskResult(
            question=question,
            status="refused",
            reason=(
                "cannot map this question to a Phase 0 cited SQL template; "
                "no answer shipped"
            ),
        )

    err = guard_sql(plan.sql)
    if err:
        return AskResult(question=question, status="refused", reason=err, plan=plan)

    try:
        columns, raw_rows, truncated = execute_readonly(conn, plan, world_id, limit)
    except Exception as e:
        try:
            conn.rollback()
        except Exception:
            pass
        return AskResult(
            question=question,
            status="refused",
            reason=f"database could not produce a cited answer: {str(e).splitlines()[0]}",
            plan=plan,
        )

    scene_idx = scene_col_index(columns)
    if scene_idx is None:
        return AskResult(
            question=question,
            status="refused",
            reason="query produced no scene_id column; no answer shipped",
            plan=plan,
            columns=columns,
        )

    if not raw_rows:
        return AskResult(
            question=question,
            status="no_support",
            reason="canon has no cited support for this question",
            plan=plan,
            columns=columns,
        )

    citations = resolve_citations(conn, [row[scene_idx] for row in raw_rows])
    rendered_rows: list[dict[str, Any]] = []
    citation_list: list[str] = []
    for row in raw_rows:
        scene_id = row[scene_idx]
        citation = citations.get(scene_id)
        if citation is None:
            return AskResult(
                question=question,
                status="refused",
                reason="at least one result row lacked a resolvable scene citation",
                plan=plan,
                columns=columns,
            )
        d = dict(zip(columns, row))
        d["citation"] = citation["full"]
        d["citation_label"] = citation["label"]
        rendered_rows.append(d)
        if citation["full"] not in citation_list:
            citation_list.append(citation["full"])

    return AskResult(
        question=question,
        status="answered",
        plan=plan,
        columns=columns,
        rows=rendered_rows,
        citations=citation_list,
        truncated=truncated,
    )


# ---------------------------------------------------------------------------
# Natural-language routing
# ---------------------------------------------------------------------------


def build_plan(question: str) -> QueryPlan | None:
    q = _compact(question)

    m = re.match(r"what does (.+?) (know|believe)s?\b(.*)", q)
    if m:
        subject = _clean_phrase(m.group(1))
        verb = "knows" if m.group(2).startswith("know") else "believes"
        rest = m.group(3)
        scene = _scene_position(rest)
        object_phrase = _about_phrase(rest)
        return _epistemic_plan(subject, verb, object_phrase=object_phrase, story_position=scene)

    m = re.match(r"who knows (.+?)(?: in the order.*)?$", q)
    if m:
        return _who_knows_plan(_knowledge_handle(m.group(1)))

    m = re.match(r"when does (.+?) die$", q)
    if m:
        return _simple_assertion_plan("dies", _clean_phrase(m.group(1)))

    m = re.match(r"where is (.+?) (?:hidden|located|kept)$", q)
    if m:
        return _located_at_plan(_clean_phrase(m.group(1)))

    m = re.match(r"what can(?:not|'t) (.+?) do$", q)
    if m:
        return _simple_assertion_plan("cannot", _clean_phrase(m.group(1)))

    m = re.match(r"what open promises does (.+?) have$", q)
    if m:
        return _open_assertion_plan("promised", _clean_phrase(m.group(1)))

    m = re.match(r"what is (.+?)s job$", q)
    if m:
        return _simple_assertion_plan("occupation", _clean_phrase(m.group(1)))

    m = re.match(r"what happened to (.+)$", q)
    if m:
        return _event_plan(_clean_phrase(m.group(1)))

    m = re.match(r"where does (.+?) appear after (?:(?:his|her|their)\s+)?death$", q)
    if m:
        return _appears_after_death_plan(_clean_phrase(m.group(1)))

    return None


def _compact(text: str) -> str:
    s = (text or "").strip().lower()
    s = s.replace("can't", "cannot")
    s = s.replace("'s", "s")
    s = re.sub(r"[^a-z0-9\s']", " ", s)
    return re.sub(r"\s+", " ", s).strip()


def _clean_phrase(text: str) -> str:
    text = _compact(text)
    text = re.sub(r"^(the|a|an)\s+", "", text)
    return text.strip()


def _knowledge_handle(text: str) -> str:
    text = _clean_phrase(text)
    text = text.replace("s location", " location")
    text = text.replace(" location of", " location")
    return text


def _scene_position(text: str) -> int | None:
    m = re.search(r"\b(?:scene|pos|position)\s+([0-9]+)\b", text)
    return int(m.group(1)) if m else None


def _about_phrase(text: str) -> str | None:
    m = re.search(r"\babout (.+?)(?: and when| at scene| at pos| at position|$)", text)
    return _clean_phrase(m.group(1)) if m else None


def _term_params(prefix: str, phrase: str) -> dict[str, str]:
    cleaned = _clean_phrase(phrase)
    return {
        f"{prefix}_exact": cleaned,
        f"{prefix}_pattern": f"%{cleaned}%",
    }


def _entity_ids_sql(prefix: str) -> str:
    return (
        "SELECT e_match.id FROM entities e_match "
        "LEFT JOIN aliases al_match ON al_match.entity_id = e_match.id "
        "WHERE e_match.world_id = :world_id "
        f"AND (lower(e_match.name) = lower(:{prefix}_exact) "
        f"OR lower(al_match.alias) = lower(:{prefix}_exact) "
        f"OR e_match.name ILIKE :{prefix}_pattern "
        f"OR al_match.alias ILIKE :{prefix}_pattern)"
    )


def _object_text_sql() -> str:
    return (
        "coalesce("
        "obj.name, "
        "nullif(concat_ws(' ', fact_subj.name, fact.predicate, "
        "coalesce(fact_obj.name, fact.object_value)), ''), "
        "a.object_value"
        ")"
    )


def _assertion_from_sql() -> str:
    return (
        "FROM assertions a "
        "JOIN entities subj ON subj.id = a.subject_id "
        "LEFT JOIN entities obj ON obj.id = a.object_id "
        "LEFT JOIN assertions fact ON fact.id = a.object_assertion_id "
        "LEFT JOIN entities fact_subj ON fact_subj.id = fact.subject_id "
        "LEFT JOIN entities fact_obj ON fact_obj.id = fact.object_id "
        "JOIN scenes sc ON sc.id = a.established_in_scene "
    )


def _base_assertion_select() -> str:
    obj = _object_text_sql()
    return (
        "SELECT subj.name AS subject, a.predicate, "
        f"{obj} AS object, "
        "lower(a.valid_during) AS from_pos, upper(a.valid_during) AS until_pos, "
        "a.supporting_quote, a.established_in_scene AS scene_id "
        + _assertion_from_sql()
    )


def _object_filter_sql() -> str:
    obj = _object_text_sql()
    return f"AND {obj} ILIKE :object_pattern "


def _epistemic_plan(
    subject: str,
    verb: str,
    *,
    object_phrase: str | None = None,
    story_position: int | None = None,
) -> QueryPlan:
    params: dict[str, Any] = _term_params("subject", subject)
    predicates = "('knows')" if verb == "knows" else "('believes')"
    where = [
        "a.world_id = :world_id",
        _STATUS_EXCLUDE,
        f"a.predicate IN {predicates}",
        f"a.subject_id IN ({_entity_ids_sql('subject')})",
    ]
    if object_phrase:
        params.update(_term_params("object", object_phrase))
        where.append(_object_filter_sql().removeprefix("AND ").strip())
    if story_position is not None:
        params["story_position"] = story_position
        where.append("a.valid_during @> :story_position")
    sql = (
        _base_assertion_select()
        + "WHERE "
        + " AND ".join(where)
        + " ORDER BY lower(a.valid_during) NULLS LAST, sc.story_position, a.id LIMIT 50"
    )
    notes = "resolves knows/believes intervals at a story position" if story_position else ""
    return QueryPlan("epistemic", sql, params, notes)


def _who_knows_plan(object_phrase: str) -> QueryPlan:
    params = _term_params("object", object_phrase)
    sql = (
        _base_assertion_select()
        + "WHERE a.world_id = :world_id AND "
        + _STATUS_EXCLUDE
        + " AND a.predicate = 'knows' "
        + _object_filter_sql()
        + "ORDER BY lower(a.valid_during) NULLS LAST, sc.story_position, subj.name LIMIT 50"
    )
    return QueryPlan("who_knows", sql, params)


def _simple_assertion_plan(predicate: str, subject: str) -> QueryPlan:
    params = _term_params("subject", subject)
    sql = (
        _base_assertion_select()
        + "WHERE a.world_id = :world_id AND "
        + _STATUS_EXCLUDE
        + " AND a.predicate = :predicate "
        + f"AND a.subject_id IN ({_entity_ids_sql('subject')}) "
        + "ORDER BY lower(a.valid_during) NULLS LAST, sc.story_position, a.id LIMIT 50"
    )
    params["predicate"] = predicate
    return QueryPlan(predicate, sql, params)


def _open_assertion_plan(predicate: str, subject: str) -> QueryPlan:
    params = _term_params("subject", subject)
    sql = (
        _base_assertion_select()
        + "WHERE a.world_id = :world_id AND "
        + _STATUS_EXCLUDE
        + " AND a.predicate = :predicate AND upper_inf(a.valid_during) "
        + f"AND a.subject_id IN ({_entity_ids_sql('subject')}) "
        + "ORDER BY lower(a.valid_during) NULLS LAST, sc.story_position, a.id LIMIT 50"
    )
    params["predicate"] = predicate
    return QueryPlan("open_" + predicate, sql, params)


def _located_at_plan(subject: str) -> QueryPlan:
    params = _term_params("subject", subject)
    sql = (
        "SELECT subj.name AS subject, a.predicate, obj.name AS object, "
        "lower(a.valid_during) AS from_pos, upper(a.valid_during) AS until_pos, "
        "a.supporting_quote, a.established_in_scene AS scene_id "
        "FROM assertions a "
        "JOIN entities subj ON subj.id = a.subject_id "
        "JOIN entities obj ON obj.id = a.object_id "
        "JOIN scenes sc ON sc.id = a.established_in_scene "
        "WHERE a.world_id = :world_id AND "
        + _STATUS_EXCLUDE
        + " AND a.predicate = 'located_at' "
        + f"AND a.subject_id IN ({_entity_ids_sql('subject')}) "
        + "ORDER BY lower(a.valid_during) NULLS LAST, sc.story_position, a.id LIMIT 50"
    )
    return QueryPlan("located_at", sql, params)


def _event_plan(subject: str) -> QueryPlan:
    params = _term_params("subject", subject)
    sql = (
        _base_assertion_select()
        + "WHERE a.world_id = :world_id AND "
        + _STATUS_EXCLUDE
        + " AND a.predicate IN ('dies','destroyed','created','fact') "
        + f"AND a.subject_id IN ({_entity_ids_sql('subject')}) "
        + "ORDER BY lower(a.valid_during) NULLS LAST, sc.story_position, a.id LIMIT 50"
    )
    return QueryPlan("event", sql, params)


def _appears_after_death_plan(subject: str) -> QueryPlan:
    params = _term_params("subject", subject)
    sql = (
        "SELECT e.name AS subject, 'appears_after_death' AS predicate, "
        "s.slug AS object, s.story_position AS from_pos, "
        "CAST(NULL AS integer) AS until_pos, CAST(NULL AS text) AS supporting_quote, "
        "s.id AS scene_id "
        "FROM scene_presence sp "
        "JOIN entities e ON e.id = sp.entity_id "
        "JOIN scenes s ON s.id = sp.scene_id "
        "JOIN works w ON w.id = s.work_id "
        "JOIN assertions d ON d.world_id = :world_id "
        "  AND d.subject_id = e.id AND d.predicate = 'dies' "
        "  AND d.status NOT IN ('rejected','retconned') "
        "WHERE w.world_id = :world_id "
        + f"AND e.id IN ({_entity_ids_sql('subject')}) "
        + "AND lower(d.valid_during) IS NOT NULL "
        + "AND s.story_position > lower(d.valid_during) "
        + "ORDER BY s.story_position LIMIT 50"
    )
    return QueryPlan("appears_after_death", sql, params)


# ---------------------------------------------------------------------------
# SQL execution and citation enforcement
# ---------------------------------------------------------------------------


def guard_sql(sql: str) -> str | None:
    clean = (sql or "").strip()
    if not clean:
        return "empty SQL"
    if ";" in clean:
        return "multiple statements are not allowed"
    if not re.match(r"(?is)^select\b", clean):
        return "query must be a single read-only SELECT"
    m = _WRITE_RE.search(clean)
    if m:
        return f"read-only only: {m.group(1)} is not allowed"
    if ":world_id" not in clean:
        return "query must be scoped to :world_id"
    if not re.search(r"\bscene_id\b", clean, re.IGNORECASE):
        return "query must return a scene_id citation column"
    return None


def _to_driver_sql(sql: str) -> str:
    return _PARAM_RE.sub(r"%(\1)s", sql)


def execute_readonly(conn, plan: QueryPlan, world_id: int, limit: int) -> tuple[list[str], list[tuple], bool]:
    params = {"world_id": world_id, **plan.params}
    cur = conn.cursor()
    try:
        cur.execute("SET TRANSACTION READ ONLY")
        cur.execute(f"SET LOCAL statement_timeout = {int(STATEMENT_TIMEOUT_MS)}")
        cur.execute(_to_driver_sql(plan.sql), params)
        columns = [d[0] for d in cur.description]
        rows = cur.fetchmany(limit + 1)
        truncated = len(rows) > limit
        return columns, rows[:limit], truncated
    finally:
        conn.rollback()


def scene_col_index(columns: list[str]) -> int | None:
    for i, col in enumerate(columns):
        if str(col).lower() == "scene_id":
            return i
    return None


def resolve_citations(conn, scene_ids: list[Any]) -> dict[Any, dict[str, str]]:
    ids: list[int] = []
    seen: set[int] = set()
    for scene_id in scene_ids:
        try:
            sid = int(scene_id)
        except (TypeError, ValueError):
            continue
        if sid not in seen:
            ids.append(sid)
            seen.add(sid)
    if not ids:
        return {}

    cur = conn.cursor()
    cur.execute(
        "SELECT id, title, slug, story_position, idx FROM ("
        "  SELECT s.id, w.title, s.slug, s.story_position,"
        "         row_number() OVER (PARTITION BY s.work_id ORDER BY s.story_position) AS idx "
        "  FROM scenes s JOIN works w ON w.id = s.work_id"
        ") t WHERE t.id = ANY(%s)",
        (ids,),
    )
    out: dict[Any, dict[str, str]] = {}
    for sid, title, slug, pos, idx in cur.fetchall():
        work = (title or "?").split()[0]
        label = f"{work}/sc{idx}"
        out[sid] = {"label": label, "full": f"{label} - {slug} (pos {pos})"}
    conn.rollback()
    return out


def render_answer(result: AskResult, *, show_sql: bool = False) -> str:
    lines = [f"Q: {result.question}", ""]
    if result.status == "answered":
        for row in result.rows:
            lines.append("  " + _render_row(row))
        if result.truncated:
            lines.append(f"  ... truncated at {MAX_ROWS} rows")
        lines.append("")
        lines.append("cited: " + "; ".join(result.citations))
    elif result.status == "no_support":
        lines.append(f"Cannot answer from cited canon: {result.reason}.")
    else:
        lines.append(f"REFUSED: {result.reason}.")

    if show_sql and result.plan is not None:
        lines.extend(["", "-- sql --", result.plan.sql, "-- params --", repr(result.plan.params)])
    return "\n".join(lines)


def _render_row(row: dict[str, Any]) -> str:
    subject = row.get("subject")
    predicate = row.get("predicate")
    obj = row.get("object")
    pieces = [str(subject), str(predicate)]
    if obj not in (None, ""):
        pieces.append(str(obj))
    if row.get("from_pos") is not None:
        pieces.append(f"from pos {row['from_pos']}")
    quote = row.get("supporting_quote")
    if quote:
        pieces.append(f"quote: {quote}")
    return " | ".join(pieces) + f" [{row['citation_label']}]"

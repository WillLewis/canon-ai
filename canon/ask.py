"""Ask the Bible — Phase 0 step 6 (PLAN item 4; SPEC R5).

`NL question --> SQL --> answer + scene citations`. Doctrine (D2/D7): the LLM
writes a read-only SQL query against the canon schema; Postgres produces the
facts; the harness resolves citations and renders. The LLM never answers from
its own reading of the story — if the query can't support a cited answer, we
refuse rather than improvise. R5: refuses to answer without a citation.

Enforcement is mechanical, not vibes:
  - guard: single SELECT/WITH, no writes, no extra statements, must scope to
    `:world_id`; executed inside a READ ONLY transaction with a statement
    timeout (the transaction, not the regex, is the real backstop).
  - citations: the query must return a `scene_id` column; every returned scene id
    resolves to "E101/sc3 · SLUG (pos N)" labels. No scene ids -> no answer.
  - optional narration: a second LLM call phrases the rows, but only from the
    rows, with citation tags; deterministic table rendering is the fallback.
"""

from __future__ import annotations

import json
import re

from .check import to_psycopg
from .extract import DEFAULT_EFFORT, DEFAULT_MODEL, PREDICATES, first_text

MAX_ROWS = 50
STATEMENT_TIMEOUT_MS = 8000
MAX_ATTEMPTS = 2  # initial + one corrective retry

SCHEMA_CARD = f"""TABLES (Postgres):
  worlds(id, name)
  works(id, world_id, title, sort_order)        -- episode/chapter; title like 'E101 — The Ledger'
  scenes(id, work_id, slug, story_position, is_flashback)
      -- story_position: ONE global integer axis per world, in reading order
  entities(id, world_id, kind, name, provisional)
      -- kind: character|location|object|faction|event|rule|other
  aliases(entity_id, alias, kind)
  assertions(id, world_id, subject_id->entities, predicate, object_id->entities,
             object_value, polarity, valid_during int4range,   -- story-position validity '[start,end)'
             established_in_scene->scenes, supporting_quote, confidence,
             status in ('draft','canon','sealed','retconned','rejected'))
  scene_presence(scene_id, entity_id)           -- who/what is present in each scene

PREDICATES: {', '.join(PREDICATES)}

YOUR SQL MUST:
1. Be ONE read-only SELECT (WITH ... SELECT allowed). No writes. No semicolons.
2. Scope to the world via the literal placeholder :world_id
   (assertions.world_id / entities.world_id / works.world_id = :world_id).
3. Return a column named scene_id citing where each row was established —
   usually `a.established_in_scene AS scene_id`, or `s.id AS scene_id` for
   scene/presence queries. Rows without scene_id cannot be shown to the writer.
4. Include a.supporting_quote when selecting from assertions.
5. Exclude a.status IN ('rejected','retconned').
6. Match character/location/object names through BOTH entities and aliases,
   case-insensitively:
     a.subject_id IN (SELECT e.id FROM entities e
                      LEFT JOIN aliases al ON al.entity_id = e.id
                      WHERE e.world_id = :world_id
                        AND (e.name ILIKE '%pattern%' OR al.alias ILIKE '%pattern%'))
7. Temporal phrasing: "when did X begin/learn" = lower(valid_during);
   "true at position P" = valid_during @> P; "still open" = upper_inf(valid_during).
8. ORDER BY story position when the question implies sequence. LIMIT {MAX_ROWS}.
9. object_value holds short free-text handles — match them BROADLY (single-keyword
   ILIKE like '%ledger%', or a few ORed keywords), never an exact phrase. An
   over-narrow filter that returns 0 rows reads as "canon has no answer".
"""

ASK_SYSTEM_PROMPT = (
    "You translate a fiction writer's question about THEIR OWN story canon into one "
    "read-only Postgres SELECT over the Canon AI schema below. You never answer the "
    "question yourself and never invent story content — the database answers; you "
    "only write the query. Follow every rule in the schema card exactly.\n\n"
    + SCHEMA_CARD
)

NARRATE_SYSTEM_PROMPT = (
    "You are Canon AI's Ask-the-Bible narrator. Answer the writer's question using "
    "ONLY the rows provided — they are query results from the writer's own canon "
    "database. Never use outside knowledge and never invent, embellish, or continue "
    "story content; you are an index, not an author. End every factual claim with "
    "its citation tag exactly as given in the rows, e.g. [E102/sc1]. Quote "
    "supporting_quote text when it helps. If the rows do not actually answer the "
    "question, say plainly that canon does not establish it. Be brief: 1–4 sentences."
)

_WRITE_RE = re.compile(
    r"(?i)\b(insert|update|delete|merge|drop|alter|create|truncate|grant|revoke|"
    r"copy|vacuum|call|listen|notify|reset|begin|commit|rollback|lock|prepare|deallocate)\b"
)


def sql_schema() -> dict:
    return {
        "type": "object",
        "additionalProperties": False,
        "properties": {"sql": {"type": "string"}, "notes": {"type": "string"}},
        "required": ["sql", "notes"],
    }


# ---------------------------------------------------------------------------
# Guard + execution
# ---------------------------------------------------------------------------

def guard_sql(sql: str) -> tuple:
    """Return (clean_sql, error). error is None when the query is acceptable."""
    s = (sql or "").strip().rstrip(";").strip()
    if not s:
        return s, "empty SQL"
    if ";" in s:
        return s, "multiple statements are not allowed"
    if not re.match(r"(?is)^(select|with)\b", s):
        return s, "must be a single read-only SELECT"
    m = _WRITE_RE.search(s)
    if m:
        return s, f"read-only only — '{m.group(1)}' is not allowed"
    if ":world_id" not in s:
        return s, "must scope to the current world via :world_id"
    return s, None


def execute_readonly(conn, sql: str, world_id: int, limit: int = MAX_ROWS) -> tuple:
    """Run inside a READ ONLY tx with a timeout. Returns (columns, rows, truncated)."""
    cur = conn.cursor()
    try:
        cur.execute("SET TRANSACTION READ ONLY")
        cur.execute(f"SET LOCAL statement_timeout = {int(STATEMENT_TIMEOUT_MS)}")
        cur.execute(to_psycopg(sql), {"world_id": world_id})
        columns = [d[0] for d in cur.description]
        rows = cur.fetchmany(limit + 1)
        truncated = len(rows) > limit
        return columns, rows[:limit], truncated
    finally:
        conn.rollback()  # never leave the RO transaction open


def scene_col_index(columns: list) -> int | None:
    for i, c in enumerate(columns):
        if str(c).lower() in ("scene_id", "established_in_scene", "cite_scene_id"):
            return i
    return None


def resolve_citations(conn, scene_ids: list) -> dict:
    """scene_id -> {'label': 'E101/sc3', 'full': 'E101/sc3 · SLUG (pos 3)'}.

    Values that aren't coercible to int (a query that aliased something odd as
    scene_id) are simply unciteable — skipped here, which surfaces as a refusal
    upstream rather than a crash.
    """
    ids = set()
    for i in scene_ids:
        try:
            ids.add(int(i))
        except (TypeError, ValueError):
            continue
    ids = sorted(ids)
    if not ids:
        return {}
    cur = conn.cursor()
    cur.execute(
        "SELECT id, title, slug, story_position, idx FROM ("
        "  SELECT s.id, w.title, s.slug, s.story_position,"
        "         row_number() OVER (PARTITION BY s.work_id ORDER BY s.story_position) AS idx"
        "  FROM scenes s JOIN works w ON w.id = s.work_id) t "
        "WHERE t.id = ANY(%(ids)s)",
        {"ids": ids},
    )
    out = {}
    for sid, title, slug, pos, idx in cur.fetchall():
        head = (title or "?").split()[0]
        label = f"{head}/sc{idx}"
        out[sid] = {"label": label, "full": f"{label} · {slug} (pos {pos})"}
    conn.rollback()
    return out


# ---------------------------------------------------------------------------
# LLM calls (injected client)
# ---------------------------------------------------------------------------

def build_sql_prompt(question: str, feedback: str | None = None) -> str:
    p = f"WRITER'S QUESTION:\n{question}\n\nWrite the SQL."
    if feedback:
        p += f"\n\nYOUR PREVIOUS ATTEMPT WAS REJECTED: {feedback}\nFix it and return corrected SQL."
    return p


def generate_sql(client, question: str, feedback: str | None, model: str,
                 effort: str, thinking: bool) -> dict:
    output_config: dict = {"format": {"type": "json_schema", "schema": sql_schema()}}
    if effort:
        output_config["effort"] = effort
    req: dict = {
        "model": model,
        "max_tokens": 4000,
        "system": ASK_SYSTEM_PROMPT,
        "messages": [{"role": "user", "content": build_sql_prompt(question, feedback)}],
        "output_config": output_config,
    }
    if thinking:
        req["thinking"] = {"type": "adaptive"}
    resp = client.messages.create(**req)
    text = first_text(resp)
    if text is None:
        raise RuntimeError("no text block in SQL-generation response")
    return json.loads(text)


def narrate(client, question: str, lines: list, model: str, effort: str, thinking: bool) -> str:
    req: dict = {
        "model": model,
        "max_tokens": 1500,
        "system": NARRATE_SYSTEM_PROMPT,
        "messages": [{"role": "user",
                      "content": f"QUESTION:\n{question}\n\nROWS:\n" + "\n".join(lines)}],
    }
    if effort:
        req["output_config"] = {"effort": effort}
    if thinking:
        req["thinking"] = {"type": "adaptive"}
    resp = client.messages.create(**req)
    return (first_text(resp) or "").strip()


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------

def _row_lines(columns: list, rows: list, scene_i: int, cites: dict) -> list:
    lines = []
    for r in rows:
        parts = []
        quote = None
        for i, col in enumerate(columns):
            if i == scene_i:
                continue
            if str(col).lower() == "supporting_quote":
                quote = r[i]
                continue
            parts.append(f"{col}={r[i]}")
        cite = cites.get(r[scene_i], {}).get("label", "?") if r[scene_i] is not None else "?"
        line = "  ".join(parts)
        if quote:
            line += f'  — "{quote}"'
        lines.append(f"{line}  [{cite}]")
    return lines


def ask(conn, client, question: str, world_id: int, *, model: str = DEFAULT_MODEL,
        effort: str = DEFAULT_EFFORT, thinking: bool = True, limit: int = MAX_ROWS,
        sql_override: str | None = None, do_narrate: bool = True,
        max_attempts: int = MAX_ATTEMPTS) -> dict:
    """Returns {status: answered|no_support|refused, sql, columns, rows, row_lines,
    citations, narration, reason, attempts}."""
    attempts: list = []
    feedback = None
    n_tries = 1 if sql_override else max_attempts

    for _ in range(n_tries):
        if sql_override:
            sql, notes = sql_override, "(provided via --sql)"
        else:
            dec = generate_sql(client, question, feedback, model, effort, thinking)
            sql, notes = dec.get("sql", ""), dec.get("notes", "")

        sql, err = guard_sql(sql)
        if err:
            attempts.append({"sql": sql, "error": err})
            feedback = err
            continue
        try:
            columns, rows, truncated = execute_readonly(conn, sql, world_id, limit)
        except Exception as e:
            try:
                conn.rollback()
            except Exception:
                pass
            err = f"database error: {str(e).splitlines()[0]}"
            attempts.append({"sql": sql, "error": err})
            feedback = err
            continue

        scene_i = scene_col_index(columns)
        if scene_i is None:
            err = "result has no scene_id column — every answer must cite its scenes (rule 3)"
            attempts.append({"sql": sql, "error": err})
            feedback = err
            continue

        if not rows:
            return {"status": "no_support", "sql": sql, "columns": columns, "rows": [],
                    "row_lines": [], "citations": [], "narration": None,
                    "reason": "the query ran but returned no rows — canon does not "
                              "establish an answer", "attempts": attempts, "notes": notes,
                    "truncated": False}

        cites = resolve_citations(conn, [r[scene_i] for r in rows])
        labels = []
        for r in rows:
            c = cites.get(r[scene_i])
            if c and c["full"] not in labels:
                labels.append(c["full"])
        if not labels:
            return {"status": "refused", "sql": sql, "columns": columns, "rows": rows,
                    "row_lines": [], "citations": [], "narration": None,
                    "reason": "rows had no resolvable scene citations — refusing to "
                              "answer uncited", "attempts": attempts, "notes": notes,
                    "truncated": truncated}

        lines = _row_lines(columns, rows, scene_i, cites)
        narration = None
        if do_narrate and client is not None:
            try:
                narration = narrate(client, question, lines, model, effort, thinking)
            except Exception:
                narration = None  # deterministic rendering still stands
        return {"status": "answered", "sql": sql, "columns": columns, "rows": rows,
                "row_lines": lines, "citations": labels, "narration": narration,
                "reason": None, "attempts": attempts, "notes": notes,
                "truncated": truncated}

    return {"status": "refused", "sql": attempts[-1]["sql"] if attempts else None,
            "columns": [], "rows": [], "row_lines": [], "citations": [],
            "narration": None,
            "reason": "could not produce a valid, citable query: "
                      + "; ".join(a["error"] for a in attempts),
            "attempts": attempts, "notes": None, "truncated": False}


def render_answer(result: dict, question: str, show_sql: bool = False) -> str:
    lines = [f"Q: {question}", ""]
    if result["status"] == "answered":
        if result.get("narration"):
            lines.append(result["narration"])
            lines.append("")
        lines.extend(f"  {ln}" for ln in result["row_lines"])
        if result.get("truncated"):
            lines.append(f"  ... (truncated at {MAX_ROWS} rows)")
        lines.append("")
        lines.append("cited: " + "; ".join(result["citations"]))
    elif result["status"] == "no_support":
        lines.append(f"No cited answer: {result['reason']}.")
    else:
        lines.append(f"REFUSED: {result['reason']}.")
    if show_sql and result.get("sql"):
        lines += ["", "-- sql --", result["sql"]]
    return "\n".join(lines)

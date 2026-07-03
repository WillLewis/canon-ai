"""Read-only data access for the triage workbench (+ the single seal/unseal write).

Every function here opens a short-lived connection, runs one batch of queries, and
closes it — no pool, because an internal single-user inspector does not need one.
Rows come back as dicts (psycopg3 `dict_row`) so the templates can read columns by
name.

Connection reuse, not reinvention: we import `connect`/`resolve_db_url` from
`canon.ingest` (read-only) so the workbench speaks to the same database the
pipeline loads. The only departure is the default URL — `resolve_db_url` falls
back to nothing, and `canon.ingest`'s own fallback points at the Supabase default
port (54322), whereas the loaded `greyharbor_s1` world lives on 5432. So we supply
our own documented local default and let CANON_DB_URL / DATABASE_URL override it.

This module issues SELECTs only, except `seal_finding` / `unseal_finding`, which
write the `seals` table exactly as the checker reads it (the same
`(check_name, assertion_a, coalesce(assertion_b,0))` key used in canon/check.py)
and mirror the result onto `findings.sealed` so a re-check is not required to see
the change — and `set_note_status`, the note surface's one write: the permanent
open -> sealed/dismissed transition on `coverage_notes`, with attribution — and
`rule_assertion`, the confirm queue's one write: the guarded draft -> canon/
rejected transition, audited via ops.metering.
"""

from __future__ import annotations

import contextlib
import pathlib
import sys

# Make `canon` importable no matter the working directory the server is launched
# from (python -m ui.app, uvicorn ui.app:app, an IDE run config, ...).
_REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from canon.ingest import connect, resolve_db_url  # noqa: E402  (read-only import)
from ops import metering  # noqa: E402  (confirm-queue audit rows; never edited here)

# Audit rows for confirm-queue rulings are zero-token, so their cost is 0 with
# any model — but record_usage suffixes the kind with ':unpriced' when the
# model is unknown, which would make 'confirm_ruling' rows un-greppable. Pass a
# priced model id to keep the kind clean; usage_events stores no model column,
# so nothing false is recorded.
_AUDIT_MODEL = next(iter(metering.PRICING))

# The loaded dev world lives here (task brief); env vars still win via resolve_db_url.
DEFAULT_LOCAL_URL = "postgresql://postgres:postgres@127.0.0.1:5432/postgres"


def db_url() -> str:
    return resolve_db_url(None) or DEFAULT_LOCAL_URL


try:
    from psycopg.rows import dict_row
except ModuleNotFoundError as e:  # pragma: no cover - psycopg3 is a project dep
    raise RuntimeError(
        "psycopg (v3) is required for the UI. Run `pip install -r requirements.txt`."
    ) from e


@contextlib.contextmanager
def db_conn():
    """Yield a dict-row connection and always close it."""
    conn = connect(db_url())
    try:
        conn.row_factory = dict_row
        yield conn
    finally:
        conn.close()


def _all(conn, sql: str, params: dict | None = None) -> list[dict]:
    cur = conn.cursor()
    cur.execute(sql, params or {})
    return cur.fetchall()


def _one(conn, sql: str, params: dict | None = None) -> dict | None:
    cur = conn.cursor()
    cur.execute(sql, params or {})
    return cur.fetchone()


# ---------------------------------------------------------------------------
# Worlds
# ---------------------------------------------------------------------------

def list_worlds() -> list[dict]:
    with db_conn() as conn:
        return _all(conn, """
            select w.id, w.name,
              (select count(*) from works   wk where wk.world_id = w.id) as n_works,
              (select count(*) from scenes  s join works wk on wk.id = s.work_id
                 where wk.world_id = w.id)                               as n_scenes,
              (select count(*) from entities e where e.world_id = w.id)  as n_entities,
              (select count(*) from assertions a where a.world_id = w.id) as n_assertions,
              (select count(*) from findings f where f.world_id = w.id)  as n_findings
            from worlds w
            order by w.name
        """)


def get_world(name: str) -> dict | None:
    with db_conn() as conn:
        return _one(conn, "select id, name from worlds where name = %(n)s", {"n": name})


def get_world_by_id(world_id: int) -> dict | None:
    with db_conn() as conn:
        return _one(conn, "select id, name from worlds where id = %(id)s", {"id": world_id})


def member_role(world_id: int, user_id: str) -> str | None:
    """The user's role on a world: worlds.owner_id counts as 'owner', otherwise
    the world_members row (mirrors the canon_world_role() SQL helper the RLS
    policies use, so the app and the database agree on who may do what)."""
    with db_conn() as conn:
        row = _one(conn, """
            select coalesce(
              (select 'owner' from worlds w
                 where w.id = %(w)s and w.owner_id = %(u)s::uuid),
              (select m.role from world_members m
                 where m.world_id = %(w)s and m.user_id = %(u)s::uuid)) as role
        """, {"w": world_id, "u": user_id})
    return row["role"] if row else None


def world_summary(world_id: int) -> dict:
    """Dashboard counts: entities by kind, assertions by status, findings by severity."""
    with db_conn() as conn:
        entities_by_kind = _all(conn, """
            select kind, count(*) n, count(*) filter (where provisional) provisional
            from entities where world_id = %(w)s group by kind order by n desc
        """, {"w": world_id})
        assertions_by_status = _all(conn, """
            select status, count(*) n from assertions where world_id = %(w)s
            group by status order by n desc
        """, {"w": world_id})
        findings_by_sev = _all(conn, """
            select severity,
                   count(*) filter (where not sealed) live,
                   count(*) filter (where sealed) sealed
            from findings where world_id = %(w)s group by severity
        """, {"w": world_id})
        totals = _one(conn, """
            select
              (select count(*) from entities  where world_id = %(w)s) entities,
              (select count(*) from assertions where world_id = %(w)s) assertions,
              (select count(*) from scenes s join works wk on wk.id = s.work_id
                 where wk.world_id = %(w)s) scenes,
              (select count(*) from works where world_id = %(w)s) works,
              (select count(*) from findings where world_id = %(w)s and not sealed) findings_live,
              (select count(*) from findings where world_id = %(w)s and sealed) findings_sealed
        """, {"w": world_id})
        works = _all(conn, """
            select wk.id, wk.title, wk.sort_order,
              (select count(*) from scenes s where s.work_id = wk.id) n_scenes
            from works wk where wk.world_id = %(w)s order by wk.sort_order
        """, {"w": world_id})
    return {
        "entities_by_kind": entities_by_kind,
        "assertions_by_status": assertions_by_status,
        "findings_by_sev": findings_by_sev,
        "totals": totals,
        "works": works,
    }


# ---------------------------------------------------------------------------
# Entities
# ---------------------------------------------------------------------------

def list_entities(world_id: int, kind: str | None = None, q: str | None = None) -> list[dict]:
    params: dict = {"w": world_id}
    where = ["e.world_id = %(w)s"]
    if kind:
        where.append("e.kind = %(kind)s")
        params["kind"] = kind
    if q:
        where.append(
            "(e.name ilike %(q)s or exists "
            "(select 1 from aliases al where al.entity_id = e.id and al.alias ilike %(q)s))"
        )
        params["q"] = f"%{q}%"
    with db_conn() as conn:
        return _all(conn, f"""
            select e.id, e.kind, e.name, e.dossier, e.provisional,
              (select count(*) from aliases    al where al.entity_id = e.id) n_aliases,
              (select count(*) from assertions a  where a.subject_id = e.id) n_subject,
              (select count(*) from assertions a  where a.object_id  = e.id) n_object
            from entities e
            where {' and '.join(where)}
            order by e.kind, e.name
        """, params)


def kinds_in_world(world_id: int) -> list[str]:
    with db_conn() as conn:
        rows = _all(conn,
            "select distinct kind from entities where world_id = %(w)s order by kind",
            {"w": world_id})
    return [r["kind"] for r in rows]


def get_entity(world_id: int, entity_id: int) -> dict | None:
    with db_conn() as conn:
        entity = _one(conn, """
            select id, kind, name, dossier, provisional
            from entities where id = %(id)s and world_id = %(w)s
        """, {"id": entity_id, "w": world_id})
        if not entity:
            return None
        aliases = _all(conn, """
            select alias, kind from aliases where entity_id = %(id)s order by kind, alias
        """, {"id": entity_id})
        as_subject = _all(conn, _ASSERTION_SELECT + """
            where a.world_id = %(w)s and a.subject_id = %(id)s
            order by s.story_position, a.id
        """, {"id": entity_id, "w": world_id})
        as_object = _all(conn, _ASSERTION_SELECT + """
            where a.world_id = %(w)s and a.object_id = %(id)s
            order by s.story_position, a.id
        """, {"id": entity_id, "w": world_id})
    entity["aliases"] = aliases
    entity["as_subject"] = as_subject
    entity["as_object"] = as_object
    return entity


# ---------------------------------------------------------------------------
# Assertions
# ---------------------------------------------------------------------------

# Shared projection so list / detail / entity views render assertions identically.
_ASSERTION_SELECT = """
    select a.id, a.predicate, a.polarity,
           a.valid_during::text as valid_during,
           a.object_value, a.object_assertion_id,
           a.confidence, a.status, a.confirmed_by_human, a.superseded_by,
           a.supporting_quote,
           subj.id subject_id, subj.name subject_name, subj.kind subject_kind,
           obj.id  object_id,  obj.name  object_name,  obj.kind  object_kind,
           s.id scene_id, s.slug scene_slug, s.story_position, s.is_flashback,
           wk.title episode_title
    from assertions a
    join entities subj on subj.id = a.subject_id
    left join entities obj on obj.id = a.object_id
    join scenes s on s.id = a.established_in_scene
    join works  wk on wk.id = s.work_id
"""


def list_assertions(world_id: int, subject_id: int | None = None,
                    predicate: str | None = None, status: str | None = None,
                    q: str | None = None, limit: int = 100, offset: int = 0) -> tuple[list[dict], int]:
    params: dict = {"w": world_id, "limit": limit, "offset": offset}
    where = ["a.world_id = %(w)s"]
    if subject_id:
        where.append("a.subject_id = %(subj)s")
        params["subj"] = subject_id
    if predicate:
        where.append("a.predicate = %(pred)s")
        params["pred"] = predicate
    if status:
        where.append("a.status = %(status)s")
        params["status"] = status
    if q:
        where.append(
            "(subj.name ilike %(q)s or obj.name ilike %(q)s "
            "or a.object_value ilike %(q)s or a.supporting_quote ilike %(q)s "
            "or a.predicate ilike %(q)s)"
        )
        params["q"] = f"%{q}%"
    clause = " and ".join(where)
    with db_conn() as conn:
        total = _one(conn, f"""
            select count(*) n from assertions a
            join entities subj on subj.id = a.subject_id
            left join entities obj on obj.id = a.object_id
            where {clause}
        """, params)["n"]
        rows = _all(conn, _ASSERTION_SELECT + f"""
            where {clause}
            order by s.story_position, a.id
            limit %(limit)s offset %(offset)s
        """, params)
    return rows, total


def predicates_in_world(world_id: int) -> list[str]:
    with db_conn() as conn:
        rows = _all(conn,
            "select distinct predicate from assertions where world_id = %(w)s order by predicate",
            {"w": world_id})
    return [r["predicate"] for r in rows]


def get_assertion(world_id: int, assertion_id: int) -> dict | None:
    with db_conn() as conn:
        row = _one(conn, _ASSERTION_SELECT + """
            where a.world_id = %(w)s and a.id = %(id)s
        """, {"w": world_id, "id": assertion_id})
        if not row:
            return None
        row["scene_raw_text"] = _one(conn,
            "select raw_text from scenes where id = %(s)s",
            {"s": row["scene_id"]})["raw_text"]
        # Findings that cite this assertion (either anchor).
        row["findings"] = _all(conn, """
            select id, check_name, severity, sealed
            from findings
            where world_id = %(w)s and (assertion_a = %(id)s or assertion_b = %(id)s)
            order by id
        """, {"w": world_id, "id": assertion_id})
    return row


# ---------------------------------------------------------------------------
# Confirm queue (P3-CONFIRM): drafts are the queue. canon/store.py loads every
# assertion below the confidence gate (0.85) as status 'draft'; the queue view
# lists them and the writer's ruling settles each one — 'canon' or 'rejected'.
# ---------------------------------------------------------------------------

# ruling status -> the usage_events audit kind recorded for it.
ASSERTION_RULINGS = {"canon": "confirm_ruling", "rejected": "reject_ruling"}


def list_draft_assertions(world_id: int) -> list[dict]:
    """The confirm queue: every 'draft' assertion, most-confident first, with
    the same projection the assertion views use (quote + scene included)."""
    with db_conn() as conn:
        return _all(conn, _ASSERTION_SELECT + """
            where a.world_id = %(w)s and a.status = 'draft'
            order by a.confidence desc, a.id
        """, {"w": world_id})


def count_drafts(world_id: int) -> int:
    """How many drafts await a ruling — the report view's 'Verify your canon (N)' pill."""
    with db_conn() as conn:
        row = _one(conn, """
            select count(*) n from assertions
            where world_id = %(w)s and status = 'draft'
        """, {"w": world_id})
    return row["n"] if row else 0


def last_story_position(world_id: int) -> int | None:
    """The corpus's last story position — the empty queue's 'clean through scene X'."""
    with db_conn() as conn:
        row = _one(conn, """
            select max(s.story_position) p
            from scenes s join works wk on wk.id = s.work_id
            where wk.world_id = %(w)s
        """, {"w": world_id})
    return row["p"] if row else None


def rule_assertion(world_id: int, assertion_id: int, status: str,
                   ruled_by: str | None = None) -> bool:
    """Confirm or reject one draft assertion — the confirm queue's only write.

    The WHERE guards status='draft', so rulings are idempotent and can never
    flip a settled row (canon, rejected, retconned). Confirming also sets
    confirmed_by_human, matching canon.store.status_for's confirmed -> canon
    promotion. Attribution: the assertions table has no confirmed_by column yet
    (MIGRATIONS-NEEDED.md — set confirmed_by/confirmed_at here once the columns
    land), so each applied ruling writes a usage_events audit row instead
    (kind per ASSERTION_RULINGS, zero tokens, user + world attributed).
    """
    if status not in ASSERTION_RULINGS:
        raise ValueError(
            f"status must be one of {sorted(ASSERTION_RULINGS)}, got {status!r}")
    with db_conn() as conn:
        cur = conn.cursor()
        cur.execute("""
            update assertions
               set status = %(s)s,
                   confirmed_by_human = confirmed_by_human or %(confirm)s
             where id = %(id)s and world_id = %(w)s and status = 'draft'
             returning id
        """, {"s": status, "confirm": status == "canon",
              "id": assertion_id, "w": world_id})
        changed = cur.fetchone() is not None
        if changed:
            metering.record_usage(
                cur, user_id=ruled_by, world_id=world_id,
                kind=ASSERTION_RULINGS[status],
                tokens_in=0, tokens_out=0, model=_AUDIT_MODEL)
        conn.commit()
    return changed


# ---------------------------------------------------------------------------
# Scenes
# ---------------------------------------------------------------------------

def list_scenes(world_id: int) -> list[dict]:
    with db_conn() as conn:
        return _all(conn, """
            select s.id, s.slug, s.story_position, s.is_flashback,
              wk.id work_id, wk.title episode_title, wk.sort_order,
              (select count(*) from scene_presence sp where sp.scene_id = s.id) n_present,
              (select count(*) from assertions a where a.established_in_scene = s.id) n_assertions
            from scenes s join works wk on wk.id = s.work_id
            where wk.world_id = %(w)s
            order by s.story_position
        """, {"w": world_id})


def get_scene(world_id: int, scene_id: int) -> dict | None:
    with db_conn() as conn:
        scene = _one(conn, """
            select s.id, s.slug, s.story_position, s.is_flashback, s.raw_text,
              wk.title episode_title, wk.id work_id
            from scenes s join works wk on wk.id = s.work_id
            where s.id = %(id)s and wk.world_id = %(w)s
        """, {"id": scene_id, "w": world_id})
        if not scene:
            return None
        scene["present"] = _all(conn, """
            select e.id, e.name, e.kind, e.provisional
            from scene_presence sp join entities e on e.id = sp.entity_id
            where sp.scene_id = %(id)s
            order by e.kind, e.name
        """, {"id": scene_id})
        scene["assertions"] = _all(conn, _ASSERTION_SELECT + """
            where a.established_in_scene = %(id)s
            order by a.id
        """, {"id": scene_id})
        scene["findings"] = _all(conn, """
            select id, check_name, severity, explanation, sealed
            from findings where world_id = %(w)s and scene_id = %(id)s
            order by id
        """, {"id": scene_id, "w": world_id})
    return scene


# ---------------------------------------------------------------------------
# Findings  (+ the one write op: seal / unseal)
# ---------------------------------------------------------------------------

_SEV_ORDER = "case severity when 'critical' then 0 when 'warning' then 1 else 2 end"


def list_findings(world_id: int) -> list[dict]:
    """Findings with the subject name of each anchor assertion, for at-a-glance context."""
    with db_conn() as conn:
        return _all(conn, f"""
            select f.id, f.check_name, f.severity, f.explanation, f.sealed,
                   f.scene_id, f.assertion_a, f.assertion_b,
                   s.slug scene_slug, s.story_position,
                   wk.title episode_title,
                   sa.name subject_a, sb.name subject_b
            from findings f
            left join scenes s on s.id = f.scene_id
            left join works  wk on wk.id = s.work_id
            left join assertions aa on aa.id = f.assertion_a
            left join entities   sa on sa.id = aa.subject_id
            left join assertions ab on ab.id = f.assertion_b
            left join entities   sb on sb.id = ab.subject_id
            where f.world_id = %(w)s
            order by {_SEV_ORDER}, f.check_name, s.story_position nulls last, f.id
        """, {"w": world_id})


# --- writer-authored rules composed into the findings views (P3-WIRING) -----

_RULE_FINDING_DEFAULTS = {
    "id": None, "scene_slug": None, "story_position": None,
    "episode_title": None, "subject_a": None, "subject_b": None,
    "sealed": False, "assertion_a": None, "assertion_b": None, "scene_id": None,
}


def rules_conn():
    """Tuple-row connection for canon.rules composition (module-level seam so
    tests can fake it). None when no database is reachable — the findings
    views then render the plain checker rows unchanged."""
    try:
        return connect(db_url())
    except Exception:
        return None


def _is_undefined_table(exc: Exception) -> bool:
    """True for Postgres 42P01 (undefined_table) from psycopg 2 or 3."""
    code = getattr(exc, "sqlstate", None) or getattr(exc, "pgcode", None)
    return code == "42P01"


def _normalized_finding(f: dict) -> dict:
    """Rule findings arrive keyed 'check' (canon/check.py shape); give them the
    ui/db.list_findings column names so every template renders them alike."""
    if "check_name" in f:
        return f
    out = _RULE_FINDING_DEFAULTS | dict(f)
    out["check_name"] = out.pop("check", None)
    return out


def list_findings_composed(world_id: int) -> list[dict]:
    """Findings for the report/findings/script views with the world's
    writer-authored rules composed in (canon.rules.compose_findings): enabled
    CANNOT/ONLY rules contribute findings, EXCEPTION rules filter them.

    Feature-detected: the world_rules table lands with Wave 5's schema sync,
    so on an undefined-table error — or with no database reachable at all —
    the plain findings return unchanged, silently. Any other composition
    failure also falls back to plain findings (a broken rules pass must never
    take the report down), but is logged.
    """
    rows = list_findings(world_id)
    conn = rules_conn()
    if conn is None:
        return rows
    try:
        from canon import rules as rules_engine  # no LLM in there (grep-guarded)

        composed = rules_engine.compose_findings(conn.cursor(), world_id, rows)
    except Exception as exc:
        if not _is_undefined_table(exc):
            import logging

            logging.getLogger("canon.ui").warning(
                "rule composition failed; rendering plain findings: %r", exc)
        return rows
    finally:
        try:
            conn.close()
        except Exception:
            pass
    return [_normalized_finding(f) for f in composed]


# --- ops / billing cursor (P3-WIRING): the rate-limit guard's store ----------

_OPS_CONN = None


def ops_cursor():
    """A plain cursor over the ops/billing tables (usage_events,
    billing_subscriptions) for ops.middleware.rate_limited and
    billing.store.make_tier_resolver. One lazily-opened autocommit connection,
    reused across requests: the guard only reads, and record_action's insert
    must commit immediately. Failure handling belongs to the callers — the
    middleware fails open, billing resolves to 'free' — so this raises freely."""
    global _OPS_CONN
    if _OPS_CONN is None or getattr(_OPS_CONN, "closed", False):
        conn = connect(db_url())
        conn.autocommit = True
        _OPS_CONN = conn
    return _OPS_CONN.cursor()


def _finding_assertion(conn, world_id: int, assertion_id: int | None) -> dict | None:
    if assertion_id is None:
        return None
    return _one(conn, _ASSERTION_SELECT + """
        where a.world_id = %(w)s and a.id = %(id)s
    """, {"w": world_id, "id": assertion_id})


def get_finding(world_id: int, finding_id: int) -> dict | None:
    with db_conn() as conn:
        f = _one(conn, """
            select f.id, f.check_name, f.severity, f.explanation, f.sealed,
                   f.scene_id, f.assertion_a, f.assertion_b, f.run_at
            from findings f where f.id = %(id)s and f.world_id = %(w)s
        """, {"id": finding_id, "w": world_id})
        if not f:
            return None
        if f["scene_id"] is not None:
            f["scene"] = _one(conn, """
                select s.id, s.slug, s.story_position, s.is_flashback, s.raw_text,
                  wk.title episode_title
                from scenes s join works wk on wk.id = s.work_id
                where s.id = %(s)s
            """, {"s": f["scene_id"]})
        else:
            f["scene"] = None
        f["a"] = _finding_assertion(conn, world_id, f["assertion_a"])
        f["b"] = _finding_assertion(conn, world_id, f["assertion_b"])
        # Current seal (independent of the finding's own `sealed` mirror).
        f["seal"] = _one(conn, """
            select reason, created_at from seals
            where world_id = %(w)s and check_name = %(c)s
              and assertion_a = %(a)s and coalesce_b = coalesce(%(b)s, 0)
        """, {"w": world_id, "c": f["check_name"],
              "a": f["assertion_a"], "b": f["assertion_b"]})
        f["sealable"] = f["assertion_a"] is not None
    return f


def seal_finding(world_id: int, finding_id: int, reason: str,
                 ruled_by: str | None = None) -> bool:
    """Insert a seal for the finding and mirror it onto findings.sealed.

    Writes the `seals` table on the exact key the checker reads
    (canon/check.py: (check_name, assertion_a, coalesce(assertion_b, 0))). The
    matching `findings.sealed` update means the UI reflects the seal without
    re-running `canon check`; a real re-check would compute the same flag.

    ruled_by (an auth.users uuid) records who made the ruling; ruled_at is
    stamped in SQL. Both columns arrive with the identity_and_access migration.
    """
    with db_conn() as conn:
        cur = conn.cursor()
        f = _one(conn,
            "select check_name, assertion_a, assertion_b from findings "
            "where id = %(id)s and world_id = %(w)s",
            {"id": finding_id, "w": world_id})
        if not f or f["assertion_a"] is None:
            return False
        key = {"w": world_id, "c": f["check_name"],
               "a": f["assertion_a"], "b": f["assertion_b"]}
        # Upsert without leaning on ON CONFLICT against the generated coalesce_b key.
        cur.execute("""
            delete from seals
            where world_id = %(w)s and check_name = %(c)s
              and assertion_a = %(a)s and coalesce_b = coalesce(%(b)s, 0)
        """, key)
        cur.execute("""
            insert into seals (world_id, check_name, assertion_a, assertion_b,
                               reason, ruled_by, ruled_at)
            values (%(w)s, %(c)s, %(a)s, %(b)s, %(reason)s, %(ruled_by)s::uuid, now())
        """, key | {"reason": (reason or "").strip() or None, "ruled_by": ruled_by})
        cur.execute(f"""
            update findings set sealed = true
            where world_id = %(w)s and check_name = %(c)s and assertion_a = %(a)s
              and coalesce(assertion_b, 0) = coalesce(%(b)s, 0)
        """, key)
        conn.commit()
    return True


# ---------------------------------------------------------------------------
# Note surface (P3-SURFACE): coverage notes, load-bearing canon, script text,
# and the ask pane. Reads everywhere; the only writes are the two permanent
# status transitions on coverage_notes (open -> sealed | dismissed).
# ---------------------------------------------------------------------------

NOTE_TERMINAL_STATUSES = ("sealed", "dismissed")


def list_coverage_notes(world_id: int) -> list[dict]:
    """Every coverage note for the world, newest-run data included.

    Rows carry the whole lifecycle (status, first/last_seen_run, resolved_at,
    status_changed_by/at) so the report, footer, and draft-2 diff all render
    from one query. `evidence` is jsonb and arrives as a dict.
    """
    with db_conn() as conn:
        return _all(conn, """
            select id::text as id, note_key, family, summary, body, evidence,
                   salience, status,
                   first_seen_run::text as first_seen_run,
                   last_seen_run::text  as last_seen_run,
                   resolved_at,
                   status_changed_by::text as status_changed_by,
                   status_changed_at,
                   created_at, updated_at
            from coverage_notes
            where world_id = %(w)s
            order by family, salience desc, note_key
        """, {"w": world_id})


def set_note_status(world_id: int, note_id: str, status: str,
                    changed_by: str | None = None) -> bool:
    """Seal or dismiss a coverage note — permanently.

    The WHERE clause enforces the product law from docs/readers-report.md: only
    an *open* note can transition, and nothing here (or anywhere in ui/) can
    re-open a sealed/dismissed note. Attribution goes to the identity columns
    added by the identity_and_access migration.
    """
    if status not in NOTE_TERMINAL_STATUSES:
        raise ValueError(f"status must be one of {NOTE_TERMINAL_STATUSES}, got {status!r}")
    with db_conn() as conn:
        cur = conn.cursor()
        cur.execute("""
            update coverage_notes
               set status = %(s)s,
                   status_changed_by = %(u)s::uuid,
                   status_changed_at = now(),
                   updated_at = now()
             where id = %(id)s::uuid and world_id = %(w)s and status = 'open'
             returning id
        """, {"s": status, "u": changed_by, "id": note_id, "w": world_id})
        changed = cur.fetchone() is not None
        conn.commit()
    return changed


def load_bearing(world_id: int, limit: int = 6) -> list[dict]:
    """Section 2 of the report: the most-connected entities, fully deterministic
    (assertion counts and distinct cited scenes; no opinion anywhere)."""
    with db_conn() as conn:
        return _all(conn, """
            select e.id, e.name, e.kind,
                   count(*) as n_assertions,
                   count(distinct t.scene_id) as n_scenes
            from (
              select subject_id as entity_id, established_in_scene as scene_id
                from assertions where world_id = %(w)s
              union all
              select object_id, established_in_scene
                from assertions where world_id = %(w)s and object_id is not null
            ) t
            join entities e on e.id = t.entity_id
            group by e.id, e.name, e.kind
            order by n_assertions desc, e.name
            limit %(limit)s
        """, {"w": world_id, "limit": limit})


def list_scenes_with_text(world_id: int) -> list[dict]:
    """Scene records with raw text, in story order — the script view's spine."""
    with db_conn() as conn:
        return _all(conn, """
            select s.id, s.slug, s.story_position, s.is_flashback, s.raw_text,
                   wk.id work_id, wk.title episode_title, wk.sort_order
            from scenes s join works wk on wk.id = s.work_id
            where wk.world_id = %(w)s
            order by s.story_position, s.id
        """, {"w": world_id})


def entity_names(world_id: int, entity_ids: list[int]) -> dict[int, str]:
    """id -> name for the given entities (used to scope a note's Ask seed)."""
    ids = sorted({int(i) for i in entity_ids or [] if i})
    if not ids:
        return {}
    with db_conn() as conn:
        rows = _all(conn, """
            select id, name from entities
            where world_id = %(w)s and id = any(%(ids)s)
        """, {"w": world_id, "ids": ids})
    return {r["id"]: r["name"] for r in rows}


def ask_question(world_id: int, question: str):
    """Run the ask-the-bible engine (ask/engine.py) against this world.

    The engine indexes result rows positionally, so it gets a plain tuple-row
    connection (not the dict_row one the templates use). Import stays local so
    `ui` never grows a hard module-level dependency on the ask package.
    """
    from ask import engine as ask_engine  # read-only import; never edited here

    conn = connect(db_url())
    try:
        return ask_engine.ask_question(conn, world_id, question)
    finally:
        conn.close()


def unseal_finding(world_id: int, finding_id: int) -> bool:
    with db_conn() as conn:
        cur = conn.cursor()
        f = _one(conn,
            "select check_name, assertion_a, assertion_b from findings "
            "where id = %(id)s and world_id = %(w)s",
            {"id": finding_id, "w": world_id})
        if not f or f["assertion_a"] is None:
            return False
        key = {"w": world_id, "c": f["check_name"],
               "a": f["assertion_a"], "b": f["assertion_b"]}
        cur.execute("""
            delete from seals
            where world_id = %(w)s and check_name = %(c)s
              and assertion_a = %(a)s and coalesce_b = coalesce(%(b)s, 0)
        """, key)
        cur.execute(f"""
            update findings set sealed = false
            where world_id = %(w)s and check_name = %(c)s and assertion_a = %(a)s
              and coalesce(assertion_b, 0) = coalesce(%(b)s, 0)
        """, key)
        conn.commit()
    return True

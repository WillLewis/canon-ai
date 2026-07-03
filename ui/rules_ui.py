"""Rule builder surface (P3-RULES) — /worlds/{world_id}/rules.

Structured, never free-form: the builder form is dropdowns/selects populated
from the closed predicate vocabulary (extract/schema.py), the world's own
entities and trait values, its scene story positions, and the check families.
The optional writer label is the only free-text field. Rules are constructed
through canon.rules.from_params, so anything that can't compile unambiguously
is a 400 at creation — never a guess, never a warning.

Surface pattern (ui/app.py): reads stay open — viewers and anonymous visitors
see the rules, action buttons render disabled; the three writes (create,
enable/disable, delete) gate on require_role('editor'); AUTH_DISABLED=1 keeps
the single-user dev flow working. No LLM calls anywhere in this module.

The orchestrator wires `router` into ui/app.py (suggested nav slot: between
Report and Script — see the workstream report).
"""

from __future__ import annotations

import pathlib
import sys
from urllib.parse import parse_qs

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, PlainTextResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

# Make `canon`/`extract` importable regardless of launch directory (same
# guard ui/db.py uses; harmless when already present).
_REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from billing import gating as billing_gating  # noqa: E402  (import only — P3-WIRING)
from billing import store as billing_store  # noqa: E402
from canon import rules as rules_engine  # noqa: E402  (no LLM in there — grep-guarded)
from canon import rules_store  # noqa: E402
from canon.ingest import connect  # noqa: E402  (read/write the same DB the pipeline loads)
from extract.schema import INTRANSITIVE, PREDICATES  # noqa: E402

from . import auth, db  # noqa: E402

router = APIRouter()

_HERE = pathlib.Path(__file__).resolve().parent
templates = Jinja2Templates(directory=str(_HERE / "templates"))


# ---------------------------------------------------------------------------
# Plan gate (P3-WIRING): rule authoring is a paid feature (docs/readers-
# report.md "Unit economics"). Only the three WRITE routes gate — reads stay
# open to every member. Free users get 402 with the /billing upgrade path
# (plan_gate builds that body); CANON_FORCE_TIER=paid is the dev override.
# ---------------------------------------------------------------------------

def _billing_cursor_factory():
    # Resolved through the db module at call time so tests can fake db.ops_cursor.
    return db.ops_cursor()


# Module attribute (not a closure) so tests can swap in a fake tier resolver.
rule_tier_resolver = billing_store.make_tier_resolver(_billing_cursor_factory)

_paid_gate = billing_gating.plan_gate(
    "rule_authoring", tier_resolver=lambda user: rule_tier_resolver(user))


# ---------------------------------------------------------------------------
# Data access — module-level functions so tests fake them exactly like ui.db's
# (tests/test_surface.py pattern). Plain tuple-row connections: rules_store is
# pure-cursor and zips its own columns.
# ---------------------------------------------------------------------------

def store_list(world_id: int) -> list[dict]:
    conn = connect(db.db_url())
    try:
        return rules_store.list_rules(conn.cursor(), world_id)
    finally:
        conn.close()


def store_create(world_id: int, rule, created_by=None) -> str:
    """Persist an already-constructed (therefore already-validated) rule."""
    conn = connect(db.db_url())
    try:
        rule_id = rules_engine.save_rule(conn.cursor(), world_id, rule,
                                         created_by=created_by)
        conn.commit()
        return rule_id
    finally:
        conn.close()


def store_set_enabled(world_id: int, rule_id: str, enabled: bool) -> bool:
    conn = connect(db.db_url())
    try:
        changed = rules_store.set_rule_enabled(conn.cursor(), world_id, rule_id, enabled)
        conn.commit()
        return changed
    finally:
        conn.close()


def store_delete(world_id: int, rule_id: str) -> bool:
    conn = connect(db.db_url())
    try:
        deleted = rules_store.delete_rule(conn.cursor(), world_id, rule_id)
        conn.commit()
        return deleted
    finally:
        conn.close()


def world_traits(world_id: int) -> list[str]:
    """Distinct trait values asserted in this world — the trait dropdown."""
    conn = connect(db.db_url())
    try:
        cur = conn.cursor()
        cur.execute(
            "select distinct object_value from assertions"
            " where world_id = %(world_id)s and predicate = 'trait'"
            "   and object_value is not null and object_value <> ''"
            " order by object_value",
            {"world_id": world_id})
        return [r[0] for r in cur.fetchall()]
    finally:
        conn.close()


def world_positions(world_id: int) -> list[int]:
    """The world's scene story positions — the range dropdowns (keeps the
    whole builder select-only; no numeric free entry)."""
    conn = connect(db.db_url())
    try:
        cur = conn.cursor()
        cur.execute(
            "select distinct s.story_position"
            " from scenes s join works wk on wk.id = s.work_id"
            " where wk.world_id = %(world_id)s"
            " order by s.story_position",
            {"world_id": world_id})
        return [r[0] for r in cur.fetchall()]
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# View plumbing (mirrors ui/app.py's surface helpers, which we may not edit).
# ---------------------------------------------------------------------------

def _surface_world(world_id: int):
    worlds = db.list_worlds()
    active = next((w for w in worlds if w["id"] == world_id), None)
    return active, worlds


def _surface_role(request: Request, world_id: int):
    user = auth.peek_user(request, world_id)
    role = user.role if user else None
    return role, auth.has_role(role, "editor")


async def _form(request: Request) -> dict:
    """Parse an x-www-form-urlencoded body without the python-multipart dep."""
    body = (await request.body()).decode("utf-8")
    return {k: v[0] for k, v in parse_qs(body, keep_blank_values=True).items()}


def _entity_ids_in(params: dict) -> list[int]:
    ids = []
    for key in ("subject_entity_id", "object_entity_id", "entity_id"):
        v = params.get(key)
        if v is not None:
            ids.append(int(v))
    return ids


def _describe(row: dict, names: dict) -> str:
    """Plain-English description of a stored rule, entity names resolved.
    Deterministic templating over the writer's own structure — nothing phrased."""
    p = row.get("params") or {}

    def ename(key):
        eid = p.get(key)
        return names.get(int(eid), f"entity #{eid}") if eid is not None else None

    if row["kind"] == "cannot":
        scope = (f"any entity of trait “{p['subject_trait']}”"
                 if p.get("subject_trait") else ename("subject_entity_id"))
        tail = f" {ename('object_entity_id')}" if p.get("object_entity_id") else ""
        return f"{scope} cannot {p.get('predicate')}{tail}"
    if row["kind"] == "only":
        tail = f" {ename('object_entity_id')}" if p.get("object_entity_id") else ""
        return f"only {ename('entity_id')} may {p.get('predicate')}{tail}"
    rng = ""
    if p.get("pos_from") is not None or p.get("pos_to") is not None:
        rng = (f" in positions {p.get('pos_from') if p.get('pos_from') is not None else '*'}"
               f"..{p.get('pos_to') if p.get('pos_to') is not None else '*'}")
    return f"skip {p.get('check_name')} for {ename('entity_id')}{rng}"


def _rule_from_form(form: dict):
    """Builder form -> typed rule. canon.rules.from_params does every
    validation; ValueError propagates to the route's 400."""
    kind = (form.get("kind") or "").strip()
    label = form.get("label")
    severity = (form.get("severity") or "").strip() or None
    if kind == "cannot":
        by_trait = form.get("subject_mode") == "trait"
        params = {
            "predicate": form.get("predicate"),
            "subject_entity_id": None if by_trait else form.get("subject_entity_id"),
            "subject_trait": form.get("subject_trait") if by_trait else None,
            "object_entity_id": form.get("object_entity_id"),
        }
    elif kind == "only":
        params = {
            "entity_id": form.get("only_entity_id"),
            "predicate": form.get("only_predicate"),
            "object_entity_id": form.get("only_object_entity_id"),
        }
    else:  # 'exception' or garbage — from_params rejects garbage
        severity = None  # exceptions suppress; they carry no severity
        params = {
            "check_name": form.get("exc_check"),
            "entity_id": form.get("exc_entity_id"),
            "pos_from": form.get("pos_from"),
            "pos_to": form.get("pos_to"),
        }
    return rules_engine.from_params(kind, params, label=label, severity=severity)


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@router.get("/worlds/{world_id}/rules", response_class=HTMLResponse)
def rules_page(request: Request, world_id: int):
    active, worlds = _surface_world(world_id)
    if not active:
        return templates.TemplateResponse(
            request, "no_world.html", {"world": None, "worlds": worlds, "nav": ""})
    rows = store_list(world_id)
    ids: list[int] = []
    for r in rows:
        ids.extend(_entity_ids_in(r.get("params") or {}))
    names = db.entity_names(world_id, ids)
    for r in rows:
        r["description"] = _describe(r, names)
    role, can_edit = _surface_role(request, world_id)
    return templates.TemplateResponse(request, "rules.html", {
        "world": active, "worlds": worlds, "nav": "rules",
        "rules": rows,
        "n_enabled": sum(1 for r in rows if r.get("enabled")),
        "predicates": PREDICATES,
        "intransitive": sorted(INTRANSITIVE),
        "entities": db.list_entities(world_id),
        "traits": world_traits(world_id),
        "families": rules_engine.check_families(),
        "severities": rules_engine.SEVERITIES,
        "default_severity": rules_engine.DEFAULT_SEVERITY,
        "positions": world_positions(world_id),
        "role": role, "can_edit": can_edit,
    })


@router.post("/worlds/{world_id}/rules")
async def create_rule(request: Request, world_id: int,
                      user: auth.User = Depends(auth.require_role("editor")),
                      _paid: auth.User = Depends(_paid_gate)):
    """Create one structured rule. Anything the closed vocabulary can't express
    unambiguously is a 400 right here — rejected at creation, never guessed."""
    form = await _form(request)
    try:
        rule = _rule_from_form(form)
    except ValueError as e:
        return PlainTextResponse(f"rule rejected: {e}", status_code=400)
    store_create(world_id, rule, created_by=user.id)
    return RedirectResponse(url=f"/worlds/{world_id}/rules", status_code=303)


@router.post("/worlds/{world_id}/rules/{rule_id}/toggle")
async def toggle_rule(request: Request, world_id: int, rule_id: str,
                      user: auth.User = Depends(auth.require_role("editor")),
                      _paid: auth.User = Depends(_paid_gate)):
    """Enable/disable without deleting — a disabled rule never runs."""
    form = await _form(request)
    store_set_enabled(world_id, rule_id, form.get("enabled") == "1")
    return RedirectResponse(url=f"/worlds/{world_id}/rules", status_code=303)


@router.post("/worlds/{world_id}/rules/{rule_id}/delete")
async def delete_rule(request: Request, world_id: int, rule_id: str,
                      user: auth.User = Depends(auth.require_role("editor")),
                      _paid: auth.User = Depends(_paid_gate)):
    store_delete(world_id, rule_id)
    return RedirectResponse(url=f"/worlds/{world_id}/rules", status_code=303)

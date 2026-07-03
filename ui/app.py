"""Canon AI triage workbench — a small FastAPI + server-rendered-Jinja inspector.

Read-only views over a loaded world (entities, assertions, scenes, findings) with
every claim shown next to its citation, plus the single permitted write:
seal/unseal a finding. No story is ever generated — the app only indexes, displays,
and (for seals) records the writer's intent.

Run it:
    python -m ui.app                # serves http://127.0.0.1:8000
    uvicorn ui.app:app --reload     # dev autoreload
Point it at a database with CANON_DB_URL / DATABASE_URL; otherwise it uses the
documented local default (postgresql://postgres:postgres@127.0.0.1:5432/postgres).
"""

from __future__ import annotations

import os
from pathlib import Path
from urllib.parse import parse_qs, urlencode

from fastapi import Depends, FastAPI, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from . import auth, db
from . import notes as note_vm
from .format import (gloss_range, highlight, object_side, plain_assertion,
                     quote_present, SEVERITY_RANK)

_HERE = Path(__file__).resolve().parent

app = FastAPI(title="Canon AI — Triage Workbench")
app.mount("/static", StaticFiles(directory=str(_HERE / "static")), name="static")
templates = Jinja2Templates(directory=str(_HERE / "templates"))

# Expose the pure display helpers to every template.
templates.env.globals["gloss_range"] = gloss_range
templates.env.globals["object_side"] = object_side
templates.env.globals["quote_present"] = quote_present
templates.env.globals["plain_assertion"] = plain_assertion
templates.env.globals["SEVERITY_RANK"] = SEVERITY_RANK
templates.env.filters["highlight"] = highlight
templates.env.globals["FAMILY_LABELS"] = note_vm.FAMILY_LABELS
templates.env.globals["FAMILIES"] = note_vm.FAMILIES

PER_PAGE = 100


# ---------------------------------------------------------------------------
# World plumbing — every page operates within one selected world.
# ---------------------------------------------------------------------------

def _pick_world(world_param: str | None, worlds: list[dict]) -> dict | None:
    by_name = {w["name"]: w for w in worlds}
    if world_param and world_param in by_name:
        return by_name[world_param]
    if "greyharbor_s1" in by_name:          # the loaded dev world (task brief)
        return by_name["greyharbor_s1"]
    return worlds[0] if worlds else None


def _merge_query(request: Request, **overrides) -> str:
    """Current query string with overrides applied (drop keys set to None)."""
    params = dict(request.query_params)
    for k, v in overrides.items():
        if v is None:
            params.pop(k, None)
        else:
            params[k] = v
    return ("?" + urlencode(params)) if params else ""


def _render(request: Request, name: str, world: dict | None, worlds: list[dict],
            nav: str, **ctx) -> HTMLResponse:
    ctx = {
        "world": world,
        "worlds": worlds,
        "nav": nav,
        "merge_query": lambda **o: _merge_query(request, **o),
        **ctx,
    }
    return templates.TemplateResponse(request, name, ctx)


def _no_world(request: Request, worlds: list[dict]) -> HTMLResponse:
    return _render(request, "no_world.html", None, worlds, nav="")


# ---------------------------------------------------------------------------
# Connection-failure handling — the most common first-run snag.
# ---------------------------------------------------------------------------

@app.exception_handler(Exception)
async def _on_error(request: Request, exc: Exception):
    # Surface DB connection problems helpfully; re-raise anything unexpected so it
    # still shows in the server log during development.
    name = type(exc).__name__
    if "OperationalError" in name or "connect" in str(exc).lower():
        html = templates.get_template("db_error.html").render(
            request=request, db_url=db.db_url(), error=str(exc).strip())
        return HTMLResponse(html, status_code=503)
    raise exc


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@app.get("/", response_class=HTMLResponse)
def overview(request: Request, world: str | None = None):
    worlds = db.list_worlds()
    active = _pick_world(world, worlds)
    if not active:
        return _no_world(request, worlds)
    summary = db.world_summary(active["id"])
    return _render(request, "overview.html", active, worlds, nav="overview",
                   summary=summary)


@app.get("/entities", response_class=HTMLResponse)
def entities(request: Request, world: str | None = None,
             kind: str | None = None, q: str | None = None):
    worlds = db.list_worlds()
    active = _pick_world(world, worlds)
    if not active:
        return _no_world(request, worlds)
    rows = db.list_entities(active["id"], kind=kind or None, q=q or None)
    return _render(request, "entities.html", active, worlds, nav="entities",
                   entities=rows, kinds=db.kinds_in_world(active["id"]),
                   sel_kind=kind or "", q=q or "")


@app.get("/entities/{entity_id}", response_class=HTMLResponse)
def entity_detail(request: Request, entity_id: int, world: str | None = None):
    worlds = db.list_worlds()
    active = _pick_world(world, worlds)
    if not active:
        return _no_world(request, worlds)
    entity = db.get_entity(active["id"], entity_id)
    if not entity:
        return _render(request, "not_found.html", active, worlds, nav="entities",
                       what=f"entity #{entity_id}")
    return _render(request, "entity.html", active, worlds, nav="entities",
                   entity=entity)


@app.get("/assertions", response_class=HTMLResponse)
def assertions(request: Request, world: str | None = None,
               subject: int | None = None, predicate: str | None = None,
               status: str | None = None, q: str | None = None, page: int = 1):
    worlds = db.list_worlds()
    active = _pick_world(world, worlds)
    if not active:
        return _no_world(request, worlds)
    page = max(1, page)
    rows, total = db.list_assertions(
        active["id"], subject_id=subject, predicate=predicate or None,
        status=status or None, q=q or None,
        limit=PER_PAGE, offset=(page - 1) * PER_PAGE)
    subject_entity = db.get_entity(active["id"], subject) if subject else None
    total_pages = max(1, (total + PER_PAGE - 1) // PER_PAGE)
    return _render(request, "assertions.html", active, worlds, nav="assertions",
                   assertions=rows, total=total, page=page, total_pages=total_pages,
                   predicates=db.predicates_in_world(active["id"]),
                   sel_predicate=predicate or "", sel_status=status or "", q=q or "",
                   subject_entity=subject_entity)


@app.get("/assertions/{assertion_id}", response_class=HTMLResponse)
def assertion_detail(request: Request, assertion_id: int, world: str | None = None):
    worlds = db.list_worlds()
    active = _pick_world(world, worlds)
    if not active:
        return _no_world(request, worlds)
    a = db.get_assertion(active["id"], assertion_id)
    if not a:
        return _render(request, "not_found.html", active, worlds, nav="assertions",
                       what=f"assertion #{assertion_id}")
    return _render(request, "assertion.html", active, worlds, nav="assertions",
                   a=a)


@app.get("/scenes", response_class=HTMLResponse)
def scenes(request: Request, world: str | None = None):
    worlds = db.list_worlds()
    active = _pick_world(world, worlds)
    if not active:
        return _no_world(request, worlds)
    rows = db.list_scenes(active["id"])
    return _render(request, "scenes.html", active, worlds, nav="scenes", scenes=rows)


@app.get("/scenes/{scene_id}", response_class=HTMLResponse)
def scene_detail(request: Request, scene_id: int, world: str | None = None,
                 q: int | None = None):
    """q = an assertion id to highlight its supporting quote in this scene."""
    worlds = db.list_worlds()
    active = _pick_world(world, worlds)
    if not active:
        return _no_world(request, worlds)
    scene = db.get_scene(active["id"], scene_id)
    if not scene:
        return _render(request, "not_found.html", active, worlds, nav="scenes",
                       what=f"scene #{scene_id}")
    highlight_quote = None
    if q:
        match = next((a for a in scene["assertions"] if a["id"] == q), None)
        if match is None:
            match = db.get_assertion(active["id"], q)
        highlight_quote = match["supporting_quote"] if match else None
    return _render(request, "scene.html", active, worlds, nav="scenes",
                   scene=scene, highlight_quote=highlight_quote, focus_assertion=q)


@app.get("/findings", response_class=HTMLResponse)
def findings(request: Request, world: str | None = None):
    worlds = db.list_worlds()
    active = _pick_world(world, worlds)
    if not active:
        return _no_world(request, worlds)
    rows = db.list_findings(active["id"])
    return _render(request, "findings.html", active, worlds, nav="findings",
                   findings=rows)


@app.get("/findings/{finding_id}", response_class=HTMLResponse)
def finding_detail(request: Request, finding_id: int, world: str | None = None):
    worlds = db.list_worlds()
    active = _pick_world(world, worlds)
    if not active:
        return _no_world(request, worlds)
    f = db.get_finding(active["id"], finding_id)
    if not f:
        return _render(request, "not_found.html", active, worlds, nav="findings",
                       what=f"finding #{finding_id}")
    return _render(request, "finding.html", active, worlds, nav="findings", f=f)


async def _form(request: Request) -> dict:
    """Parse an x-www-form-urlencoded body without the python-multipart dep."""
    body = (await request.body()).decode("utf-8")
    return {k: v[0] for k, v in parse_qs(body, keep_blank_values=True).items()}


@app.post("/findings/{finding_id}/seal")
async def seal(request: Request, finding_id: int, world: str | None = None,
               user: auth.User = Depends(auth.require_role("editor"))):
    worlds = db.list_worlds()
    active = _pick_world(world, worlds)
    if not active:
        return _no_world(request, worlds)
    form = await _form(request)
    db.seal_finding(active["id"], finding_id, form.get("reason", ""), ruled_by=user.id)
    return RedirectResponse(
        url=f"/findings/{finding_id}?world={active['name']}", status_code=303)


@app.post("/findings/{finding_id}/unseal")
async def unseal(request: Request, finding_id: int, world: str | None = None,
                 user: auth.User = Depends(auth.require_role("editor"))):
    worlds = db.list_worlds()
    active = _pick_world(world, worlds)
    if not active:
        return _no_world(request, worlds)
    db.unseal_finding(active["id"], finding_id)
    return RedirectResponse(
        url=f"/findings/{finding_id}?world={active['name']}", status_code=303)


# ---------------------------------------------------------------------------
# Note surface (P3-SURFACE) — the writer-facing Reader's Report views.
# World-scoped by path id: /worlds/{world_id}/... . Read views stay open
# (viewers see everything, action buttons disabled); the two note writes and
# nothing else gate on the editor role. No LLM calls anywhere below — the
# engine wrote the rows, these routes only render them.
# ---------------------------------------------------------------------------

def _surface_world(world_id: int) -> tuple[dict | None, list[dict]]:
    worlds = db.list_worlds()
    active = next((w for w in worlds if w["id"] == world_id), None)
    return active, worlds


def _surface_role(request: Request, world_id: int) -> tuple[str | None, bool]:
    """(role, can_edit) for read views — never raises; anonymous = read-only."""
    user = auth.peek_user(request, world_id)
    role = user.role if user else None
    return role, auth.has_role(role, "editor")


def _live_first(findings: list[dict]) -> tuple[list[dict], list[dict]]:
    live = [f for f in findings if not f.get("sealed")]
    sealed = [f for f in findings if f.get("sealed")]
    return live, sealed


def _report_context(request: Request, world_id: int,
                    ask_q: str = "", ask_result=None) -> dict | None:
    active, worlds = _surface_world(world_id)
    if not active:
        return None
    notes = db.list_coverage_notes(world_id)
    names = db.entity_names(world_id, note_vm.all_entity_ids(notes))
    note_vm.decorate_notes(world_id, notes, names)
    grouped = note_vm.split_notes(notes)
    live, sealed_findings = _live_first(db.list_findings(world_id))
    role, can_edit = _surface_role(request, world_id)
    summary = db.world_summary(world_id)
    # The "Verify your canon (N)" pill count. world_summary already carries the
    # by-status assertion counts, so the report render keeps its query budget
    # (db.count_drafts is the same number for callers without a summary in hand).
    draft_count = next((r["n"] for r in summary["assertions_by_status"]
                        if r["status"] == "draft"), 0)
    return {
        "world": active, "worlds": worlds, "nav": "report",
        "grouped": grouped,
        "findings_live": live[: note_vm.FINDINGS_CAP],
        "findings_more": max(0, len(live) - note_vm.FINDINGS_CAP),
        "findings_sealed_n": len(sealed_findings),
        "load_bearing": db.load_bearing(world_id, note_vm.LOAD_BEARING_CAP),
        "summary": summary,
        "draft_count": draft_count,
        "diff": note_vm.diff_summary(notes),
        "role": role, "can_edit": can_edit,
        "ask_q": ask_q, "ask_result": ask_result,
    }


@app.get("/worlds/{world_id}/report", response_class=HTMLResponse)
def report_view(request: Request, world_id: int, ask: str | None = None):
    ctx = _report_context(request, world_id, ask_q=ask or "")
    if ctx is None:
        return _no_world(request, db.list_worlds())
    return templates.TemplateResponse(request, "report.html", ctx)


@app.get("/worlds/{world_id}/report/diff", response_class=HTMLResponse)
def report_diff(request: Request, world_id: int):
    active, worlds = _surface_world(world_id)
    if not active:
        return _no_world(request, worlds)
    notes = db.list_coverage_notes(world_id)
    note_vm.decorate_notes(world_id, notes)
    return templates.TemplateResponse(request, "report_diff.html", {
        "world": active, "worlds": worlds, "nav": "report",
        "diff": note_vm.diff_summary(notes),
    })


def _script_context(request: Request, world_id: int, scene: int | None,
                    quote: str | None, ask_q: str = "", ask_result=None) -> dict | None:
    active, worlds = _surface_world(world_id)
    if not active:
        return None
    scenes = db.list_scenes_with_text(world_id)
    notes = db.list_coverage_notes(world_id)
    names = db.entity_names(world_id, note_vm.all_entity_ids(notes))
    note_vm.decorate_notes(world_id, notes, names)
    by_scene = note_vm.notes_by_scene(notes)
    findings_by_scene: dict[int, list[dict]] = {}
    live, _sealed = _live_first(db.list_findings(world_id))
    for f in live:
        if f.get("scene_id"):
            findings_by_scene.setdefault(f["scene_id"], []).append(f)
    role, can_edit = _surface_role(request, world_id)
    return {
        "world": active, "worlds": worlds, "nav": "script",
        "scenes": scenes,
        "notes_by_scene": by_scene,
        "findings_by_scene": findings_by_scene,
        "focus_scene": scene,
        "focus_quote": quote or None,
        "role": role, "can_edit": can_edit,
        "ask_q": ask_q, "ask_result": ask_result,
    }


@app.get("/worlds/{world_id}/script", response_class=HTMLResponse)
def script_view(request: Request, world_id: int, scene: int | None = None,
                quote: str | None = None, ask: str | None = None):
    ctx = _script_context(request, world_id, scene, quote, ask_q=ask or "")
    if ctx is None:
        return _no_world(request, db.list_worlds())
    return templates.TemplateResponse(request, "script.html", ctx)


async def _note_status_change(request: Request, world_id: int, note_id: str,
                              status: str, user: auth.User):
    active, worlds = _surface_world(world_id)
    if not active:
        return _no_world(request, worlds)
    db.set_note_status(world_id, note_id, status, changed_by=user.id)
    return RedirectResponse(url=f"/worlds/{world_id}/report", status_code=303)


@app.post("/worlds/{world_id}/notes/{note_id}/seal")
async def seal_note(request: Request, world_id: int, note_id: str,
                    user: auth.User = Depends(auth.require_role("editor"))):
    """Writer ruled the flagged thing intentional. Permanent: never re-raised."""
    return await _note_status_change(request, world_id, note_id, "sealed", user)


@app.post("/worlds/{world_id}/notes/{note_id}/dismiss")
async def dismiss_note(request: Request, world_id: int, note_id: str,
                       user: auth.User = Depends(auth.require_role("editor"))):
    """Writer ruled the note wrong. Permanent, one keystroke, no guilt-trip."""
    return await _note_status_change(request, world_id, note_id, "dismissed", user)


@app.post("/worlds/{world_id}/ask", response_class=HTMLResponse)
async def ask_pane(request: Request, world_id: int):
    """The ask-the-bible pane. Calls ask/engine.py (SQL templates, no LLM) and
    re-renders whichever view hosted the pane, answer + citations included.
    Refusals render verbatim — an uncited answer never ships."""
    form = await _form(request)
    question = (form.get("question") or "").strip()
    view = form.get("view") or "report"
    result = db.ask_question(world_id, question) if question else None
    if view == "script":
        scene = form.get("scene")
        ctx = _script_context(request, world_id,
                              int(scene) if (scene or "").isdigit() else None,
                              form.get("quote") or None,
                              ask_q=question, ask_result=result)
        template = "script.html"
    else:
        ctx = _report_context(request, world_id, ask_q=question, ask_result=result)
        template = "report.html"
    if ctx is None:
        return _no_world(request, db.list_worlds())
    return templates.TemplateResponse(request, template, ctx)


# ---------------------------------------------------------------------------
# Confirm queue (P3-CONFIRM) — "verify your canon". Extraction loads sub-gate
# assertions as status 'draft' (canon/store.py); this surface lists them and
# records the writer's ruling: Confirm -> canon, Reject -> rejected. Both are
# guarded WHERE status='draft' in ui/db.py, so a ruling never flips a settled
# row. Read view stays open (viewers see the queue, buttons disabled); the two
# writes gate on the editor role. Nothing is generated — every card shows an
# extracted fact beside its verbatim supporting quote, and the writer judges.
# ---------------------------------------------------------------------------

@app.get("/worlds/{world_id}/confirm", response_class=HTMLResponse)
def confirm_queue(request: Request, world_id: int):
    active, worlds = _surface_world(world_id)
    if not active:
        return _no_world(request, worlds)
    drafts = db.list_draft_assertions(world_id)
    role, can_edit = _surface_role(request, world_id)
    return templates.TemplateResponse(request, "confirm.html", {
        "world": active, "worlds": worlds, "nav": "confirm",
        "drafts": drafts,
        "verified_through": db.last_story_position(world_id),
        "role": role, "can_edit": can_edit,
    })


async def _rule_assertion(request: Request, world_id: int, assertion_id: int,
                          status: str, user: auth.User):
    active, worlds = _surface_world(world_id)
    if not active:
        return _no_world(request, worlds)
    db.rule_assertion(world_id, assertion_id, status, ruled_by=user.id)
    return RedirectResponse(url=f"/worlds/{world_id}/confirm", status_code=303)


@app.post("/worlds/{world_id}/assertions/{assertion_id}/confirm")
async def confirm_assertion(request: Request, world_id: int, assertion_id: int,
                            user: auth.User = Depends(auth.require_role("editor"))):
    """Writer ruled the extracted fact true: draft -> canon (idempotent)."""
    return await _rule_assertion(request, world_id, assertion_id, "canon", user)


@app.post("/worlds/{world_id}/assertions/{assertion_id}/reject")
async def reject_assertion(request: Request, world_id: int, assertion_id: int,
                           user: auth.User = Depends(auth.require_role("editor"))):
    """Writer ruled the extraction wrong: draft -> rejected (idempotent)."""
    return await _rule_assertion(request, world_id, assertion_id, "rejected", user)


def main() -> None:
    import uvicorn
    host = os.environ.get("CANON_UI_HOST", "127.0.0.1")
    port = int(os.environ.get("CANON_UI_PORT", "8000"))
    uvicorn.run(app, host=host, port=port)


if __name__ == "__main__":
    main()

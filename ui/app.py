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
from .format import gloss_range, highlight, object_side, quote_present, SEVERITY_RANK

_HERE = Path(__file__).resolve().parent

app = FastAPI(title="Canon AI — Triage Workbench")
app.mount("/static", StaticFiles(directory=str(_HERE / "static")), name="static")
templates = Jinja2Templates(directory=str(_HERE / "templates"))

# Expose the pure display helpers to every template.
templates.env.globals["gloss_range"] = gloss_range
templates.env.globals["object_side"] = object_side
templates.env.globals["quote_present"] = quote_present
templates.env.globals["SEVERITY_RANK"] = SEVERITY_RANK
templates.env.filters["highlight"] = highlight

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


def main() -> None:
    import uvicorn
    host = os.environ.get("CANON_UI_HOST", "127.0.0.1")
    port = int(os.environ.get("CANON_UI_PORT", "8000"))
    uvicorn.run(app, host=host, port=port)


if __name__ == "__main__":
    main()

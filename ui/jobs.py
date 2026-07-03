"""Upload -> background run -> ingest theater (P3-FRONTDOOR).

The first ninety seconds: a writer drops a script, the gate chain rules on it
BEFORE any LLM call, ingestion loads scene records (SQL only), and a daemon
thread runs canon/runner.py while the theater page polls the event ledger.

Routes:
    POST /api/uploads              raw body + X-Canon-Filename (the repo avoids
                                   python-multipart); 202 {run_id, theater_url}
    GET  /upload                   the drop-zone page
    GET  /worlds/{id}/ingest       the theater page (reload resumes watching)
    GET  /api/runs/{id}/events     short-poll JSON {run, events, next}
    POST /api/runs/{id}/resume     editor-only, failed runs only
    GET  /demo/report              the logged-out demo world (share_mode render)

Gate chain, strictly in order and strictly before any LLM call: auth (401) ->
tier -> rate limits script_ingest / world_create (429 + Retry-After) ->
concurrency (429) -> local parse -> page cap (400) -> COGS estimate vs
CANON_RUN_COGS_CAP_USD (400, honest message) -> ingest -> record_action (only
on success) -> run row -> spawn.

Rights hygiene: the raw upload NEVER persists on disk — non-Fountain formats
parse through a NamedTemporaryFile deleted in a finally; what Canon keeps is
scene records in Postgres, exportable and deletable like everything else.

Wiring seams (module attributes, monkeypatchable in tests, the repo pattern):
`ops_cursor_factory`, `tier_resolver`, `conn_factory`, `client_factory`,
`spawn`, `active_runs`.
"""

from __future__ import annotations

import contextlib
import io
import math
import os
import pathlib
import tempfile
import threading
from datetime import datetime, timezone
from types import SimpleNamespace

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.templating import Jinja2Templates

from billing import store as billing_store
from canon import ingest as canon_ingest
from canon import runner as pipeline_runner
from canon.fountain import parse_fountain
from ops import alerts as ops_alerts
from ops import metering as ops_metering
from ops import ratelimit as ops_ratelimit

from . import auth, db

router = APIRouter()

_HERE = pathlib.Path(__file__).resolve().parent
templates = Jinja2Templates(directory=str(_HERE / "templates"))

SUPPORTED_EXTS = (".fountain", ".fdx", ".pdf", ".docx")
LINES_PER_PAGE = 55
STALE_HEARTBEAT_SECONDS = 90
DEMO_WORLD_ENV = "CANON_DEMO_WORLD"
DEFAULT_DEMO_WORLD = "greyharbor_s1"
TRUST_CAPTION = ("Your words never train a model. "
                 "Export or delete everything, any time.")


# --- wiring seams ----------------------------------------------------------------

def ops_cursor_factory():
    """Cursor over usage_events for the rate-limit / tier reads (fail-open
    callers). Module seam; tests replace it with a fake-cursor factory."""
    return db.ops_cursor()


# Module attribute (not a closure) so tests can swap in a fake tier resolver —
# same pattern as ui.app.ask_tier_resolver.
tier_resolver = billing_store.make_tier_resolver(lambda: ops_cursor_factory())


def conn_factory():
    """Tuple-row connection to the canon database (loader + runner)."""
    from canon.ingest import connect  # lazy: importing the router needs no DB

    return connect(db.db_url())


def client_factory():
    """The metered Anthropic client (extract/llm.py seam; no-training org)."""
    from extract import llm

    return llm.make_client()


RUNS: dict[str, threading.Thread] = {}   # run_id -> live runner thread


def active_runs() -> int:
    for rid in [r for r, t in RUNS.items() if not t.is_alive()]:
        RUNS.pop(rid, None)
    return len(RUNS)


def spawn(run_id: str, target) -> None:
    """Start the runner as a daemon thread. Module seam: tests replace this
    with an inline call (or a recorder that never runs anything)."""
    thread = threading.Thread(target=target, name=f"canon-run-{run_id}", daemon=True)
    RUNS[run_id] = thread
    thread.start()


def max_concurrent_runs() -> int:
    try:
        return int(os.environ.get("CANON_MAX_CONCURRENT_RUNS", "3"))
    except ValueError:
        return 3


# --- pure helpers ------------------------------------------------------------------

def page_estimate(work) -> int:
    """ceil(lines/55) over the parsed scenes — the non-PDF page estimate."""
    lines = sum(len((sc.raw_text or "").splitlines()) + 1 for sc in work.scenes)
    return max(1, math.ceil(lines / LINES_PER_PAGE))


def world_name_for(filename: str) -> str:
    import re

    stem = pathlib.Path(filename).stem
    slug = re.sub(r"[^a-z0-9]+", "_", stem.lower()).strip("_") or "script"
    return slug[:60]


def rebuild_works(rows: list[dict]) -> list:
    """Duck-typed ParsedWork/Scene records from rows already in the database
    (db.works_with_scenes) — resume never needs the original file, which was
    never kept anyway (rights hygiene)."""
    works = []
    for w in rows:
        scenes = [SimpleNamespace(scene_index=i, slug=s["slug"],
                                  is_flashback=s["is_flashback"],
                                  raw_text=s["raw_text"])
                  for i, s in enumerate(w["scenes"], start=1)]
        works.append(SimpleNamespace(title=w["title"], source_file=w["source_file"],
                                     episode=None, sort_order=w["sort_order"],
                                     scenes=scenes))
    return works


def _stale(run: dict, now: datetime | None = None) -> bool:
    if run.get("status") != "running":
        return False
    beat = run.get("heartbeat_at")
    if beat is None:
        return False
    now = now or datetime.now(timezone.utc)
    if beat.tzinfo is None:
        beat = beat.replace(tzinfo=timezone.utc)
    return (now - beat).total_seconds() > STALE_HEARTBEAT_SECONDS


def _run_json(run: dict) -> dict:
    return {
        "id": run.get("id"), "world_id": run.get("world_id"),
        "status": run.get("status"), "phase": run.get("phase"),
        "scenes_total": run.get("scenes_total"), "scenes_done": run.get("scenes_done"),
        "facts_total": run.get("facts_total"),
        "cost_usd": float(run.get("cost_usd") or 0),
        "error": run.get("error"),
        "resumable": run.get("status") == "failed",
    }


# --- parsing (local, never persisted) -----------------------------------------------

def parse_upload(filename: str, body: bytes):
    """Parse one uploaded script by extension. The raw bytes never persist:
    formats whose parsers need a path go through a NamedTemporaryFile deleted
    in the finally, and the stored source_file is the writer's filename."""
    ext = pathlib.Path(filename).suffix.lower()
    if ext not in SUPPORTED_EXTS:
        raise HTTPException(status_code=400, detail=(
            f"unsupported script format '{ext or filename}' — "
            f"supported: {', '.join(SUPPORTED_EXTS)}"))
    try:
        if ext == ".fountain":
            work = parse_fountain(body.decode("utf-8", errors="replace"),
                                  source_file=filename)
        else:
            tmp = tempfile.NamedTemporaryFile(suffix=ext, delete=False)
            try:
                tmp.write(body)
                tmp.close()
                if ext == ".fdx":
                    from ingest.fdx import parse_fdx

                    work = parse_fdx(tmp.name)
                elif ext == ".pdf":
                    from canon.script_doc import parse_pdf

                    work = parse_pdf(tmp.name)
                else:
                    from canon.script_doc import parse_docx

                    work = parse_docx(tmp.name)
            finally:
                with contextlib.suppress(OSError):
                    os.unlink(tmp.name)
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=400,
                            detail=f"could not read {filename}: {exc}")
    work.source_file = filename   # never leak a tmp path into the database
    return work


def upload_page_count(ext: str, body: bytes, work) -> int:
    if ext == ".pdf":
        try:
            from pypdf import PdfReader

            return max(1, len(PdfReader(io.BytesIO(body)).pages))
        except Exception:
            return page_estimate(work)
    return page_estimate(work)


# --- the background run ----------------------------------------------------------------

def execute_run(run_id: str, world_name: str, works, *, model: str | None = None,
                cost_cap_usd: float | None = None,
                resume_from: list[dict] | None = None) -> dict:
    """Thread body: run the pipeline, mirroring every event onto the ledger
    and the run row (heartbeat on every event). Never raises."""

    def emit(kind: str, data: dict) -> None:
        try:
            db.append_run_event(run_id, kind, data)
            fields: dict = {}
            if kind == "phase" and data.get("phase"):
                fields["phase"] = data["phase"]
                if data.get("scenes_total"):
                    fields["scenes_total"] = data["scenes_total"]
            elif kind == "scene":
                fields["scenes_done"] = data.get("index")
                fields["facts_total"] = data.get("facts_total")
            db.update_run(run_id, **fields)   # empty dict = pure heartbeat
        except Exception as exc:              # narration never kills the run
            ops_alerts.alert("run_event_write_failed",
                             {"run_id": run_id, "kind": kind, "error": repr(exc)})

    result = pipeline_runner.run_pipeline(
        conn_factory, world_name, works,
        client_factory=client_factory, emit=emit, model=model,
        cost_cap_usd=cost_cap_usd, resume_from=resume_from)

    fields = {"status": result["status"],
              "scenes_done": result.get("scenes_done", 0),
              "facts_total": result.get("facts_total", 0),
              "cost_usd": result.get("cost_usd", 0),
              "error": result.get("error")}
    if result["status"] == "done":
        fields["phase"] = "done"
        fields["candidates"] = None           # resume state no longer needed
    else:
        fields["candidates"] = result.get("candidates")
    try:
        db.update_run(run_id, **fields)
    except Exception as exc:
        ops_alerts.alert("run_row_write_failed",
                         {"run_id": run_id, "error": repr(exc)})
    return result


# --- routes -------------------------------------------------------------------------

def _denied(decision) -> JSONResponse:
    headers = {}
    if decision.retry_after:
        headers["Retry-After"] = str(decision.retry_after)
    return JSONResponse({"detail": decision.reason}, status_code=429, headers=headers)


@router.post("/api/uploads")
async def upload_script(request: Request,
                        user: auth.User = Depends(auth.get_current_user)):
    """The whole gate chain, then 202. See module docstring for the order."""
    filename = (request.headers.get("x-canon-filename") or "").strip()
    if not filename:
        raise HTTPException(status_code=400,
                            detail="missing X-Canon-Filename header")
    ext = pathlib.Path(filename).suffix.lower()
    if ext not in SUPPORTED_EXTS:
        raise HTTPException(status_code=400, detail=(
            f"unsupported script format '{ext or filename}' — "
            f"supported: {', '.join(SUPPORTED_EXTS)}"))
    body = await request.body()
    if not body:
        raise HTTPException(status_code=400, detail="empty upload")

    # Tier + rate limits (fail-open store; denial = 429 with Retry-After).
    tier = tier_resolver(user)
    try:
        policy = ops_ratelimit.get_policy(tier)
    except ValueError:
        policy = ops_ratelimit.get_policy("free")
    try:
        cur = ops_cursor_factory()
    except Exception:
        cur = None   # check_limit fails open and alerts

    decision = ops_ratelimit.check_limit(cur, user.id, "script_ingest", policy)
    if not decision:
        return _denied(decision)

    # Reuse an existing world (?world=<name>, draft-2 flow) or create a new one.
    world_param = (request.query_params.get("world") or "").strip()
    existing = db.get_world(world_param) if world_param else None
    if world_param and not existing:
        raise HTTPException(status_code=404, detail=f"world '{world_param}' not found")
    creating = existing is None
    if creating:
        decision = ops_ratelimit.check_limit(cur, user.id, "world_create", policy)
        if not decision:
            return _denied(decision)
    else:
        role = ("owner" if auth.auth_disabled()
                else db.member_role(existing["id"], user.id))
        if not auth.has_role(role, "editor"):
            raise HTTPException(status_code=403,
                                detail="requires the editor role on this world")

    # Concurrency gate — before any heavy work.
    if active_runs() >= max_concurrent_runs():
        return JSONResponse(
            {"detail": "Canon is at capacity — try again in a few minutes."},
            status_code=429, headers={"Retry-After": "120"})

    # Local parse (never persisted raw) + page cap + COGS estimate.
    work = parse_upload(filename, body)
    if not work.scenes:
        raise HTTPException(status_code=400, detail=(
            f"no scenes found in {filename} — Canon needs scene headings "
            "(INT./EXT.) to read a script"))
    pages = upload_page_count(ext, body, work)
    page_decision = ops_ratelimit.check_script_pages(pages, policy)
    if not page_decision:
        raise HTTPException(status_code=400, detail=page_decision.reason)

    model = pipeline_runner.web_model()
    cap = pipeline_runner.cost_cap_usd_default()
    estimate = float(ops_metering.estimate_script_cost(pages, model))
    if estimate > cap:
        raise HTTPException(status_code=400, detail=(
            f"this script (~{pages} pages) would cost about ${estimate:.2f} "
            f"to read, over the ${cap:.2f} per-run ceiling — split it into "
            "episodes and upload them separately"))

    # Ingest scene records (SQL only; no LLM yet).
    world_name = existing["name"] if existing else _unique_world_name(filename)
    works = canon_ingest.order_works([work])
    conn = conn_factory()
    try:
        info = canon_ingest.load_into(conn, world_name, works, reset=not creating)
        if creating:
            wcur = conn.cursor()
            wcur.execute("UPDATE worlds SET owner_id = %s WHERE id = %s",
                         (user.id, info["world_id"]))
            conn.commit()
    finally:
        with contextlib.suppress(Exception):
            conn.close()

    # Success: NOW the actions count (denied/failed attempts are free).
    try:
        rcur = ops_cursor_factory()
        ops_ratelimit.record_action(rcur, user_id=user.id, action="script_ingest",
                                    world_id=info["world_id"])
        if creating:
            ops_ratelimit.record_action(rcur, user_id=user.id, action="world_create",
                                        world_id=info["world_id"])
    except Exception as exc:   # fail open, alert — same stance as ui.app._record_ask
        ops_alerts.alert("ratelimit_db_error", {
            "action": "script_ingest", "stage": "record_action", "error": repr(exc)})

    run_id = db.create_run(info["world_id"], user.id, scenes_total=info["scenes"])
    spawn(run_id, lambda: execute_run(run_id, world_name, works,
                                      model=model, cost_cap_usd=cap))
    return JSONResponse(
        {"run_id": run_id, "theater_url": f"/worlds/{info['world_id']}/ingest"},
        status_code=202)


def _unique_world_name(filename: str) -> str:
    base = world_name_for(filename)
    if not db.get_world(base):
        return base
    import uuid

    return f"{base}_{uuid.uuid4().hex[:6]}"


@router.get("/upload", response_class=HTMLResponse)
def upload_page(request: Request):
    user = auth.peek_user(request)
    free = ops_ratelimit.get_policy("free")
    return templates.TemplateResponse(request, "upload.html", {
        "world": None, "worlds": [], "nav": "",
        "user": user,
        "trust_caption": TRUST_CAPTION,
        "max_pages": free.max_script_pages,
        "free_limits": {
            "scripts": free.limits["script_ingest"].max_events,
            "worlds": free.limits["world_create"].max_events,
            "reports_per_day": free.limits["report_run"].max_events,
            "asks_per_hour": free.limits["ask"].max_events,
        },
        "supported_exts": SUPPORTED_EXTS,
    })


@router.get("/worlds/{world_id}/ingest", response_class=HTMLResponse)
def theater_page(request: Request, world_id: int):
    worlds = db.list_worlds()
    active = next((w for w in worlds if w["id"] == world_id), None)
    if not active:
        raise HTTPException(status_code=404, detail="world not found")
    user = auth.peek_user(request, world_id)
    run = db.latest_run_for_world(world_id)   # reload resumes watching
    return templates.TemplateResponse(request, "theater.html", {
        "world": active, "worlds": worlds, "nav": "",
        "run": run,
        "run_json": _run_json(run) if run else None,
        "role": user.role if user else None,
        "can_edit": auth.has_role(user.role if user else None, "editor"),
    })


@router.get("/api/runs/{run_id}/events")
def run_events(request: Request, run_id: str, after: int = 0):
    """Short-poll: {run, events, next}. Member-read; unknown and unauthorized
    are one identical 404 (no leakage). A running row whose heartbeat is older
    than 90s is surfaced — and persisted — as failed/resumable."""
    run = db.get_run(run_id)
    if not run:
        raise HTTPException(status_code=404, detail="not found")
    user = auth.peek_user(request, run["world_id"])
    if user is None or not auth.has_role(user.role, "viewer"):
        raise HTTPException(status_code=404, detail="not found")

    if _stale(run):
        error = "the runner stopped reporting (heartbeat lost) — resume to continue"
        with contextlib.suppress(Exception):
            db.update_run(run_id, status="failed", error=error)
        run = dict(run, status="failed", error=error)

    events = db.list_run_events(run_id, after=after)
    return {
        "run": _run_json(run),
        "events": [{"seq": e["seq"], "kind": e["kind"], "data": e["data"]}
                   for e in events],
        "next": events[-1]["seq"] if events else after,
    }


@router.post("/api/runs/{run_id}/resume")
def resume_run(request: Request, run_id: str,
               user: auth.User = Depends(auth.get_current_user)):
    """Re-spawn a failed run from its persisted candidates (scenes_done).
    Editor-gated; failed runs only; cost aborts stay aborted."""
    run = db.get_run(run_id)
    if not run:
        raise HTTPException(status_code=404, detail="not found")
    role = ("owner" if auth.auth_disabled()
            else db.member_role(run["world_id"], user.id))
    if not auth.has_role(role, "editor"):
        raise HTTPException(status_code=403,
                            detail="requires the editor role on this world")
    if run["status"] != "failed":
        raise HTTPException(status_code=409, detail=(
            f"only failed runs can resume (this one is {run['status']})"))
    if active_runs() >= max_concurrent_runs():
        return JSONResponse(
            {"detail": "Canon is at capacity — try again in a few minutes."},
            status_code=429, headers={"Retry-After": "120"})

    world = db.get_world_by_id(run["world_id"])
    if not world:
        raise HTTPException(status_code=404, detail="not found")
    works = rebuild_works(db.works_with_scenes(run["world_id"]))
    if not works:
        raise HTTPException(status_code=409, detail="world has no scenes to resume")
    candidates = run.get("candidates") or []

    db.update_run(run_id, status="running", error=None)
    spawn(run_id, lambda: execute_run(run_id, world["name"], works,
                                      resume_from=candidates))
    return JSONResponse(
        {"run_id": run_id, "theater_url": f"/worlds/{run['world_id']}/ingest"},
        status_code=202)


# --- the logged-out demo world ---------------------------------------------------------

@router.get("/demo/report", response_class=HTMLResponse)
def demo_report(request: Request):
    """A full Reader's Report for the demo world, logged out, actions disabled
    — the launch-gate item. Reuses the share_mode read-only render path
    (ui/share_ui.py pattern); 404 when the demo world is not loaded."""
    name = os.environ.get(DEMO_WORLD_ENV) or DEFAULT_DEMO_WORLD
    world = db.get_world(name)
    if not world:
        raise HTTPException(status_code=404, detail="demo world not loaded")
    from . import app as app_mod   # lazy: ui.app includes this router

    ctx = app_mod._report_context(request, world["id"])
    if ctx is None:
        raise HTTPException(status_code=404, detail="demo world not loaded")
    ctx.update(share_mode=True, role=None, can_edit=False,
               ask_q="", ask_result=None, nav="")
    return app_mod.templates.TemplateResponse(request, "report.html", ctx)

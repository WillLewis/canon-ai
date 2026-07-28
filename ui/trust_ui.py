"""Trust routes (P3-TRUST): export-everything, delete-everything, terms.

Exposes `router = APIRouter()`; the orchestrator wires it into ui/app.py
(`app.include_router(trust_ui.router)`) — this module never touches app.py.

Routes:
    GET  /worlds/{id}/export.zip   any member (viewer+) — it's their material
    GET  /worlds/{id}/delete       owner — confirm page (export offered FIRST)
    POST /worlds/{id}/delete       owner — typed world-name confirmation;
                                   mismatch -> 400 and nothing is deleted
    GET  /account/export.zip       signed-in user — full account zip
    GET  /account/delete           signed-in user — confirm page (export first)
    POST /account/delete           typed account-email confirmation
    GET  /legal/terms              public — DRAFT terms / no-training warranty

Every export/delete is audited via ops.metering.record_usage (zero tokens).
No LLM imports anywhere on this path; nothing here generates story.
"""

from __future__ import annotations

import re
from pathlib import Path
from urllib.parse import parse_qs

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, Response
from fastapi.templating import Jinja2Templates

from canon.export.full_export import export_account, export_world
from canon.trust_delete import delete_account, delete_world
from ops import metering  # imported, never edited (docs/ops.md)

from . import auth, db

router = APIRouter()
templates = Jinja2Templates(directory=str(Path(__file__).resolve().parent / "templates"))

# Audit rows are zero-token; pass a priced model id so the kind stays clean
# (record_usage suffixes ':unpriced' for unknown models — same trick as ui/db.py;
# usage_events stores no model column, so nothing false is recorded).
_AUDIT_MODEL = next(iter(metering.PRICING))


def _open_conn():
    """A plain tuple-row connection (module-level seam so tests can fake it).

    Not ui.db.db_conn(): that one installs dict_row, and the bible/report
    loaders that export_world reuses index rows positionally."""
    from canon.ingest import connect  # read-only import, as in ui/db.py

    return connect(db.db_url())


async def _form(request: Request) -> dict:
    """x-www-form-urlencoded body without the python-multipart dep (ui/app.py pattern)."""
    body = (await request.body()).decode("utf-8")
    return {k: v[0] for k, v in parse_qs(body, keep_blank_values=True).items()}


def _audit(cur, user_id, world_id, kind: str) -> None:
    metering.record_usage(cur, user_id=user_id, world_id=world_id, kind=kind,
                          tokens_in=0, tokens_out=0, model=_AUDIT_MODEL)


def _zip_response(data: bytes, filename: str) -> Response:
    safe = re.sub(r"[^A-Za-z0-9._-]+", "_", filename)
    return Response(content=data, media_type="application/zip",
                    headers={"Content-Disposition": f'attachment; filename="{safe}"'})


def _flatten_counts(counts: dict) -> list[tuple[str, int]]:
    """(label, n) rows for the deleted-summary template, nested dicts flattened."""
    rows: list[tuple[str, int]] = []
    for key, value in counts.items():
        if isinstance(value, dict):
            rows.extend((f"{key} · {k}", n) for k, n in _flatten_counts(value))
        else:
            rows.append((str(key), int(value)))
    return rows


def _page(request: Request, name: str, ctx: dict, status_code: int = 200) -> HTMLResponse:
    base = {"world": None, "worlds": [], "nav": ""}
    return templates.TemplateResponse(request, name, base | ctx, status_code=status_code)


# ---------------------------------------------------------------------------
# World export & delete
# ---------------------------------------------------------------------------

@router.get("/worlds/{world_id}/export.zip")
def world_export(world_id: int,
                 user: auth.User = Depends(auth.require_role("viewer"))):
    """Any member may take the whole world out — it's their material."""
    world = db.get_world_by_id(world_id)
    if not world:
        return HTMLResponse("world not found", status_code=404)
    conn = _open_conn()
    try:
        cur = conn.cursor()
        data = export_world(cur, world_id)
        _audit(cur, user.id, world_id, "world_export")
        conn.commit()
    finally:
        conn.close()
    return _zip_response(data, f"{world['name']}-export.zip")


@router.get("/worlds/{world_id}/delete", response_class=HTMLResponse)
def world_delete_confirm(request: Request, world_id: int,
                         user: auth.User = Depends(auth.require_role("owner"))):
    world = db.get_world_by_id(world_id)
    if not world:
        return HTMLResponse("world not found", status_code=404)
    return _page(request, "trust_delete_world.html",
                 {"world": world, "worlds": db.list_worlds(), "error": None})


@router.post("/worlds/{world_id}/delete", response_class=HTMLResponse)
async def world_delete(request: Request, world_id: int,
                       user: auth.User = Depends(auth.require_role("owner"))):
    """OWNER only. The world's exact name must be typed back; a mismatch is a
    400 and executes zero delete SQL."""
    world = db.get_world_by_id(world_id)
    if not world:
        return HTMLResponse("world not found", status_code=404)
    form = await _form(request)
    typed = (form.get("confirm") or "").strip()
    if typed != world["name"]:
        return _page(request, "trust_delete_world.html",
                     {"world": world, "worlds": db.list_worlds(),
                      "error": ("The name you typed does not match this world's "
                                "exact name. Nothing was deleted.")},
                     status_code=400)
    conn = _open_conn()
    try:
        cur = conn.cursor()
        counts = delete_world(cur, world_id)
        # world_id=None: the worlds row is gone, and usage_events.world_id is a
        # real FK. The event stays user-attributed.
        _audit(cur, user.id, None, "world_delete")
        conn.commit()
    finally:
        conn.close()
    return _page(request, "trust_deleted.html",
                 {"kind": "world", "name": world["name"],
                  "rows": _flatten_counts(counts),
                  "total": sum(n for _, n in _flatten_counts(counts))})


# ---------------------------------------------------------------------------
# Account export & delete
# ---------------------------------------------------------------------------

@router.get("/account/export.zip")
def account_export(user: auth.User = Depends(auth.get_current_user)):
    conn = _open_conn()
    try:
        cur = conn.cursor()
        data = export_account(cur, user.id)
        _audit(cur, user.id, None, "account_export")
        conn.commit()
    finally:
        conn.close()
    return _zip_response(data, "canon-account-export.zip")


@router.get("/account/delete", response_class=HTMLResponse)
def account_delete_confirm(request: Request,
                           user: auth.User = Depends(auth.get_current_user)):
    return _page(request, "trust_delete_account.html",
                 {"email": user.email, "error": None})


@router.post("/account/delete", response_class=HTMLResponse)
async def account_delete(request: Request,
                         user: auth.User = Depends(auth.get_current_user)):
    """Typed confirmation is the account email; mismatch -> 400, nothing deleted."""
    form = await _form(request)
    typed = (form.get("confirm") or "").strip()
    if not user.email or typed != user.email:
        return _page(request, "trust_delete_account.html",
                     {"email": user.email,
                      "error": ("The email you typed does not match this "
                                "account's email. Nothing was deleted.")},
                     status_code=400)
    conn = _open_conn()
    try:
        cur = conn.cursor()
        counts = delete_account(cur, user.id)
        # One zero-token audit row survives (uuid + kind only — operational
        # metadata, none of the writer's content).
        _audit(cur, user.id, None, "account_delete")
        conn.commit()
    finally:
        conn.close()
    return _page(request, "trust_deleted.html",
                 {"kind": "account", "name": user.email,
                  "rows": _flatten_counts(counts),
                  "total": sum(n for _, n in _flatten_counts(counts))})


# ---------------------------------------------------------------------------
# Terms — public
# ---------------------------------------------------------------------------

@router.get("/legal/terms", response_class=HTMLResponse)
def terms(request: Request):
    return _page(request, "terms.html", {})

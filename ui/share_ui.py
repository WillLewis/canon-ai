"""Share-link routes (P3-WIRING) — HTTP over canon/export/share.py.

    POST /worlds/{id}/share         owner only; kind report|bible -> mints a
                                    token via canon.export.share and renders
                                    the share URL (nothing is shareable until
                                    the writer explicitly creates a link).
    POST /worlds/{id}/share/revoke  owner only; permanent (a revoked token
                                    never resolves again — mint a new one).
    GET  /share/{token}             PUBLIC, no auth: a live token renders the
                                    shared artifact READ-ONLY — the report via
                                    the report rendering path with every
                                    action disabled and no nav, the bible as
                                    canon/export's standalone HTML. Unknown or
                                    revoked tokens are one identical 404 (no
                                    information leakage).

This is the viral loop: the shared page's footer carries the product line and
a link to /billing. No LLM calls anywhere in this module; the shared page only
re-renders rows the engine already wrote, citations included.

Wiring seams (module attributes, monkeypatchable in tests, same pattern as
billing/routes.py): `cursor_ctx` for the share_links table, `bible_html` for
the export renderer.
"""

from __future__ import annotations

import contextlib
import pathlib
from urllib.parse import parse_qs

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse, PlainTextResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from canon.export import share as share_store

from . import auth, db

router = APIRouter()

_HERE = pathlib.Path(__file__).resolve().parent
templates = Jinja2Templates(directory=str(_HERE / "templates"))

# The one product line every shared page carries (canon/export/render.py says
# the same thing on printed artifacts).
PRODUCT_LINE = "Canon never writes your story — it keeps it true."
BILLING_URL = "/billing"


# --- wiring seams --------------------------------------------------------------

@contextlib.contextmanager
def _default_cursor_ctx():
    # Tuple-row connection: share.py indexes result rows positionally.
    from canon.ingest import connect  # lazy: no live DB needed to import the router

    conn = connect(db.db_url())
    try:
        yield conn.cursor()
        conn.commit()
    finally:
        conn.close()


def cursor_ctx():
    """Context manager yielding a cursor over share_links; commit on success.
    Tests replace this module attribute with a fake-cursor nullcontext."""
    return _default_cursor_ctx()


def bible_html(world_id: int) -> str:
    """Standalone canon/export HTML for a shared bible (module seam; tests
    fake it). Deterministic SQL + rendering — zero LLM calls."""
    from canon.export.bible import build_bible
    from canon.export.render import render_bible_html
    from canon.ingest import connect

    holes_path = _HERE.parent / "db" / "holes.sql"
    holes_sql = holes_path.read_text(encoding="utf-8") if holes_path.is_file() else None
    conn = connect(db.db_url())
    try:
        bible, _hole_errors = build_bible(conn, world_id, holes_sql=holes_sql)
    finally:
        conn.close()
    return render_bible_html(bible)


# --- helpers ---------------------------------------------------------------------

async def _form(request: Request) -> dict:
    """Parse an x-www-form-urlencoded body without the python-multipart dep."""
    body = (await request.body()).decode("utf-8")
    return {k: v[0] for k, v in parse_qs(body, keep_blank_values=True).items()}


def _share_footer() -> str:
    return (f'<footer class="share-foot trust">{PRODUCT_LINE} '
            f'Shared read-only via Canon — <a href="{BILLING_URL}">run your own '
            f'script through Canon</a>.</footer>')


def _with_share_footer(html_text: str) -> str:
    """Inject the viral-loop footer into a standalone export document."""
    marker = "</body>"
    if marker in html_text:
        return html_text.replace(marker, _share_footer() + marker, 1)
    return html_text + _share_footer()


def _not_found() -> HTTPException:
    # One identical answer for unknown, malformed, and revoked tokens.
    return HTTPException(status_code=404, detail="not found")


# --- routes ---------------------------------------------------------------------

@router.post("/worlds/{world_id}/share", response_class=HTMLResponse)
async def create_share(request: Request, world_id: int,
                       user: auth.User = Depends(auth.require_role("owner"))):
    """Mint a share link for this world's report or bible. Owner only — a
    share link is a standing grant, so it takes the strongest role to create."""
    form = await _form(request)
    kind = (form.get("kind") or "report").strip()
    try:
        with cursor_ctx() as cur:
            link = share_store.create_share_link(cur, world_id, kind,
                                                 created_by=user.id)
    except ValueError as e:
        return PlainTextResponse(f"share link rejected: {e}", status_code=400)
    share_url = str(request.base_url).rstrip("/") + f"/share/{link['token']}"
    worlds = db.list_worlds()
    active = next((w for w in worlds if w["id"] == world_id), None)
    return templates.TemplateResponse(request, "share_link.html", {
        "world": active, "worlds": worlds, "nav": "",
        "share_url": share_url, "token": link["token"], "kind": kind,
    })


@router.post("/worlds/{world_id}/share/revoke")
async def revoke_share(request: Request, world_id: int,
                       user: auth.User = Depends(auth.require_role("owner"))):
    """Revoke a live token — permanently. Idempotent, and scoped: an owner can
    only revoke links that point at the world they own."""
    form = await _form(request)
    token = (form.get("token") or "").strip()
    with cursor_ctx() as cur:
        live = share_store.resolve_share_link(cur, token)
        if live and live["world_id"] == world_id:
            share_store.revoke_share_link(cur, token)
    return RedirectResponse(url=f"/worlds/{world_id}/report", status_code=303)


@router.get("/share/{token}", response_class=HTMLResponse)
def shared_artifact(request: Request, token: str):
    """PUBLIC read-only view of a shared artifact. No auth: the token IS the
    capability. Unknown/revoked tokens 404 identically."""
    with cursor_ctx() as cur:
        link = share_store.resolve_share_link(cur, token)
    if link is None:
        raise _not_found()
    if link["kind"] == "bible":
        return HTMLResponse(_with_share_footer(bible_html(link["world_id"])))
    return _shared_report(request, link["world_id"])


def _shared_report(request: Request, world_id: int) -> HTMLResponse:
    # Reuse the report rendering path (ui.app._report_context + report.html);
    # share_mode strips the nav and the ask pane, and can_edit=False renders
    # every action disabled regardless of who is looking.
    from . import app as app_mod  # lazy: ui.app includes this router

    ctx = app_mod._report_context(request, world_id)
    if ctx is None:
        raise _not_found()   # world vanished after the link was minted
    ctx.update(share_mode=True, role=None, can_edit=False,
               ask_q="", ask_result=None, nav="")
    return app_mod.templates.TemplateResponse(request, "report.html", ctx)

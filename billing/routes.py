"""Billing routes (APIRouter — the orchestrator mounts this on ui/app.py).

    GET  /billing           pricing page: free vs paid columns, the viewer's
                            current tier, Upgrade / Manage billing actions.
                            Open logged-out (it IS the upgrade path).
    POST /billing/checkout  auth required -> 303 to Stripe-hosted checkout
    GET  /billing/portal    auth required -> 303 to the Stripe customer portal
    POST /billing/webhook   UNauthenticated by design (Stripe calls it), but
                            signature-verified: 400 on a bad/stale signature,
                            503 when STRIPE_WEBHOOK_SECRET is unset (an
                            unverifiable webhook endpoint must not exist).

Wiring seams (module attributes, monkeypatchable in tests, matching the
repo's fake-the-module-function pattern from tests/test_auth.py):

    cursor_ctx   () -> context manager yielding a DB cursor; commits on
                 clean exit. Defaults to a short-lived ui.db connection.
    peek_user    best-effort identity for the pricing page (never 401s);
                 defaults to ui.auth.peek_user.

This module renders ui/templates/billing.html through its own Jinja2Templates
instance — it does not touch ui/app.py or base.html (the orchestrator wires
the router and the nav link).
"""

from __future__ import annotations

import contextlib
import json
import os
import pathlib

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from ui import auth

from . import store, stripe_client, webhooks

router = APIRouter()

_TEMPLATE_DIR = pathlib.Path(__file__).resolve().parent.parent / "ui" / "templates"
templates = Jinja2Templates(directory=str(_TEMPLATE_DIR))

# The tier definitions, verbatim from docs/readers-report.md "Unit economics".
# Data lives here (not in the template) so the pricing page and any future
# marketing surface render from one source.
FREE_FEATURES = [
    "Account (email or Google sign-in)",
    "1 world",
    "1 script",
    "Full Reader's Report — no crippling",
    "Share link",
]
PAID_FEATURES = [
    "Multi-script worlds",
    "Continuity scans against the back-catalog",
    "Unlimited draft diffs",
    "Bible export",
    "Retcon ripple",
    "Per-world rule authoring",
]
PAID_PRICE_LABEL = "$20"


# --- wiring seams ------------------------------------------------------------

@contextlib.contextmanager
def _default_cursor_ctx():
    from ui import db  # lazy: no live DB needed to import the router
    with db.db_conn() as conn:
        yield conn.cursor()
        conn.commit()   # skipped when the body raises; db_conn closes either way


def cursor_ctx():
    """Context manager yielding a cursor; commit on success. Tests replace
    this module attribute with a fake-cursor nullcontext."""
    return _default_cursor_ctx()


def peek_user(request: Request):
    """Best-effort identity for read views — never raises (ui.auth contract)."""
    return auth.peek_user(request)


def _tier_for(user) -> str:
    """The viewer's tier for display; a broken store shows 'free' (the page
    must render — resolve_tier already alerts on DB errors)."""
    try:
        with cursor_ctx() as cur:
            return store.resolve_tier(cur, user.id)
    except Exception:
        return "free"


# --- routes --------------------------------------------------------------------

@router.get("/billing", response_class=HTMLResponse)
def billing_page(request: Request):
    user = peek_user(request)
    tier = _tier_for(user) if user is not None else None
    return templates.TemplateResponse(request, "billing.html", {
        "user": user,
        "tier": tier,
        "free_features": FREE_FEATURES,
        "paid_features": PAID_FEATURES,
        "paid_price": PAID_PRICE_LABEL,
        "stripe_ready": stripe_client.stripe_available()
                        and bool(os.environ.get(stripe_client.SECRET_KEY_ENV)),
        "checkout_state": request.query_params.get("checkout"),
        "world": None,   # base.html renders nav-less without a world
    })


@router.post("/billing/checkout")
def start_checkout(user: auth.User = Depends(auth.get_current_user)):
    try:
        url = stripe_client.create_checkout_session(user.id, user.email)
    except RuntimeError as e:
        # Missing SDK or env — configuration, not a client error.
        raise HTTPException(status_code=503, detail=str(e))
    return RedirectResponse(url, status_code=303)


@router.get("/billing/portal")
def customer_portal(user: auth.User = Depends(auth.get_current_user)):
    with cursor_ctx() as cur:
        customer_id = store.get_customer_id(cur, user.id)
    if not customer_id:
        raise HTTPException(status_code=404,
                            detail="no billing customer on file — upgrade first")
    try:
        url = stripe_client.create_portal_session(customer_id)
    except RuntimeError as e:
        raise HTTPException(status_code=503, detail=str(e))
    return RedirectResponse(url, status_code=303)


@router.post("/billing/webhook")
async def stripe_webhook(request: Request):
    secret = os.environ.get(stripe_client.WEBHOOK_SECRET_ENV)
    if not secret:
        # Fail closed: without a secret nothing is verifiable, and an
        # unauthenticated endpoint that skips verification is an open write.
        raise HTTPException(status_code=503,
                            detail="billing webhooks are not configured "
                                   f"({stripe_client.WEBHOOK_SECRET_ENV} unset)")
    payload = await request.body()
    header = request.headers.get(webhooks.SIGNATURE_HEADER)
    if not webhooks.verify_signature(payload, header, secret):
        raise HTTPException(status_code=400, detail="invalid signature")
    try:
        event = json.loads(payload)
    except ValueError:
        raise HTTPException(status_code=400, detail="invalid JSON payload")
    with cursor_ctx() as cur:
        action = webhooks.handle_event(cur, event)
    # Always 2xx once verified — Stripe retries anything else, and unhandled
    # event types are acknowledged-and-ignored, not errors.
    return {"received": True, "action": action}

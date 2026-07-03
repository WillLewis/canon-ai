"""Stripe SDK wrapper — the only module that talks to Stripe's API.

The `stripe` package is an OPTIONAL extra (`pip install 'canon-ai[billing]'`),
feature-detected behind a guarded import exactly like weasyprint in
canon/export/render.py: importing this module never requires it, and the test
suite passes without it (webhook verification in billing/webhooks.py is
implemented against Stripe's documented scheme directly, no SDK).

All configuration is env-only — no keys in code, config files, or the repo:

    STRIPE_SECRET_KEY       API key (sk_...); set only in the deploy env
    STRIPE_PRICE_ID         the ~$20/mo subscription price (price_...)
    STRIPE_WEBHOOK_SECRET   used by billing/webhooks.py, listed here for the
                            one-stop config inventory
    CANON_BASE_URL          where Stripe sends the user back to
                            (default http://localhost:8000)

Both functions return URLs to redirect the user to; Stripe hosts the pages.
Cards, invoices, and dunning live entirely on Stripe's side.
"""

from __future__ import annotations

import os

SECRET_KEY_ENV = "STRIPE_SECRET_KEY"
PRICE_ID_ENV = "STRIPE_PRICE_ID"
WEBHOOK_SECRET_ENV = "STRIPE_WEBHOOK_SECRET"
BASE_URL_ENV = "CANON_BASE_URL"

INSTALL_HINT = (
    "the stripe SDK is not installed (it is optional). "
    "Run `pip install 'canon-ai[billing]'` or `pip install stripe`."
)


def stripe_available() -> bool:
    """Feature detection, same shape as canon.export.render.pdf_available."""
    try:
        import stripe  # noqa: F401
    except Exception:
        return False
    return True


def base_url() -> str:
    return (os.environ.get(BASE_URL_ENV) or "http://localhost:8000").rstrip("/")


def _require_env(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        raise RuntimeError(f"billing is not configured ({name} unset)")
    return value


def _stripe():
    """The configured stripe module, or a RuntimeError that says exactly what
    is missing (package or env) instead of failing mysteriously."""
    try:
        import stripe
    except Exception as e:  # ImportError or the SDK's own init errors
        raise RuntimeError(INSTALL_HINT) from e
    stripe.api_key = _require_env(SECRET_KEY_ENV)
    return stripe


def create_checkout_session(user_id, email: str | None) -> str:
    """Start a subscription checkout for the paid tier; returns the URL to
    redirect the user to.

    user_id rides along as client_reference_id AND as metadata on both the
    session and the subscription, so every webhook event that comes back can
    be attributed to the account without a customer-id lookup.
    """
    stripe = _stripe()
    price_id = _require_env(PRICE_ID_ENV)
    base = base_url()
    session = stripe.checkout.Session.create(
        mode="subscription",
        line_items=[{"price": price_id, "quantity": 1}],
        client_reference_id=str(user_id),
        customer_email=email or None,
        metadata={"user_id": str(user_id)},
        subscription_data={"metadata": {"user_id": str(user_id)}},
        success_url=f"{base}/billing?checkout=success",
        cancel_url=f"{base}/billing?checkout=canceled",
    )
    return session.url


def create_portal_session(customer_id: str) -> str:
    """Stripe-hosted customer portal (update card, cancel, invoices) for an
    existing customer; returns the URL to redirect the user to."""
    stripe = _stripe()
    session = stripe.billing_portal.Session.create(
        customer=customer_id,
        return_url=f"{base_url()}/billing",
    )
    return session.url

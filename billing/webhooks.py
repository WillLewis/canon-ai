"""Stripe webhooks: signature verification + event -> state transitions.

Verification is implemented against Stripe's documented scheme DIRECTLY —
no SDK — so the whole path is testable offline (the stripe package is an
optional extra and the suite must pass without it):

    Stripe-Signature: t=<unix ts>,v1=<hex sig>[,v1=...][,v0=...]
    signed_payload   = "{t}.{raw request body}"
    expected v1      = HMAC-SHA256(webhook secret, signed_payload)

Accept iff any v1 matches (constant-time compare) AND |now - t| is within
tolerance (Stripe's documented default: 5 minutes). The v0 scheme is ignored.

Handled events (everything else is acknowledged and ignored — Stripe retries
non-2xx, so "ignored" must still 200):

    checkout.session.completed      record customer + subscription -> paid.
                                    Carries no current_period_end; the row is
                                    written with NULL, which resolve_tier
                                    treats as paid until the follow-up
                                    subscription.updated fills the real date.
    customer.subscription.updated   status/period changes; tier follows the
                                    status (active/trialing = paid)
    customer.subscription.deleted   -> free

Attribution: user_id comes from the subscription/session metadata (planted by
stripe_client.create_checkout_session) or, failing that, a stripe_customer_id
lookup in billing_customers. An event we cannot attribute writes nothing and
alerts — a wrong-account tier flip is worse than a delayed one.

All state writes go through billing/store.py. The caller (billing/routes.py)
owns the cursor and the commit.
"""

from __future__ import annotations

import hashlib
import hmac
import time
from datetime import datetime, timezone

from ops import alerts

from . import store

SIGNATURE_HEADER = "stripe-signature"
DEFAULT_TOLERANCE_SECONDS = 300  # Stripe's documented recommendation


# --- signature scheme -----------------------------------------------------------

def _signed_payload(payload: bytes, timestamp: int) -> bytes:
    return str(int(timestamp)).encode() + b"." + payload


def compute_signature(payload: bytes, secret: str,
                      timestamp: int | None = None) -> str:
    """A valid Stripe-Signature header value for `payload` — used by tests
    and the local `stripe listen`-less dev loop."""
    timestamp = int(timestamp if timestamp is not None else time.time())
    digest = hmac.new(secret.encode(), _signed_payload(payload, timestamp),
                      hashlib.sha256).hexdigest()
    return f"t={timestamp},v1={digest}"


def parse_signature_header(header: str) -> tuple[int | None, list[str]]:
    """(timestamp, [v1 signatures]) from a Stripe-Signature header.
    Malformed pieces are skipped; a missing/invalid t comes back as None."""
    timestamp: int | None = None
    v1s: list[str] = []
    for item in header.split(","):
        key, _, value = item.strip().partition("=")
        if key == "t":
            try:
                timestamp = int(value)
            except ValueError:
                timestamp = None
        elif key == "v1" and value:
            v1s.append(value)
    return timestamp, v1s


def verify_signature(payload: bytes, header: str | None, secret: str, *,
                     tolerance: int = DEFAULT_TOLERANCE_SECONDS,
                     now: float | None = None) -> bool:
    """True iff `header` proves `payload` was signed with `secret` recently.

    Rejects: missing header/secret, unparseable header, timestamp outside
    tolerance (either direction — a future timestamp is as wrong as a stale
    one), and any signature mismatch. Comparison is constant-time.
    """
    if not header or not secret:
        return False
    timestamp, candidates = parse_signature_header(header)
    if timestamp is None or not candidates:
        return False
    now = time.time() if now is None else now
    if abs(now - timestamp) > tolerance:
        return False
    expected = hmac.new(secret.encode(), _signed_payload(payload, timestamp),
                        hashlib.sha256).hexdigest()
    return any(hmac.compare_digest(expected, candidate) for candidate in candidates)


# --- event handling ---------------------------------------------------------------

def _tier_for_status(status: str | None) -> str:
    return "paid" if status in store.PAID_STATUSES else "free"


def _period_end(obj: dict) -> datetime | None:
    """current_period_end as tz-aware datetime. Top-level on classic API
    versions; newer versions move it onto the subscription items."""
    ts = obj.get("current_period_end")
    if ts is None:
        items = (obj.get("items") or {}).get("data") or []
        if items:
            ts = items[0].get("current_period_end")
    if ts is None:
        return None
    return datetime.fromtimestamp(int(ts), tz=timezone.utc)


def _user_id_for(cursor, obj: dict) -> str | None:
    """metadata.user_id (planted at checkout) or a customer-id lookup."""
    meta = obj.get("metadata") or {}
    if meta.get("user_id"):
        return str(meta["user_id"])
    customer = obj.get("customer")
    if customer:
        return store.user_id_for_customer(cursor, customer)
    return None


def _unattributed(event_type: str, obj: dict) -> str:
    alerts.alert("billing_webhook_unattributed", {
        "event_type": event_type,
        "customer": obj.get("customer"),
        "subscription": obj.get("id") or obj.get("subscription"),
    })
    return f"unattributed: {event_type}"


def handle_event(cursor, event: dict) -> str:
    """Apply one verified Stripe event to the billing tables; returns a short
    plain-English description of what happened (for logs and tests). Never
    writes on events it cannot attribute to a user."""
    event_type = event.get("type") or "?"
    obj = (event.get("data") or {}).get("object") or {}

    if event_type == "checkout.session.completed":
        user_id = obj.get("client_reference_id") or _user_id_for(cursor, obj)
        if not user_id:
            return _unattributed(event_type, obj)
        customer = obj.get("customer")
        if customer:
            store.upsert_customer(cursor, user_id, customer)
        subscription = obj.get("subscription")
        if subscription:
            # No period end on this event; NULL = paid-in-grace until
            # customer.subscription.updated lands the real date.
            store.upsert_subscription(cursor, user_id, subscription,
                                      status="active", tier="paid",
                                      current_period_end=None)
        return f"checkout completed: user {user_id} -> paid"

    if event_type == "customer.subscription.updated":
        user_id = _user_id_for(cursor, obj)
        if not user_id:
            return _unattributed(event_type, obj)
        status = obj.get("status") or "active"
        tier = _tier_for_status(status)
        store.upsert_subscription(cursor, user_id, obj.get("id"),
                                  status=status, tier=tier,
                                  current_period_end=_period_end(obj))
        return f"subscription updated: user {user_id} -> {tier} ({status})"

    if event_type == "customer.subscription.deleted":
        user_id = _user_id_for(cursor, obj)
        if not user_id:
            return _unattributed(event_type, obj)
        store.upsert_subscription(cursor, user_id, obj.get("id"),
                                  status=obj.get("status") or "canceled",
                                  tier="free",
                                  current_period_end=_period_end(obj))
        return f"subscription deleted: user {user_id} -> free"

    return f"ignored: {event_type}"

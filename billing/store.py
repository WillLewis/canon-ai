"""Billing state — pure cursor functions, plus resolve_tier.

Two tables back this module. THEY DO NOT EXIST YET: their DDL is queued in
MIGRATIONS-NEEDED.md (P3-BILLING section) for Wave 5's schema sync, because
the migration lane is serialized and this wave ships no migration files.

    billing_customers(user_id uuid pk, stripe_customer_id text unique,
                      created_at timestamptz)
    billing_subscriptions(user_id uuid pk, stripe_subscription_id text unique,
                          status text, tier text check in ('free','paid'),
                          current_period_end timestamptz,
                          updated_at timestamptz)

One row per user in each table (pk = user_id): an account has at most one
Stripe customer and one live subscription; upserts keep the latest state.

resolve_tier is the product of this workstream — the function shaped to plug
into ops.middleware's tier_resolver hook (and gating.plan_gate). Failure
stance is the opposite of the rate limiter's: ops fails OPEN (availability
beats enforcement — a down store must not take the product down), but tier
resolution fails to 'free' — a broken billing store must never hand out paid
features; a paying customer briefly seeing an upgrade nudge is recoverable,
un-metered giveaways are not. Both paths alert (`billing_db_error`).

Dev override: CANON_FORCE_TIER=free|paid short-circuits resolution entirely
(unknown values are ignored). This is how paid features get exercised locally
before the tables exist.
"""

from __future__ import annotations

import os
from datetime import datetime, timezone
from typing import Callable

from ops import alerts

FORCE_TIER_ENV = "CANON_FORCE_TIER"
TIERS = ("free", "paid")

# Stripe subscription statuses that count as paying. past_due, unpaid,
# canceled, incomplete, incomplete_expired, paused all resolve to free.
PAID_STATUSES = ("active", "trialing")


# --- SQL (positional %s params, matching ops/ conventions) --------------------

_UPSERT_CUSTOMER = (
    "INSERT INTO billing_customers (user_id, stripe_customer_id) VALUES (%s, %s) "
    "ON CONFLICT (user_id) DO UPDATE SET stripe_customer_id = EXCLUDED.stripe_customer_id"
)
_SELECT_CUSTOMER_ID = (
    "SELECT stripe_customer_id FROM billing_customers WHERE user_id = %s"
)
_SELECT_USER_FOR_CUSTOMER = (
    "SELECT user_id FROM billing_customers WHERE stripe_customer_id = %s"
)
_UPSERT_SUBSCRIPTION = (
    "INSERT INTO billing_subscriptions "
    "(user_id, stripe_subscription_id, status, tier, current_period_end, updated_at) "
    "VALUES (%s, %s, %s, %s, %s, now()) "
    "ON CONFLICT (user_id) DO UPDATE SET "
    "stripe_subscription_id = EXCLUDED.stripe_subscription_id, "
    "status = EXCLUDED.status, tier = EXCLUDED.tier, "
    "current_period_end = EXCLUDED.current_period_end, updated_at = now()"
)
_SELECT_SUBSCRIPTION = (
    "SELECT stripe_subscription_id, status, tier, current_period_end "
    "FROM billing_subscriptions WHERE user_id = %s"
)


# --- writes (called by billing/webhooks.py under the service role) ------------

def upsert_customer(cursor, user_id, stripe_customer_id: str) -> None:
    cursor.execute(_UPSERT_CUSTOMER, (str(user_id), stripe_customer_id))


def upsert_subscription(cursor, user_id, stripe_subscription_id: str, *,
                        status: str, tier: str,
                        current_period_end: datetime | None) -> None:
    if tier not in TIERS:
        raise ValueError(f"unknown tier {tier!r} (expected one of {TIERS})")
    cursor.execute(_UPSERT_SUBSCRIPTION, (
        str(user_id), stripe_subscription_id, status, tier, current_period_end,
    ))


# --- reads ---------------------------------------------------------------------

def get_customer_id(cursor, user_id) -> str | None:
    cursor.execute(_SELECT_CUSTOMER_ID, (str(user_id),))
    row = cursor.fetchone()
    return row[0] if row else None


def user_id_for_customer(cursor, stripe_customer_id: str) -> str | None:
    cursor.execute(_SELECT_USER_FOR_CUSTOMER, (stripe_customer_id,))
    row = cursor.fetchone()
    return row[0] if row else None


def get_subscription(cursor, user_id) -> dict | None:
    cursor.execute(_SELECT_SUBSCRIPTION, (str(user_id),))
    row = cursor.fetchone()
    if not row:
        return None
    return {"stripe_subscription_id": row[0], "status": row[1],
            "tier": row[2], "current_period_end": row[3]}


# --- tier resolution -------------------------------------------------------------

def forced_tier() -> str | None:
    """CANON_FORCE_TIER when set to a valid tier, else None."""
    value = os.environ.get(FORCE_TIER_ENV)
    return value if value in TIERS else None


def resolve_tier(cursor, user_id, now: datetime | None = None) -> str:
    """'paid' iff the user has an active/trialing subscription whose
    current_period_end is in the future, else 'free'.

    A NULL current_period_end on an active subscription also counts as paid:
    checkout.session.completed carries no period end, and the upgrade must
    take effect the moment the writer lands back from checkout, not when the
    subscription.updated event catches up seconds later.

    Any DB error resolves to 'free' with a billing_db_error alert (see module
    docstring for why billing fails closed while ops fails open).
    """
    force = forced_tier()
    if force is not None:
        return force
    now = now or datetime.now(timezone.utc)
    try:
        sub = get_subscription(cursor, user_id)
    except Exception as exc:
        alerts.alert("billing_db_error", {
            "user_id": str(user_id), "error": repr(exc), "stage": "resolve_tier",
        })
        return "free"
    if sub is None or sub["status"] not in PAID_STATUSES:
        return "free"
    period_end = sub["current_period_end"]
    if period_end is not None and period_end <= now:
        return "free"
    return "paid"


def make_tier_resolver(cursor_factory: Callable) -> Callable:
    """A (user) -> tier callable shaped for ops.middleware.rate_limited's
    tier_resolver parameter:

        guard = rate_limited("ask", cursor_factory=db.cursor,
                             tier_resolver=make_tier_resolver(db.cursor))

    Accepts anything with a .id (ui.auth.User, SimpleNamespace) or a bare id,
    mirroring how ops.middleware reads its user. A broken cursor_factory
    resolves to 'free' with an alert — same fail-closed stance as resolve_tier.
    """

    def tier_resolver(user) -> str:
        force = forced_tier()
        if force is not None:
            return force
        user_id = getattr(user, "id", None) or str(user)
        try:
            cursor = cursor_factory()
        except Exception as exc:
            alerts.alert("billing_db_error", {
                "user_id": str(user_id), "error": repr(exc),
                "stage": "tier_resolver cursor_factory",
            })
            return "free"
        return resolve_tier(cursor, user_id)

    return tier_resolver

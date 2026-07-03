"""Stripe billing + plan gating (P3-BILLING).

The free/paid split from docs/readers-report.md ("Unit economics"):

    FREE  — account, 1 world, 1 script, full Reader's Report, share link.
    PAID  — ~$20/mo: multi-script worlds, continuity scans, unlimited draft
            diffs, bible export, retcon ripple, per-world rule authoring.

Module map:

    stripe_client   the Stripe SDK behind a guarded import (optional extra,
                    like weasyprint in canon/export) — checkout + portal URLs
    webhooks        signature verification against Stripe's documented scheme
                    (no SDK needed) + event -> state transitions
    store           pure cursor functions over billing_customers /
                    billing_subscriptions, and resolve_tier — the function
                    that plugs into ops.middleware's tier_resolver hook
    gating          plan_gate(feature) dependency: 402 for free users on
                    paid features
    routes          APIRouter: GET /billing (pricing page), POST
                    /billing/checkout, GET /billing/portal, POST
                    /billing/webhook

The two tables do not exist yet — their DDL is queued in MIGRATIONS-NEEDED.md
for Wave 5's schema sync (the migration lane is serialized; this wave ships
no migration files). Until it lands, resolve_tier fails soft to 'free' and
CANON_FORCE_TIER=paid is the dev override.

Money never touches this codebase: Stripe hosts checkout and the customer
portal; we store customer/subscription ids and a tier, nothing card-shaped.
All config comes from env (STRIPE_SECRET_KEY, STRIPE_PRICE_ID,
STRIPE_WEBHOOK_SECRET) — no keys in the repo, ever.
"""

from .store import make_tier_resolver, resolve_tier  # noqa: F401

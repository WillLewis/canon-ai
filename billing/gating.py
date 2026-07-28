"""plan_gate(feature): FastAPI dependency gating paid features.

Feature-level gates only. World/script COUNT caps stay in ops.ratelimit —
this module never duplicates them; billing's contribution to those is
store.resolve_tier feeding ops.middleware's tier_resolver so the right
Policy applies.

Paid features (docs/readers-report.md, "Unit economics"): bible export,
retcon ripple, per-world rule authoring. Continuity scans / multi-script /
unlimited diffs are enforced as counts in ops.ratelimit, not here.

A free user hitting a paid feature gets 402 Payment Required with a JSON
body that carries the upgrade path:

    {"error": "payment_required", "feature": "bible_export",
     "tier": "free", "upgrade_url": "/billing", "message": ...}

Wiring mirrors ops.middleware.rate_limited: user_resolver defaults to
ui.auth.get_current_user (lazy import; its 401s propagate), and the tier
comes from tier_resolver, else cursor_factory + store.resolve_tier. A gate
wired with neither resolves to 'free' and alerts — a wiring bug must never
give paid features away silently. CANON_FORCE_TIER wins over everything
(dev override, see billing/store.py).
"""

from __future__ import annotations

from typing import Callable

from fastapi import HTTPException, Request

from ops import alerts

from . import store

# Features behind the paywall. Everything not listed passes for every tier.
PAID_FEATURES = frozenset({"bible_export", "retcon_ripple", "rule_authoring"})

UPGRADE_URL = "/billing"


def _default_user_resolver() -> Callable:
    # Lazy, mirroring ops.middleware: keeps billing importable in contexts
    # that fake the user, and lets tests avoid the JWT machinery.
    from ui.auth import get_current_user
    return get_current_user


def _resolve_tier(feature: str, user,
                  cursor_factory: Callable | None,
                  tier_resolver: Callable | None) -> str:
    force = store.forced_tier()
    if force is not None:
        return force
    if tier_resolver is not None:
        return tier_resolver(user)
    if cursor_factory is not None:
        user_id = getattr(user, "id", None) or str(user)
        try:
            cursor = cursor_factory()
        except Exception as exc:
            alerts.alert("billing_db_error", {
                "feature": feature, "user_id": str(user_id),
                "error": repr(exc), "stage": "plan_gate cursor_factory",
            })
            return "free"  # fail closed: never give paid features away
        return store.resolve_tier(cursor, user_id)
    # No way to resolve a tier is a wiring bug; fail closed and say so.
    alerts.alert("billing_gate_unwired", {
        "feature": feature,
        "detail": "plan_gate has neither tier_resolver nor cursor_factory",
    })
    return "free"


def plan_gate(feature: str, *,
              cursor_factory: Callable | None = None,
              user_resolver: Callable | None = None,
              tier_resolver: Callable | None = None) -> Callable:
    """Dependency factory: authenticate, then require the paid tier for
    `feature` (when it is a PAID_FEATURES member; otherwise pass-through).

    cursor_factory  () -> DB cursor over billing_subscriptions
    user_resolver   (Request) -> user with .id; defaults to
                    ui.auth.get_current_user (raises 401 itself)
    tier_resolver   (user) -> 'free' | 'paid'; wins over cursor_factory —
                    pass store.make_tier_resolver(...) to share one resolver
                    with ops.middleware.rate_limited

    Returns the resolved user (routes can stack it as their user dependency,
    same contract as rate_limited). Raises 402 with the upgrade path for
    free users on paid features.
    """

    def dependency(request: Request):
        resolve_user = user_resolver or _default_user_resolver()
        user = resolve_user(request)                    # 401s propagate
        if feature not in PAID_FEATURES:
            return user
        tier = _resolve_tier(feature, user, cursor_factory, tier_resolver)
        if tier != "paid":
            raise HTTPException(status_code=402, detail={
                "error": "payment_required",
                "feature": feature,
                "tier": tier,
                "upgrade_url": UPGRADE_URL,
                "message": (f"{feature.replace('_', ' ')} is a paid feature "
                            f"(~$20/mo). Upgrade at {UPGRADE_URL}."),
            })
        return user

    return dependency

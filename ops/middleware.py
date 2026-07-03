"""FastAPI integration (P3-OPS): rate_limited(action) dependency factory.

Export-only — this module does NOT touch ui/app.py; the surface stream
(P3-SURFACE) wires it onto routes:

    from ops.middleware import rate_limited

    ask_guard = rate_limited("ask", cursor_factory=db.cursor)

    @app.get("/ask")
    def ask(user=Depends(ask_guard)):
        ...
        ratelimit.record_action(cur, user_id=user.id, action="ask")  # on success

The user is resolved via ui.auth.get_current_user (Supabase JWT, P3-IDENTITY);
its 401s propagate untouched. That import is lazy and injectable so ops/
imports cleanly before the identity branch merges and so tests can fake it —
ui.auth is the only ui/ import this package is allowed (docs/workstreams.md
boundary for P3-OPS).

Denials return 429 with a Retry-After header (seconds) when the limit has a
window to wait out. DB trouble fails OPEN with a `ratelimit_db_error` alert —
same availability-beats-enforcement stance as ops.ratelimit.
"""

from __future__ import annotations

from typing import Callable

from fastapi import HTTPException, Request

from . import alerts
from .ratelimit import Policy, check_limit, get_policy


def _default_user_resolver() -> Callable:
    # Lazy: ui.auth lands with P3-IDENTITY; resolving it at request time keeps
    # ops importable on branches that predate that merge.
    from ui.auth import get_current_user
    return get_current_user


def rate_limited(action: str, *,
                 cursor_factory: Callable | None = None,
                 user_resolver: Callable | None = None,
                 tier_resolver: Callable | None = None) -> Callable:
    """Dependency factory: authenticate, then enforce `action`'s rate limit.

    cursor_factory  () -> DB cursor over usage_events. The caller owns the
                    connection lifecycle; the dependency only reads.
    user_resolver   (Request) -> user with .id; defaults to
                    ui.auth.get_current_user (raises 401 itself).
    tier_resolver   (user) -> "free" | "paid" | Policy. Defaults to "free"
                    for everyone until P3-BILLING lands.

    Returns the resolved user, so routes can take the guard as their user
    dependency instead of stacking two.
    """

    def dependency(request: Request):
        resolve_user = user_resolver or _default_user_resolver()
        user = resolve_user(request)                      # 401s propagate
        user_id = getattr(user, "id", None) or str(user)

        tier = tier_resolver(user) if tier_resolver else "free"
        policy = tier if isinstance(tier, Policy) else get_policy(tier)

        cursor = None
        if cursor_factory is not None:
            try:
                cursor = cursor_factory()
            except Exception as exc:
                alerts.alert("ratelimit_db_error", {
                    "action": action, "user_id": str(user_id),
                    "error": repr(exc), "stage": "cursor_factory",
                })
        if cursor is None:
            # Fail open (availability beats enforcement) but never silently:
            # a missing cursor_factory is a wiring bug worth an alert per hit.
            if cursor_factory is None:
                alerts.alert("ratelimit_db_error", {
                    "action": action, "detail": "no cursor_factory wired; failing open",
                })
            return user

        decision = check_limit(cursor, user_id, action, policy=policy)
        if not decision.allowed:
            headers = {}
            if decision.retry_after is not None:
                headers["Retry-After"] = str(decision.retry_after)
            raise HTTPException(status_code=429,
                                detail=decision.reason or "rate limit exceeded",
                                headers=headers)
        return user

    return dependency

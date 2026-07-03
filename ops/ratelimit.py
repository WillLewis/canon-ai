"""Per-account rate limits (P3-OPS), backed by usage_events counts.

Sliding windows over usage_events.created_at — no new tables, no Redis. The
free-tier guardrails come from docs/readers-report.md ("Unit economics"):
account required; 1 world; 1 ingested script (<= 130 pages); 3 report runs a
day; 20 ask queries an hour. Paid ceilings exist only as abuse guards.

Counting contract: an action counts when a usage_events row exists with
kind == the action name. Routes call record_action(...) after the action
succeeds; LLM metering rows (ops.metering) use different kind values
("extraction", "report_f1", ...) so they never collide with action counts.

FAIL-OPEN: if the usage_events query errors, check_limit ALLOWS the request
and emits a `ratelimit_db_error` alert. Availability beats enforcement — a
down rate-limit store must never take the product down with it; the alert is
how we notice we're flying unmetered.
"""

from __future__ import annotations

import math
import os
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Mapping

from . import alerts

HOUR = 3600
DAY = 86400


@dataclass(frozen=True)
class Limit:
    max_events: int
    window_seconds: int | None   # None = lifetime cap (count all rows ever)

    @property
    def window_label(self) -> str:
        if self.window_seconds is None:
            return "per account"
        if self.window_seconds == DAY:
            return "per day"
        if self.window_seconds == HOUR:
            return "per hour"
        return f"per {self.window_seconds}s"


@dataclass(frozen=True)
class Policy:
    tier: str
    limits: Mapping[str, Limit]
    max_script_pages: int


@dataclass(frozen=True)
class Decision:
    allowed: bool
    reason: str | None = None
    retry_after: int | None = None   # seconds; None when there is no window to wait out

    def __bool__(self) -> bool:
        return self.allowed


def allowed() -> Decision:
    return Decision(True)


def denied(reason: str, retry_after: int | None = None) -> Decision:
    return Decision(False, reason=reason, retry_after=retry_after)


# --- policies -----------------------------------------------------------------
# Windows are fixed per action; env vars override the max counts only (one
# knob per limit keeps the env surface small). Override names:
#   CANON_LIMIT_<TIER>_<ACTION>   e.g. CANON_LIMIT_FREE_ASK=30
#   CANON_<TIER>_MAX_PAGES        e.g. CANON_FREE_MAX_PAGES=150

ACTION_WINDOWS: dict[str, int | None] = {
    "world_create": None,     # lifetime cap
    "script_ingest": None,    # lifetime cap
    "report_run": DAY,
    "ask": HOUR,
}

DEFAULT_MAX_EVENTS: dict[str, dict[str, int]] = {
    "free": {"world_create": 1, "script_ingest": 1, "report_run": 3, "ask": 20},
    # Paid ceilings are abuse guards, not product limits.
    "paid": {"world_create": 25, "script_ingest": 100, "report_run": 50, "ask": 500},
}

DEFAULT_MAX_PAGES: dict[str, int] = {"free": 130, "paid": 600}


def get_policy(tier: str = "free") -> Policy:
    """Build the policy for a tier, reading env overrides at call time."""
    if tier not in DEFAULT_MAX_EVENTS:
        raise ValueError(f"unknown tier {tier!r} (expected one of {sorted(DEFAULT_MAX_EVENTS)})")
    limits = {}
    for action, default_max in DEFAULT_MAX_EVENTS[tier].items():
        raw = os.environ.get(f"CANON_LIMIT_{tier.upper()}_{action.upper()}")
        limits[action] = Limit(int(raw) if raw else default_max, ACTION_WINDOWS[action])
    raw_pages = os.environ.get(f"CANON_{tier.upper()}_MAX_PAGES")
    pages = int(raw_pages) if raw_pages else DEFAULT_MAX_PAGES[tier]
    return Policy(tier, limits, pages)


# Import-time snapshots for callers that don't need live env re-reads.
FREE = get_policy("free")
PAID = get_policy("paid")


# --- checks ---------------------------------------------------------------------

def check_limit(cursor, user_id, action: str, policy: Policy | None = None,
                now: datetime | None = None) -> Decision:
    """Is `user_id` allowed to perform `action` right now?

    Counts usage_events rows with kind == action inside the sliding window
    (or all rows for lifetime caps). Deny includes retry_after: seconds until
    the oldest in-window event ages out.
    """
    policy = policy or get_policy("free")
    limit = policy.limits.get(action)
    if limit is None:
        return allowed()   # unlimited action for this tier
    now = now or datetime.now(timezone.utc)

    try:
        if limit.window_seconds is None:
            cursor.execute(
                "SELECT COUNT(*) FROM usage_events WHERE user_id = %s AND kind = %s",
                (user_id, action),
            )
            row = cursor.fetchone() or (0,)
            count, oldest = row[0], None
        else:
            floor = now - timedelta(seconds=limit.window_seconds)
            cursor.execute(
                "SELECT COUNT(*), MIN(created_at) FROM usage_events "
                "WHERE user_id = %s AND kind = %s AND created_at >= %s",
                (user_id, action, floor),
            )
            row = cursor.fetchone() or (0, None)
            count, oldest = row[0], row[1]
    except Exception as exc:
        # FAIL OPEN (see module docstring): availability beats enforcement.
        alerts.alert("ratelimit_db_error", {
            "action": action, "user_id": str(user_id), "error": repr(exc),
        })
        return Decision(True, reason="rate-limit store unavailable; failing open")

    if count < limit.max_events:
        return allowed()

    retry_after = None
    if limit.window_seconds is not None and oldest is not None:
        remaining = (oldest + timedelta(seconds=limit.window_seconds) - now).total_seconds()
        retry_after = max(1, math.ceil(remaining))
    reason = (f"{policy.tier} tier allows {limit.max_events} "
              f"{action} {limit.window_label}")
    alerts.record_rate_limit_denial(user_id, action)
    return denied(reason, retry_after)


def check_script_pages(pages: int, policy: Policy | None = None) -> Decision:
    """Static guardrail: free scripts are capped at 130 pages (readers-report)."""
    policy = policy or get_policy("free")
    if pages <= policy.max_script_pages:
        return allowed()
    return denied(f"{policy.tier} tier accepts scripts up to "
                  f"{policy.max_script_pages} pages (got {pages})")


def record_action(cursor, *, user_id, action: str, world_id=None) -> None:
    """Log a countable action as a zero-cost usage_events row. Call AFTER the
    action succeeds — failed/denied attempts must not consume quota."""
    cursor.execute(
        "INSERT INTO usage_events (user_id, world_id, kind) VALUES (%s, %s, %s)",
        (user_id, world_id, action),
    )

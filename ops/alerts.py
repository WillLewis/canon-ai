"""Alerting (P3-OPS): threshold checks + a single dispatch point.

alert(event, payload) always logs (logging.getLogger("canon.ops")), and POSTs
JSON to ALERT_WEBHOOK_URL if that env var is set — silent no-op when unset.
Stdlib only (urllib); no external alerting service. Alerts must never crash
the caller: webhook failures are logged and swallowed.

Threshold checks in here:

    check_report_cogs   per-report COGS > $3 — the docs/readers-report.md
                        "revisit if >$3 real-world COGS" trigger
    check_daily_spend   rolling 24h spend across all users > env cap
    record_rate_limit_denial
                        in-process denial-spike detector (many 429s in a
                        short window usually means abuse or a broken client)
    eval_failed         hook stub for the eval gate (docs/workstreams.md:
                        main is green or it reverts) — wire from CI later
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
import urllib.request
from collections import deque
from datetime import datetime, timedelta, timezone

log = logging.getLogger("canon.ops")

WEBHOOK_ENV = "ALERT_WEBHOOK_URL"

# Thresholds (env-overridable; defaults are launch-sane).
REPORT_COGS_ALERT_USD = 3.00          # CANON_REPORT_COGS_ALERT_USD
DAILY_SPEND_CAP_USD = 50.00           # CANON_DAILY_SPEND_CAP_USD
DENIAL_SPIKE_THRESHOLD = 20           # CANON_DENIAL_SPIKE_THRESHOLD
DENIAL_SPIKE_WINDOW_SECONDS = 300     # CANON_DENIAL_SPIKE_WINDOW_SECONDS


def _env_float(name: str, default: float) -> float:
    raw = os.environ.get(name)
    try:
        return float(raw) if raw else default
    except ValueError:
        return default


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    try:
        return int(raw) if raw else default
    except ValueError:
        return default


def alert(event: str, payload: dict | None = None) -> bool:
    """Dispatch an alert: always log; POST to the webhook iff configured.
    Returns True only when a webhook delivery succeeded."""
    payload = payload or {}
    log.warning("ALERT %s %s", event, json.dumps(payload, default=str, sort_keys=True))
    url = os.environ.get(WEBHOOK_ENV)
    if not url:
        return False  # silent no-op: logging above is the whole story
    try:
        body = json.dumps({"event": event, "payload": payload}, default=str).encode("utf-8")
        req = urllib.request.Request(
            url, data=body, headers={"Content-Type": "application/json"}, method="POST"
        )
        urllib.request.urlopen(req, timeout=5)
        return True
    except Exception:
        log.exception("alert webhook delivery failed (event=%s)", event)
        return False


# --- threshold checks ---------------------------------------------------------

def check_report_cogs(cost_usd, *, world_id=None, user_id=None) -> bool:
    """Fire when a single report run cost more than the revisit trigger."""
    cap = _env_float("CANON_REPORT_COGS_ALERT_USD", REPORT_COGS_ALERT_USD)
    if float(cost_usd) > cap:
        alert("report_cogs_exceeded", {
            "cost_usd": float(cost_usd), "cap_usd": cap,
            "world_id": world_id, "user_id": str(user_id) if user_id else None,
        })
        return True
    return False


def check_daily_spend(cursor, now: datetime | None = None) -> bool:
    """Fire when total spend over the trailing 24h exceeds the env cap."""
    cap = _env_float("CANON_DAILY_SPEND_CAP_USD", DAILY_SPEND_CAP_USD)
    now = now or datetime.now(timezone.utc)
    cursor.execute(
        "SELECT COALESCE(SUM(cost_usd), 0) FROM usage_events WHERE created_at >= %s",
        (now - timedelta(days=1),),
    )
    row = cursor.fetchone()
    spend = float(row[0]) if row and row[0] is not None else 0.0
    if spend > cap:
        alert("daily_spend_cap_exceeded", {"spend_usd": spend, "cap_usd": cap})
        return True
    return False


# --- rate-limit denial spike ----------------------------------------------------
# In-process on purpose: a spike detector only needs to be roughly right, and
# keeping it out of the DB means it still works when the DB is the problem.

_denials: deque[float] = deque()
_denials_lock = threading.Lock()


def record_rate_limit_denial(user_id, action: str, now: float | None = None) -> bool:
    """Count a 429; fire once and reset when denials-in-window hits threshold."""
    threshold = _env_int("CANON_DENIAL_SPIKE_THRESHOLD", DENIAL_SPIKE_THRESHOLD)
    window = _env_int("CANON_DENIAL_SPIKE_WINDOW_SECONDS", DENIAL_SPIKE_WINDOW_SECONDS)
    t = time.time() if now is None else now
    with _denials_lock:
        _denials.append(t)
        floor = t - window
        while _denials and _denials[0] < floor:
            _denials.popleft()
        if len(_denials) < threshold:
            return False
        count = len(_denials)
        _denials.clear()  # reset so the alert fires once per spike, not per denial
    alert("rate_limit_denial_spike", {
        "denials_in_window": count, "window_seconds": window,
        "last_user_id": str(user_id), "last_action": action,
    })
    return True


def _reset_denials() -> None:
    """Test hook: clear the in-process denial window."""
    with _denials_lock:
        _denials.clear()


# --- eval failure hook (stub) --------------------------------------------------

def eval_failed(payload: dict | None = None) -> None:
    """Stub for the eval gate: call from CI/run_eval when the gate goes red.
    Kept here so 'what fires alerts' has one home; wiring lands with CI."""
    alert("eval_failure", payload or {})

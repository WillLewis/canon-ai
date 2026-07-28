"""Ops floor tests (P3-OPS) — all offline, fake cursor/connection pattern.

Cost math per model (incl. the unpriced fallback), the meter decorator and
context manager reading usage off a fake Anthropic response, sliding-window
counting per action, deny -> allowed after the window, the fail-open path
emitting an alert, the FastAPI dependency returning 429 + Retry-After via
TestClient on a toy app, and webhook no-op when the env is unset.

    python -m pytest -q tests/test_ops.py
    python tests/test_ops.py
"""

import contextlib
import os
import pathlib
import sys
import urllib.request
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from types import SimpleNamespace

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from fastapi import Depends, FastAPI  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from ops import alerts, metering, ratelimit  # noqa: E402
from ops.middleware import rate_limited  # noqa: E402

NOW = datetime(2026, 7, 3, 12, 0, 0, tzinfo=timezone.utc)
UID = "11111111-1111-1111-1111-111111111111"


# --- fakes -------------------------------------------------------------------

class FakeCursor:
    """Answers the exact queries ops issues, from an in-memory event list.

    events: list of (kind, created_at) rows for the user under test.
    canned: fetchone() result for the rollup (SUM ...) queries.
    fail:   every execute raises, for the fail-open path.
    """

    def __init__(self, events=(), canned=None, fail=False):
        self.events = list(events)
        self.canned = canned
        self.fail = fail
        self.calls = []
        self.inserted = []
        self._result = None

    def execute(self, sql, params=None):
        if self.fail:
            raise RuntimeError("db down")
        self.calls.append((sql, params))
        s = " ".join(sql.lower().split())
        if s.startswith("insert into usage_events"):
            self.inserted.append(params)
            self._result = None
        elif s.startswith("select count(*), min(created_at)"):
            kind, floor = params[1], params[2]
            hits = [t for k, t in self.events if k == kind and t >= floor]
            self._result = (len(hits), min(hits) if hits else None)
        elif s.startswith("select count(*)"):
            kind = params[1]
            hits = [t for k, t in self.events if k == kind]
            self._result = (len(hits),)
        else:  # rollup / spend queries
            self._result = self.canned

    def fetchone(self):
        return self._result


class FakeUsage:
    def __init__(self, tokens_in, tokens_out):
        self.input_tokens = tokens_in
        self.output_tokens = tokens_out


class FakeResponse:
    def __init__(self, tokens_in=1000, tokens_out=200, model="claude-sonnet-5"):
        self.usage = FakeUsage(tokens_in, tokens_out)
        self.model = model
        self.content = []


@contextlib.contextmanager
def env(**pairs):
    saved = {k: os.environ.get(k) for k in pairs}
    try:
        for k, v in pairs.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        yield
    finally:
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


@contextlib.contextmanager
def captured_alerts():
    """Swallow real dispatch; record (event, payload) pairs."""
    seen = []
    real = alerts.alert
    alerts.alert = lambda event, payload=None: seen.append((event, payload or {})) or False
    try:
        yield seen
    finally:
        alerts.alert = real


# --- cost math ---------------------------------------------------------------

def test_cost_per_model():
    assert metering.cost_usd("claude-sonnet-5", 1_000_000, 1_000_000) == (Decimal("18"), True)
    assert metering.cost_usd("claude-opus-4-8", 1_000_000, 1_000_000) == (Decimal("30"), True)
    assert metering.cost_usd("claude-haiku-4-5", 1_000_000, 1_000_000) == (Decimal("6"), True)
    # a realistic small call: 3k in / 500 out on sonnet
    cost, priced = metering.cost_usd("claude-sonnet-5", 3000, 500)
    assert priced and cost == Decimal("0.0165")


def test_unknown_model_costs_zero_and_flags_kind():
    cost, priced = metering.cost_usd("claude-nonexistent-9", 5000, 5000)
    assert cost == 0 and not priced
    cur = FakeCursor()
    metering.record_usage(cur, user_id=UID, world_id=7, kind="extraction",
                          tokens_in=5000, tokens_out=5000, model="claude-nonexistent-9")
    (params,) = cur.inserted
    assert params[2] == "extraction:unpriced"
    assert params[5] == 0


def test_record_usage_inserts_priced_row():
    cur = FakeCursor()
    cost = metering.record_usage(cur, user_id=UID, world_id=7, kind="extraction",
                                 tokens_in=180_000, tokens_out=38_000,
                                 model="claude-sonnet-5")
    (params,) = cur.inserted
    assert params[:3] == (UID, 7, "extraction")
    assert params[3] == 180_000 and params[4] == 38_000
    assert cost == params[5] == Decimal("1.11")   # 0.54 in + 0.57 out


# --- meter decorator / context manager ----------------------------------------

def test_meter_decorator_reads_usage_off_response():
    cur = FakeCursor()
    m = metering.meter("extraction", cursor=cur, user_id=UID, world_id=7)
    fake_create = lambda **kwargs: FakeResponse(1000, 200, model="claude-sonnet-5")  # noqa: E731
    response = m(fake_create)(model="claude-sonnet-5", max_tokens=16000)
    assert isinstance(response, FakeResponse)      # response passes through
    (params,) = cur.inserted
    assert params[3] == 1000 and params[4] == 200
    assert params[5] == Decimal("0.006")           # 1000*3/M + 200*15/M
    assert m.cost == Decimal("0.006")


def test_meter_context_manager_and_model_fallback():
    cur = FakeCursor()
    with metering.meter("resolution", cursor=cur, user_id=UID, world_id=7,
                        model="claude-haiku-4-5") as m:
        resp = FakeResponse(2000, 100, model=None)   # response missing .model
        m.record(resp)
    (params,) = cur.inserted
    assert params[2] == "resolution"
    assert params[5] == Decimal("0.0025")            # priced via the fallback model


def test_meter_never_breaks_the_call_on_insert_failure():
    cur = FakeCursor(fail=True)
    with captured_alerts() as seen:
        m = metering.meter("extraction", cursor=cur, user_id=UID, world_id=7)
        response = m(lambda: FakeResponse())()
    assert isinstance(response, FakeResponse)         # response survived
    assert [e for e, _ in seen] == ["metering_error"]


# --- rollups -------------------------------------------------------------------

def test_per_world_and_per_user_cogs():
    cur = FakeCursor(canned=(Decimal("1.85"), 285_000, 56_000, 60))
    r = metering.per_world_cogs(cur, 7)
    assert r == {"cost_usd": Decimal("1.85"), "tokens_in": 285_000,
                 "tokens_out": 56_000, "events": 60}
    since = NOW - timedelta(days=30)
    metering.per_user_cogs(cur, UID, since=since)
    sql, params = cur.calls[-1]
    assert "created_at >= %s" in sql and params == (UID, since)


# --- rate limiting: window counting per action -----------------------------------

def _events(kind, n, age_seconds):
    return [(kind, NOW - timedelta(seconds=age_seconds)) for _ in range(n)]


def test_ask_allowed_under_hourly_limit():
    cur = FakeCursor(events=_events("ask", 19, 600))
    d = ratelimit.check_limit(cur, UID, "ask", policy=ratelimit.get_policy("free"), now=NOW)
    assert d.allowed


def test_ask_denied_at_hourly_limit_with_retry_after():
    cur = FakeCursor(events=_events("ask", 20, 600))   # 20 asks, 10 min ago
    with captured_alerts():
        d = ratelimit.check_limit(cur, UID, "ask", now=NOW)
    assert not d.allowed
    assert d.retry_after == 3000                        # 3600 - 600
    assert "20 ask per hour" in d.reason


def test_report_run_daily_window():
    cur = FakeCursor(events=_events("report_run", 3, 4 * 3600))
    with captured_alerts():
        d = ratelimit.check_limit(cur, UID, "report_run", now=NOW)
    assert not d.allowed and d.retry_after == 20 * 3600
    # paid ceiling is higher: same usage passes
    d = ratelimit.check_limit(cur, UID, "report_run",
                              policy=ratelimit.get_policy("paid"), now=NOW)
    assert d.allowed


def test_lifetime_caps_for_world_and_script():
    cur = FakeCursor(events=[("world_create", NOW - timedelta(days=90))])
    with captured_alerts():
        d = ratelimit.check_limit(cur, UID, "world_create", now=NOW)
    assert not d.allowed and d.retry_after is None      # no window to wait out
    assert "per account" in d.reason
    cur2 = FakeCursor(events=[])
    assert ratelimit.check_limit(cur2, UID, "script_ingest", now=NOW).allowed


def test_deny_becomes_allowed_after_window_passes():
    cur = FakeCursor(events=_events("ask", 20, 600))
    with captured_alerts():
        assert not ratelimit.check_limit(cur, UID, "ask", now=NOW).allowed
    later = NOW + timedelta(seconds=3001)               # oldest event now aged out
    assert ratelimit.check_limit(cur, UID, "ask", now=later).allowed


def test_env_overrides_max_events_and_pages():
    with env(CANON_LIMIT_FREE_ASK="2", CANON_FREE_MAX_PAGES="150"):
        p = ratelimit.get_policy("free")
        assert p.limits["ask"].max_events == 2
        assert p.max_script_pages == 150
        cur = FakeCursor(events=_events("ask", 2, 60))
        with captured_alerts():
            assert not ratelimit.check_limit(cur, UID, "ask", policy=p, now=NOW).allowed


def test_script_pages_guardrail():
    assert ratelimit.check_script_pages(130).allowed
    d = ratelimit.check_script_pages(131)
    assert not d.allowed and "130 pages" in d.reason
    assert ratelimit.check_script_pages(131, ratelimit.get_policy("paid")).allowed


def test_unknown_action_is_unlimited():
    assert ratelimit.check_limit(FakeCursor(), UID, "export_bible", now=NOW).allowed


def test_record_action_writes_zero_cost_row():
    cur = FakeCursor()
    ratelimit.record_action(cur, user_id=UID, action="ask", world_id=7)
    (params,) = cur.inserted
    assert params == (UID, 7, "ask")


# --- fail-open ------------------------------------------------------------------

def test_fail_open_on_db_error_emits_alert():
    cur = FakeCursor(fail=True)
    with captured_alerts() as seen:
        d = ratelimit.check_limit(cur, UID, "ask", now=NOW)
    assert d.allowed                                     # availability beats enforcement
    assert [e for e, _ in seen] == ["ratelimit_db_error"]
    assert seen[0][1]["action"] == "ask"


# --- middleware (toy app, TestClient) ---------------------------------------------

def _toy_app(cursor, user_resolver=None):
    app = FastAPI()
    guard = rate_limited("ask", cursor_factory=lambda: cursor,
                         user_resolver=user_resolver or
                         (lambda request: SimpleNamespace(id=UID)))

    @app.get("/ask")
    def ask(user=Depends(guard)):
        return {"user": user.id}

    return app


def test_middleware_allows_under_limit_and_returns_user():
    client = TestClient(_toy_app(FakeCursor(events=[])))
    r = client.get("/ask")
    assert r.status_code == 200 and r.json() == {"user": UID}


def test_middleware_returns_429_with_retry_after():
    cur = FakeCursor(events=[("ask", datetime.now(timezone.utc) - timedelta(seconds=600))
                             for _ in range(20)])
    client = TestClient(_toy_app(cur))
    with captured_alerts():
        r = client.get("/ask")
    assert r.status_code == 429
    assert "20 ask per hour" in r.json()["detail"]
    assert 0 < int(r.headers["retry-after"]) <= 3600


def test_middleware_auth_errors_propagate():
    from fastapi import HTTPException

    def resolver(request):
        raise HTTPException(status_code=401, detail="not authenticated")

    client = TestClient(_toy_app(FakeCursor(), user_resolver=resolver))
    assert client.get("/ask").status_code == 401


def test_middleware_fails_open_when_cursor_factory_breaks():
    def bad_factory():
        raise RuntimeError("pool exhausted")

    app = FastAPI()
    guard = rate_limited("ask", cursor_factory=bad_factory,
                         user_resolver=lambda request: SimpleNamespace(id=UID))

    @app.get("/ask")
    def ask(user=Depends(guard)):
        return {"ok": True}

    with captured_alerts() as seen:
        r = TestClient(app).get("/ask")
    assert r.status_code == 200                          # fail open
    assert [e for e, _ in seen] == ["ratelimit_db_error"]


# --- alerts ----------------------------------------------------------------------

def test_webhook_noop_when_env_unset():
    def boom(*args, **kwargs):
        raise AssertionError("webhook must not be called when env is unset")

    real = urllib.request.urlopen
    urllib.request.urlopen = boom
    try:
        with env(ALERT_WEBHOOK_URL=None):
            assert alerts.alert("test_event", {"k": "v"}) is False
    finally:
        urllib.request.urlopen = real


def test_webhook_posts_json_when_env_set():
    posted = []

    def fake_urlopen(req, timeout=None):
        posted.append((req.full_url, req.data))
        return SimpleNamespace(status=200)

    real = urllib.request.urlopen
    urllib.request.urlopen = fake_urlopen
    try:
        with env(ALERT_WEBHOOK_URL="https://hooks.example/canon"):
            assert alerts.alert("test_event", {"k": "v"}) is True
    finally:
        urllib.request.urlopen = real
    assert posted and posted[0][0] == "https://hooks.example/canon"
    assert b'"test_event"' in posted[0][1]


def test_report_cogs_threshold():
    with captured_alerts() as seen:
        assert not alerts.check_report_cogs(1.85, world_id=7)
        assert alerts.check_report_cogs(3.10, world_id=7)
    assert [e for e, _ in seen] == ["report_cogs_exceeded"]
    assert seen[0][1]["cost_usd"] == 3.10


def test_daily_spend_cap_uses_env():
    with env(CANON_DAILY_SPEND_CAP_USD="10"):
        with captured_alerts() as seen:
            assert alerts.check_daily_spend(FakeCursor(canned=(Decimal("12.50"),)), now=NOW)
            assert not alerts.check_daily_spend(FakeCursor(canned=(Decimal("9.99"),)), now=NOW)
    assert [e for e, _ in seen] == ["daily_spend_cap_exceeded"]


def test_denial_spike_fires_once_at_threshold():
    alerts._reset_denials()
    with env(CANON_DENIAL_SPIKE_THRESHOLD="3", CANON_DENIAL_SPIKE_WINDOW_SECONDS="300"):
        with captured_alerts() as seen:
            assert not alerts.record_rate_limit_denial(UID, "ask", now=1000.0)
            assert not alerts.record_rate_limit_denial(UID, "ask", now=1001.0)
            assert alerts.record_rate_limit_denial(UID, "ask", now=1002.0)
            # window resets after firing: the next denial starts a fresh count
            assert not alerts.record_rate_limit_denial(UID, "ask", now=1003.0)
    assert [e for e, _ in seen] == ["rate_limit_denial_spike"]
    assert seen[0][1]["denials_in_window"] == 3
    alerts._reset_denials()


def test_denial_spike_ignores_stale_denials():
    alerts._reset_denials()
    with env(CANON_DENIAL_SPIKE_THRESHOLD="3", CANON_DENIAL_SPIKE_WINDOW_SECONDS="300"):
        with captured_alerts() as seen:
            alerts.record_rate_limit_denial(UID, "ask", now=1000.0)
            alerts.record_rate_limit_denial(UID, "ask", now=1001.0)
            # third denial arrives after the first two have aged out
            assert not alerts.record_rate_limit_denial(UID, "ask", now=2000.0)
    assert seen == []
    alerts._reset_denials()


def test_eval_failed_stub_dispatches():
    with captured_alerts() as seen:
        alerts.eval_failed({"gate": "phase0", "recall": 0.62})
    assert seen == [("eval_failure", {"gate": "phase0", "recall": 0.62})]


if __name__ == "__main__":
    import traceback

    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    failed = 0
    for fn in fns:
        try:
            fn()
            print(f"PASS {fn.__name__}")
        except Exception:
            failed += 1
            print(f"FAIL {fn.__name__}")
            traceback.print_exc()
    raise SystemExit(1 if failed else 0)

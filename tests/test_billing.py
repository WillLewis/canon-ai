"""Billing tests (P3-BILLING) — all offline, no stripe package installed.

Webhook signature accept/reject/expired (implemented against Stripe's
documented t=/v1= HMAC scheme, no SDK), each webhook event's state
transition, the resolve_tier matrix (none / active / past_due / canceled /
expired-period / CANON_FORCE_TIER), the checkout route (401 unauthenticated,
303 to a stubbed Stripe URL authenticated), plan_gate 402-for-free /
pass-for-paid, and the pricing page rendering both tier columns. Fake
cursors + TestClient on a toy app mounting billing.routes.router — the same
offline pattern as tests/test_ops.py and tests/test_auth.py.

    python -m pytest -q tests/test_billing.py
    python tests/test_billing.py
"""

import contextlib
import json
import os
import pathlib
import sys
import time
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from fastapi import Depends, FastAPI  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from billing import gating, store, stripe_client, webhooks  # noqa: E402
from billing import routes  # noqa: E402
from ops import alerts  # noqa: E402
from ui import auth  # noqa: E402

NOW = datetime(2026, 7, 3, 12, 0, 0, tzinfo=timezone.utc)
UID = "11111111-1111-1111-1111-111111111111"
OTHER = "22222222-2222-2222-2222-222222222222"
SECRET = "whsec-unit-billing-secret"
CUSTOMER = "cus_unit_1"
SUBSCRIPTION = "sub_unit_1"


# --- fakes ---------------------------------------------------------------------

class FakeCursor:
    """Answers exactly the SQL billing/store.py issues, from two dicts."""

    def __init__(self, fail=False):
        self.customers = {}   # user_id -> stripe_customer_id
        self.subs = {}        # user_id -> row dict
        self.fail = fail
        self.calls = []
        self._result = None

    def execute(self, sql, params=None):
        if self.fail:
            raise RuntimeError("db down")
        s = " ".join(sql.lower().split())
        self.calls.append((s, params))
        if s.startswith("insert into billing_customers"):
            self.customers[params[0]] = params[1]
            self._result = None
        elif s.startswith("insert into billing_subscriptions"):
            self.subs[params[0]] = {
                "stripe_subscription_id": params[1], "status": params[2],
                "tier": params[3], "current_period_end": params[4],
            }
            self._result = None
        elif "from billing_customers where user_id" in s:
            cust = self.customers.get(params[0])
            self._result = (cust,) if cust else None
        elif "from billing_customers where stripe_customer_id" in s:
            hits = [u for u, c in self.customers.items() if c == params[0]]
            self._result = (hits[0],) if hits else None
        elif "from billing_subscriptions where user_id" in s:
            row = self.subs.get(params[0])
            self._result = (
                (row["stripe_subscription_id"], row["status"], row["tier"],
                 row["current_period_end"]) if row else None)
        else:
            raise AssertionError(f"unexpected SQL: {s}")

    def fetchone(self):
        return self._result


def cursor_with_sub(status, period_end, tier=None):
    cur = FakeCursor()
    cur.subs[UID] = {"stripe_subscription_id": SUBSCRIPTION, "status": status,
                     "tier": tier or ("paid" if status in ("active", "trialing") else "free"),
                     "current_period_end": period_end}
    return cur


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
    seen = []
    real = alerts.alert
    alerts.alert = lambda event, payload=None: seen.append((event, payload or {})) or False
    try:
        yield seen
    finally:
        alerts.alert = real


@contextlib.contextmanager
def patched(module, **attrs):
    """Temporarily replace module attributes; restore on exit."""
    saved = {k: getattr(module, k) for k in attrs}
    for k, v in attrs.items():
        setattr(module, k, v)
    try:
        yield
    finally:
        for k, v in saved.items():
            setattr(module, k, v)


def toy_app(cursor, *, user=None):
    """The billing router on a bare app, with the cursor seam faked and
    (optionally) auth overridden to a fixed user."""
    app = FastAPI()
    app.include_router(routes.router)
    if user is not None:
        app.dependency_overrides[auth.get_current_user] = lambda: user
    return app


@contextlib.contextmanager
def client_for(cursor, *, user=None, peek=None):
    app = toy_app(cursor, user=user)
    ctx = lambda: contextlib.nullcontext(cursor)  # noqa: E731
    with patched(routes, cursor_ctx=ctx, peek_user=(peek or (lambda request: None))):
        # No dev bypass, no ambient tier force leaking in from the shell.
        with env(AUTH_DISABLED=None, CANON_FORCE_TIER=None):
            yield TestClient(app, follow_redirects=False)


def signed_post(client, payload: bytes, secret=SECRET, timestamp=None, header=None):
    header = header if header is not None else webhooks.compute_signature(
        payload, secret, timestamp)
    return client.post("/billing/webhook", content=payload,
                       headers={"stripe-signature": header})


def event_bytes(event_type, obj):
    return json.dumps({"type": event_type, "data": {"object": obj}}).encode()


# --- the stripe package is genuinely absent ------------------------------------

def test_suite_runs_without_the_stripe_sdk():
    assert not stripe_client.stripe_available(), (
        "these tests assert offline behavior; stripe must not be installed "
        "in the test env (it is an optional extra)")
    try:
        stripe_client.create_checkout_session(UID, "w@example.com")
    except RuntimeError as e:
        assert "optional" in str(e)
    else:
        raise AssertionError("expected RuntimeError without the SDK")


# --- webhook signature scheme ----------------------------------------------------

PAYLOAD = b'{"type":"ping","data":{"object":{}}}'


def test_signature_accepts_valid_header():
    header = webhooks.compute_signature(PAYLOAD, SECRET, timestamp=1_000_000)
    assert webhooks.verify_signature(PAYLOAD, header, SECRET, now=1_000_010)


def test_signature_rejects_wrong_secret_and_tampered_payload():
    header = webhooks.compute_signature(PAYLOAD, SECRET, timestamp=1_000_000)
    assert not webhooks.verify_signature(PAYLOAD, header, "whsec-other", now=1_000_010)
    assert not webhooks.verify_signature(PAYLOAD + b" ", header, SECRET, now=1_000_010)


def test_signature_rejects_expired_and_future_timestamps():
    header = webhooks.compute_signature(PAYLOAD, SECRET, timestamp=1_000_000)
    assert not webhooks.verify_signature(PAYLOAD, header, SECRET, now=1_000_301)
    assert webhooks.verify_signature(PAYLOAD, header, SECRET, now=1_000_300)
    # a timestamp from the future is as wrong as a stale one
    assert not webhooks.verify_signature(PAYLOAD, header, SECRET, now=999_699)


def test_signature_rejects_malformed_headers():
    for bad in (None, "", "v1=abc", "t=notanumber,v1=abc", "t=1000000",
                "utter garbage"):
        assert not webhooks.verify_signature(PAYLOAD, bad, SECRET, now=1_000_000)


def test_signature_accepts_any_matching_v1_of_several():
    good = webhooks.compute_signature(PAYLOAD, SECRET, timestamp=1_000_000)
    t_part, v1_part = good.split(",")
    header = f"{t_part},v1={'0' * 64},{v1_part},v0=ignored"
    assert webhooks.verify_signature(PAYLOAD, header, SECRET, now=1_000_010)


# --- webhook event -> state transitions --------------------------------------------

def test_checkout_completed_records_customer_and_paid_sub():
    cur = FakeCursor()
    action = webhooks.handle_event(cur, {
        "type": "checkout.session.completed",
        "data": {"object": {
            "client_reference_id": UID, "customer": CUSTOMER,
            "subscription": SUBSCRIPTION, "metadata": {"user_id": UID},
        }},
    })
    assert "paid" in action
    assert cur.customers[UID] == CUSTOMER
    sub = cur.subs[UID]
    assert sub["status"] == "active" and sub["tier"] == "paid"
    assert sub["current_period_end"] is None       # event carries no period end
    assert store.resolve_tier(cur, UID, now=NOW) == "paid"   # grace applies


def test_subscription_updated_active_sets_paid_with_period_end():
    cur = FakeCursor()
    cur.customers[UID] = CUSTOMER
    period_end = int((NOW + timedelta(days=30)).timestamp())
    webhooks.handle_event(cur, {
        "type": "customer.subscription.updated",
        "data": {"object": {"id": SUBSCRIPTION, "customer": CUSTOMER,
                            "status": "active",
                            "current_period_end": period_end}},
    })
    sub = cur.subs[UID]
    assert sub["tier"] == "paid" and sub["status"] == "active"
    assert sub["current_period_end"] == datetime.fromtimestamp(period_end, tz=timezone.utc)
    assert store.resolve_tier(cur, UID, now=NOW) == "paid"


def test_subscription_updated_past_due_downgrades():
    cur = FakeCursor()
    cur.customers[UID] = CUSTOMER
    webhooks.handle_event(cur, {
        "type": "customer.subscription.updated",
        "data": {"object": {"id": SUBSCRIPTION, "customer": CUSTOMER,
                            "status": "past_due",
                            "current_period_end": int((NOW + timedelta(days=3)).timestamp())}},
    })
    assert cur.subs[UID]["tier"] == "free"
    assert store.resolve_tier(cur, UID, now=NOW) == "free"


def test_subscription_updated_reads_period_end_from_items_fallback():
    cur = FakeCursor()
    cur.customers[UID] = CUSTOMER
    period_end = int((NOW + timedelta(days=30)).timestamp())
    webhooks.handle_event(cur, {
        "type": "customer.subscription.updated",
        "data": {"object": {"id": SUBSCRIPTION, "customer": CUSTOMER,
                            "status": "active",
                            "items": {"data": [{"current_period_end": period_end}]}}},
    })
    assert cur.subs[UID]["current_period_end"] is not None
    assert store.resolve_tier(cur, UID, now=NOW) == "paid"


def test_subscription_deleted_downgrades_to_free():
    cur = FakeCursor()
    cur.customers[UID] = CUSTOMER
    cur.subs[UID] = {"stripe_subscription_id": SUBSCRIPTION, "status": "active",
                     "tier": "paid",
                     "current_period_end": NOW + timedelta(days=20)}
    action = webhooks.handle_event(cur, {
        "type": "customer.subscription.deleted",
        "data": {"object": {"id": SUBSCRIPTION, "customer": CUSTOMER,
                            "status": "canceled"}},
    })
    assert "free" in action
    assert cur.subs[UID]["tier"] == "free" and cur.subs[UID]["status"] == "canceled"
    assert store.resolve_tier(cur, UID, now=NOW) == "free"


def test_unknown_event_is_ignored_without_writes():
    cur = FakeCursor()
    action = webhooks.handle_event(cur, {"type": "invoice.paid",
                                         "data": {"object": {"id": "in_1"}}})
    assert action.startswith("ignored")
    assert cur.customers == {} and cur.subs == {}


def test_unattributable_event_writes_nothing_and_alerts():
    cur = FakeCursor()   # no customer mapping, no metadata
    with captured_alerts() as seen:
        action = webhooks.handle_event(cur, {
            "type": "customer.subscription.updated",
            "data": {"object": {"id": SUBSCRIPTION, "customer": "cus_stranger",
                                "status": "active"}},
        })
    assert action.startswith("unattributed")
    assert cur.subs == {}
    assert [e for e, _ in seen] == ["billing_webhook_unattributed"]


def test_attribution_falls_back_to_metadata_user_id():
    cur = FakeCursor()   # customer unknown, but metadata carries the user
    webhooks.handle_event(cur, {
        "type": "customer.subscription.updated",
        "data": {"object": {"id": SUBSCRIPTION, "customer": "cus_stranger",
                            "status": "active",
                            "metadata": {"user_id": UID},
                            "current_period_end": int((NOW + timedelta(days=30)).timestamp())}},
    })
    assert cur.subs[UID]["tier"] == "paid"


# --- resolve_tier matrix -----------------------------------------------------------

def test_resolve_tier_no_subscription_is_free():
    with env(CANON_FORCE_TIER=None):
        assert store.resolve_tier(FakeCursor(), UID, now=NOW) == "free"


def test_resolve_tier_active_and_trialing_with_future_period_are_paid():
    with env(CANON_FORCE_TIER=None):
        future = NOW + timedelta(days=12)
        assert store.resolve_tier(cursor_with_sub("active", future), UID, now=NOW) == "paid"
        assert store.resolve_tier(cursor_with_sub("trialing", future), UID, now=NOW) == "paid"


def test_resolve_tier_past_due_and_canceled_are_free():
    with env(CANON_FORCE_TIER=None):
        future = NOW + timedelta(days=12)
        assert store.resolve_tier(cursor_with_sub("past_due", future), UID, now=NOW) == "free"
        assert store.resolve_tier(cursor_with_sub("canceled", future), UID, now=NOW) == "free"


def test_resolve_tier_expired_period_is_free():
    with env(CANON_FORCE_TIER=None):
        stale = NOW - timedelta(hours=1)
        assert store.resolve_tier(cursor_with_sub("active", stale), UID, now=NOW) == "free"


def test_resolve_tier_active_with_null_period_is_paid_grace():
    # checkout.session.completed carries no period end; the upgrade must hold
    # until customer.subscription.updated fills the real date.
    with env(CANON_FORCE_TIER=None):
        assert store.resolve_tier(cursor_with_sub("active", None), UID, now=NOW) == "paid"


def test_resolve_tier_env_override_wins_both_ways():
    with env(CANON_FORCE_TIER="paid"):
        assert store.resolve_tier(FakeCursor(), UID, now=NOW) == "paid"
    with env(CANON_FORCE_TIER="free"):
        cur = cursor_with_sub("active", NOW + timedelta(days=12))
        assert store.resolve_tier(cur, UID, now=NOW) == "free"
    with env(CANON_FORCE_TIER="platinum"):   # unknown values are ignored
        assert store.resolve_tier(FakeCursor(), UID, now=NOW) == "free"


def test_resolve_tier_db_error_fails_to_free_with_alert():
    with env(CANON_FORCE_TIER=None):
        with captured_alerts() as seen:
            assert store.resolve_tier(FakeCursor(fail=True), UID, now=NOW) == "free"
    assert [e for e, _ in seen] == ["billing_db_error"]


def test_make_tier_resolver_plugs_into_middleware_shape():
    cur = cursor_with_sub("active", NOW + timedelta(days=12))
    with env(CANON_FORCE_TIER=None):
        resolver = store.make_tier_resolver(lambda: cur)
        assert resolver(SimpleNamespace(id=UID)) == "paid"
        assert resolver(SimpleNamespace(id=OTHER)) == "free"
        with captured_alerts() as seen:
            broken = store.make_tier_resolver(lambda: (_ for _ in ()).throw(RuntimeError()))
            assert broken(SimpleNamespace(id=UID)) == "free"
        assert [e for e, _ in seen] == ["billing_db_error"]


# --- checkout / portal routes ---------------------------------------------------------

def test_checkout_requires_auth():
    with client_for(FakeCursor()) as client:
        r = client.post("/billing/checkout")
    assert r.status_code == 401


def test_checkout_redirects_to_stripe_url_when_authenticated():
    user = auth.User(id=UID, email="writer@example.com")
    seen = []

    def fake_session(user_id, email):
        seen.append((user_id, email))
        return "https://checkout.stripe.example/session-abc"

    with patched(stripe_client, create_checkout_session=fake_session):
        with client_for(FakeCursor(), user=user) as client:
            r = client.post("/billing/checkout")
    assert r.status_code == 303
    assert r.headers["location"] == "https://checkout.stripe.example/session-abc"
    assert seen == [(UID, "writer@example.com")]


def test_checkout_unconfigured_is_503_not_500():
    # No stripe SDK, no env: the guarded client raises RuntimeError -> 503.
    user = auth.User(id=UID, email="writer@example.com")
    with env(STRIPE_SECRET_KEY=None, STRIPE_PRICE_ID=None):
        with client_for(FakeCursor(), user=user) as client:
            r = client.post("/billing/checkout")
    assert r.status_code == 503


def test_portal_requires_auth_then_redirects_for_known_customer():
    with client_for(FakeCursor()) as client:
        assert client.get("/billing/portal").status_code == 401

    user = auth.User(id=UID, email="writer@example.com")
    cur = FakeCursor()
    with client_for(cur, user=user) as client:
        assert client.get("/billing/portal").status_code == 404   # never upgraded

    cur.customers[UID] = CUSTOMER
    with patched(stripe_client, create_portal_session=lambda cid: f"https://portal.stripe.example/{cid}"):
        with client_for(cur, user=user) as client:
            r = client.get("/billing/portal")
    assert r.status_code == 303
    assert r.headers["location"] == f"https://portal.stripe.example/{CUSTOMER}"


# --- webhook route ----------------------------------------------------------------------

def test_webhook_route_rejects_bad_signature_with_400():
    payload = event_bytes("checkout.session.completed", {"client_reference_id": UID})
    cur = FakeCursor()
    with env(STRIPE_WEBHOOK_SECRET=SECRET):
        with client_for(cur) as client:
            assert signed_post(client, payload, secret="whsec-wrong").status_code == 400
            assert client.post("/billing/webhook", content=payload).status_code == 400
    assert cur.customers == {} and cur.subs == {}


def test_webhook_route_rejects_expired_timestamp():
    payload = event_bytes("checkout.session.completed", {"client_reference_id": UID})
    with env(STRIPE_WEBHOOK_SECRET=SECRET):
        with client_for(FakeCursor()) as client:
            r = signed_post(client, payload, timestamp=int(time.time()) - 3600)
    assert r.status_code == 400


def test_webhook_route_503_when_secret_unset():
    with env(STRIPE_WEBHOOK_SECRET=None):
        with client_for(FakeCursor()) as client:
            r = signed_post(client, b"{}")
    assert r.status_code == 503


def test_webhook_route_applies_verified_event():
    payload = event_bytes("checkout.session.completed", {
        "client_reference_id": UID, "customer": CUSTOMER,
        "subscription": SUBSCRIPTION,
    })
    cur = FakeCursor()
    with env(STRIPE_WEBHOOK_SECRET=SECRET):
        with client_for(cur) as client:
            r = signed_post(client, payload)
    assert r.status_code == 200 and r.json()["received"] is True
    assert cur.customers[UID] == CUSTOMER
    assert cur.subs[UID]["tier"] == "paid"


def test_webhook_route_acks_unhandled_event_types():
    payload = event_bytes("invoice.paid", {"id": "in_1"})
    with env(STRIPE_WEBHOOK_SECRET=SECRET):
        with client_for(FakeCursor()) as client:
            r = signed_post(client, payload)
    assert r.status_code == 200          # Stripe retries non-2xx; ignored != error


# --- plan_gate ---------------------------------------------------------------------------

def gated_app(tier, feature="bible_export"):
    app = FastAPI()
    gate = gating.plan_gate(feature,
                            user_resolver=lambda request: SimpleNamespace(id=UID),
                            tier_resolver=lambda user: tier)

    @app.get("/feature")
    def feature_route(user=Depends(gate)):
        return {"ok": True, "user": user.id}

    return TestClient(app)


def test_plan_gate_402_for_free_with_upgrade_path():
    with env(CANON_FORCE_TIER=None):
        r = gated_app("free").get("/feature")
    assert r.status_code == 402
    detail = r.json()["detail"]
    assert detail["error"] == "payment_required"
    assert detail["feature"] == "bible_export"
    assert detail["upgrade_url"] == "/billing"


def test_plan_gate_passes_paid_and_returns_user():
    with env(CANON_FORCE_TIER=None):
        r = gated_app("paid").get("/feature")
    assert r.status_code == 200 and r.json()["user"] == UID


def test_plan_gate_ungated_feature_passes_free():
    with env(CANON_FORCE_TIER=None):
        r = gated_app("free", feature="ask").get("/feature")
    assert r.status_code == 200


def test_plan_gate_auth_errors_propagate():
    from fastapi import HTTPException

    app = FastAPI()
    gate = gating.plan_gate("bible_export", user_resolver=lambda request: (
        (_ for _ in ()).throw(HTTPException(status_code=401, detail="nope"))))

    @app.get("/feature")
    def feature_route(user=Depends(gate)):
        return {"ok": True}

    assert TestClient(app).get("/feature").status_code == 401


def test_plan_gate_env_force_paid_opens_the_gate():
    with env(CANON_FORCE_TIER="paid"):
        r = gated_app("free").get("/feature")   # resolver says free; force wins
    assert r.status_code == 200


def test_plan_gate_unwired_fails_closed_with_alert():
    app = FastAPI()
    gate = gating.plan_gate("retcon_ripple",
                            user_resolver=lambda request: SimpleNamespace(id=UID))

    @app.get("/feature")
    def feature_route(user=Depends(gate)):
        return {"ok": True}

    with env(CANON_FORCE_TIER=None):
        with captured_alerts() as seen:
            r = TestClient(app).get("/feature")
    assert r.status_code == 402
    assert [e for e, _ in seen] == ["billing_gate_unwired"]


# --- pricing page ----------------------------------------------------------------------

def test_pricing_page_renders_both_tiers_logged_out():
    with client_for(FakeCursor()) as client:
        r = client.get("/billing")
    assert r.status_code == 200
    body = r.text
    for needle in ("Free", "1 world", "1 script", "Share link",
                   "Multi-script worlds", "Bible export", "Retcon ripple",
                   "Per-world rule authoring", "$20"):
        assert needle in body, f"pricing page is missing {needle!r}"
    assert "Sign in" in body                       # logged-out upgrade path
    assert 'action="/billing/checkout"' not in body


def test_pricing_page_shows_current_tier_and_upgrade_for_free_user():
    user = auth.User(id=UID, email="writer@example.com")
    with client_for(FakeCursor(), peek=lambda request: user) as client:
        r = client.get("/billing")
    body = r.text
    assert r.status_code == 200
    assert "writer@example.com" in body
    assert 'tier-free' in body                     # current tier badge
    assert 'action="/billing/checkout"' in body    # upgrade button


def test_pricing_page_shows_manage_billing_for_paid_user():
    user = auth.User(id=UID, email="writer@example.com")
    cur = cursor_with_sub("active", NOW + timedelta(days=12))
    with client_for(cur, peek=lambda request: user) as client:
        r = client.get("/billing")
    body = r.text
    assert 'tier-paid' in body
    assert "/billing/portal" in body               # manage billing link
    assert 'action="/billing/checkout"' not in body


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

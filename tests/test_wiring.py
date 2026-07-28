"""Wave 5 cross-boundary wiring tests (P3-WIRING) — all offline.

Covers the deferred one-liners each wave left at its boundary, now connected:
extraction metering opt-in via env (silent no-op without it), the ask route's
rate limit with billing's tier resolver, share-link HTTP routes (owner-gated
create/revoke, public read-only GET, 404 without leakage), and report/findings
loading through canon.rules.compose_findings with a feature-detected
world_rules table. Fake cursors + TestClient over the real ui.app, the same
offline pattern as tests/test_surface.py and tests/test_ops.py. The rule
plan-gate (402 free / pass paid) lives with the rest of the rule-builder tests
in tests/test_rules.py.

    python -m pytest -q tests/test_wiring.py
    python tests/test_wiring.py
"""

import contextlib
import os
import pathlib
import sys
import time
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from types import SimpleNamespace

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

import jwt  # PyJWT — ui/requirements.txt  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from ask.engine import AskResult  # noqa: E402  (read-only import, never edited)
from extract import llm  # noqa: E402
from ui import auth, db, share_ui  # noqa: E402
from ui import app as app_mod  # noqa: E402
from ui.app import app  # noqa: E402

SECRET = "test-secret-0123456789abcdef0123456789abcdef"
WORLD = {"id": 1, "name": "greyharbor_s1"}
OWNER = "00000000-1111-1111-1111-111111111111"
EDITOR = "11111111-1111-1111-1111-111111111111"
ROLES = {OWNER: "owner", EDITOR: "editor"}
NOW = datetime(2026, 7, 3, 12, 0, 0, tzinfo=timezone.utc)

SUMMARY = {
    "totals": {"entities": 12, "assertions": 87, "scenes": 2, "works": 2,
               "findings_live": 1, "findings_sealed": 0},
    "entities_by_kind": [{"kind": "character", "n": 6, "provisional": 1}],
    "assertions_by_status": [{"status": "canon", "n": 80}, {"status": "draft", "n": 7}],
    "findings_by_sev": [{"severity": "critical", "live": 1, "sealed": 0}],
    "works": [{"id": 1, "title": "Pilot", "sort_order": 1, "n_scenes": 2}],
}

BASE_FINDING = {
    "id": 7, "check_name": "dead_speaker", "severity": "critical",
    "explanation": "Danny speaks in Ep2/sc1 after dying at pos 5.",
    "sealed": False, "scene_id": 9, "assertion_a": 21, "assertion_b": None,
    "scene_slug": "EXT. HARBOR - DAY", "story_position": 9,
    "episode_title": "Ep2", "subject_a": "Danny", "subject_b": None,
}

ANSWERED = AskResult(
    question="what happened to danny", status="answered",
    rows=[{"subject": "Danny", "predicate": "dies", "object": None, "from_pos": 5,
           "supporting_quote": "Danny is gone.", "scene_id": 9,
           "citation": "Ep2/sc1 (pos 9)", "citation_label": "Ep2/sc1"}],
    citations=["Ep2/sc1 (pos 9)"],
)


# --- helpers (repo-standard env/patch/fake-cursor patterns) --------------------

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
def patched(module, **attrs):
    saved = {k: getattr(module, k) for k in attrs}
    for k, v in attrs.items():
        setattr(module, k, v)
    try:
        yield
    finally:
        for k, v in saved.items():
            setattr(module, k, v)


def make_token(sub, expires_in=3600):
    now = int(time.time())
    return jwt.encode({"sub": sub, "aud": auth.JWT_AUDIENCE, "email": "w@example.com",
                       "iat": now, "exp": now + expires_in}, SECRET, algorithm="HS256")


def bearer(sub):
    return {"Authorization": f"Bearer {make_token(sub)}"}


class OpsFakeCursor:
    """Answers the usage_events queries ops issues (tests/test_ops.py shape)."""

    def __init__(self, events=(), fail=False):
        self.events = list(events)
        self.fail = fail
        self.inserted = []
        self._result = None

    def execute(self, sql, params=None):
        if self.fail:
            raise RuntimeError("db down")
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
        else:
            self._result = None

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


# --- app harness ----------------------------------------------------------------

@contextlib.contextmanager
def wired(findings=None, auth_disabled=True, tier=None,
          rules_conn=lambda: None, ops_cursor=None):
    """TestClient over the real app with every db function the wired routes
    touch faked. tier=None leaves CANON_FORCE_TIER unset; a string forces it.
    Yields (client, ops_cur)."""
    findings = [dict(BASE_FINDING)] if findings is None else findings
    ops_cur = ops_cursor if ops_cursor is not None else OpsFakeCursor()
    saved_env = {k: os.environ.get(k) for k in
                 ("SUPABASE_JWT_SECRET", "AUTH_DISABLED", "CANON_FORCE_TIER")}
    names = ("list_worlds", "member_role", "list_coverage_notes", "list_findings",
             "world_summary", "load_bearing", "list_scenes_with_text",
             "entity_names", "ask_question", "rules_conn", "ops_cursor")
    saved_fns = {n: getattr(db, n) for n in names}
    try:
        os.environ["SUPABASE_JWT_SECRET"] = SECRET
        if auth_disabled:
            os.environ["AUTH_DISABLED"] = "1"
        else:
            os.environ.pop("AUTH_DISABLED", None)
        if tier is None:
            os.environ.pop("CANON_FORCE_TIER", None)
        else:
            os.environ["CANON_FORCE_TIER"] = tier

        db.list_worlds = lambda: [dict(WORLD)]
        db.member_role = lambda world_id, user_id: (
            ROLES.get(user_id) if world_id == WORLD["id"] else None)
        db.list_coverage_notes = lambda world_id: []
        db.list_findings = lambda world_id: [dict(f) for f in findings]
        db.world_summary = lambda world_id: SUMMARY
        db.load_bearing = lambda world_id, limit=6: []
        db.list_scenes_with_text = lambda world_id: []
        db.entity_names = lambda world_id, ids: {}
        db.ask_question = lambda world_id, question: ANSWERED
        db.rules_conn = rules_conn
        db.ops_cursor = lambda: ops_cur

        yield TestClient(app, follow_redirects=False), ops_cur
    finally:
        for k, v in saved_env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        for n, fn in saved_fns.items():
            setattr(db, n, fn)


# =============================================================================
# 1 · Metering on extraction (extract/llm.py)
# =============================================================================

def test_metering_is_a_silent_noop_without_the_env():
    create = lambda **kw: FakeResponse()  # noqa: E731
    with env(CANON_METERING_DB_URL=None):
        assert llm._metered(create) is create   # the exact callable, untouched


def test_metering_is_a_silent_noop_when_the_db_is_unreachable():
    import psycopg

    def boom(*args, **kwargs):
        raise RuntimeError("connection refused")

    create = lambda **kw: FakeResponse()  # noqa: E731
    with env(CANON_METERING_DB_URL="postgresql://nope/nope"):
        with patched(psycopg, connect=boom):
            assert llm._metered(create) is create


def test_metering_records_usage_with_a_fake_connection():
    import psycopg

    cur = OpsFakeCursor()
    conn = SimpleNamespace(cursor=lambda: cur)

    def fake_connect(dsn, autocommit=False):
        assert autocommit is True               # inserts must commit immediately
        return conn

    with env(CANON_METERING_DB_URL="postgresql://fake/fake",
             CANON_METERING_WORLD_ID="7", CANON_METERING_USER_ID=None):
        with patched(psycopg, connect=fake_connect):
            wrapped = llm._metered(lambda **kw: FakeResponse(1000, 200))
            response = wrapped(model="claude-sonnet-5", max_tokens=16000)
    assert isinstance(response, FakeResponse)   # response passes through
    (params,) = cur.inserted
    assert params[0] is None                    # user_id NULL for pipeline runs
    assert params[1] == 7                       # world attribution via env
    assert params[2] == "extraction"
    assert (params[3], params[4]) == (1000, 200)
    assert params[5] == Decimal("0.006")


# =============================================================================
# 2 · Rate limit on the ask route (reads stay unlimited)
# =============================================================================

def _ask_events(n, age_seconds=600):
    return [("ask", datetime.now(timezone.utc) - timedelta(seconds=age_seconds))
            for _ in range(n)]


def test_ask_429s_over_the_free_limit_with_retry_after():
    cur = OpsFakeCursor(events=_ask_events(20))
    with patched(app_mod, ask_tier_resolver=lambda user: "free"):
        with wired(ops_cursor=cur) as (client, _):
            r = client.post("/worlds/1/ask",
                            data={"question": "what happened to danny", "view": "report"})
    assert r.status_code == 429
    assert "20 ask per hour" in r.json()["detail"]
    assert 0 < int(r.headers["retry-after"]) <= 3600
    assert cur.inserted == []                   # a denied ask never spends quota


def test_ask_honors_paid_tier_via_a_fake_tier_resolver_and_records_on_success():
    cur = OpsFakeCursor(events=_ask_events(20))  # over free, far under paid
    with patched(app_mod, ask_tier_resolver=lambda user: "paid"):
        with wired(ops_cursor=cur) as (client, _):
            r = client.post("/worlds/1/ask",
                            data={"question": "what happened to danny", "view": "report"})
    assert r.status_code == 200
    assert "Danny is gone." in r.text           # the cited answer rendered
    assert cur.inserted == [(auth.DEV_USER.id, 1, "ask")]   # counted AFTER success


def test_ask_auth_disabled_dev_flow_resolves_tier_via_canon_force_tier():
    # No fake resolver: the real billing resolver runs, and the env override
    # is how the dev bypass user gets the paid tier (no special-casing).
    cur = OpsFakeCursor(events=_ask_events(20))
    with wired(ops_cursor=cur, tier="paid") as (client, _):
        r = client.post("/worlds/1/ask",
                        data={"question": "what happened to danny", "view": "report"})
    assert r.status_code == 200


def test_ask_requires_auth_when_enabled():
    with wired(auth_disabled=False) as (client, _):
        r = client.post("/worlds/1/ask", data={"question": "x", "view": "report"})
    assert r.status_code == 401


def test_reads_stay_unlimited_over_the_ask_quota():
    cur = OpsFakeCursor(events=_ask_events(500))
    with patched(app_mod, ask_tier_resolver=lambda user: "free"):
        with wired(ops_cursor=cur) as (client, _):
            assert client.get("/worlds/1/report").status_code == 200
            assert client.get("/worlds/1/script").status_code == 200


def test_ask_fails_open_when_the_ops_store_is_down():
    # Availability beats enforcement (docs/ops.md): a broken usage_events
    # store must never take the ask pane down.
    cur = OpsFakeCursor(fail=True)
    with patched(app_mod, ask_tier_resolver=lambda user: "free"):
        with wired(ops_cursor=cur) as (client, _):
            r = client.post("/worlds/1/ask",
                            data={"question": "what happened to danny", "view": "report"})
    assert r.status_code == 200


# =============================================================================
# 3 · Share-link routes
# =============================================================================

class ShareFakeCursor:
    """Answers exactly the SQL canon/export/share.py issues."""

    def __init__(self):
        self.links = {}   # token -> {world_id, kind, created_by, revoked}
        self.rowcount = 0
        self._result = None

    def execute(self, sql, params=None):
        s = " ".join(sql.lower().split())
        if s.startswith("insert into share_links"):
            token, world_id, kind, created_by = params
            self.links[token] = {"world_id": world_id, "kind": kind,
                                 "created_by": created_by, "revoked": False}
            self._result = None
        elif s.startswith("select world_id, kind from share_links"):
            row = self.links.get(params[0])
            self._result = ((row["world_id"], row["kind"])
                            if row and not row["revoked"] else None)
        elif s.startswith("update share_links set revoked_at"):
            row = self.links.get(params[0])
            if row and not row["revoked"]:
                row["revoked"] = True
                self.rowcount = 1
            else:
                self.rowcount = 0
        else:
            raise AssertionError(f"unexpected SQL: {s}")

    def fetchone(self):
        return self._result


@contextlib.contextmanager
def share_wired(**kwargs):
    cur = ShareFakeCursor()
    with patched(share_ui, cursor_ctx=lambda: contextlib.nullcontext(cur)):
        with wired(**kwargs) as (client, ops_cur):
            yield client, cur


def test_share_create_is_owner_gated():
    with share_wired(auth_disabled=False) as (client, cur):
        assert client.post("/worlds/1/share", data={"kind": "report"}).status_code == 401
        assert client.post("/worlds/1/share", data={"kind": "report"},
                           headers=bearer(EDITOR)).status_code == 403
        assert cur.links == {}
        r = client.post("/worlds/1/share", data={"kind": "report"},
                        headers=bearer(OWNER))
        assert r.status_code == 200
        (token,) = cur.links
        assert cur.links[token] == {"world_id": 1, "kind": "report",
                                    "created_by": OWNER, "revoked": False}
        assert f"/share/{token}" in r.text           # the URL is rendered back


def test_share_create_rejects_unknown_kinds():
    with share_wired() as (client, cur):
        r = client.post("/worlds/1/share", data={"kind": "screenplay"})
        assert r.status_code == 400
        assert cur.links == {}


def test_public_share_renders_the_report_read_only():
    with share_wired() as (client, cur):
        client.post("/worlds/1/share", data={"kind": "report"})
        (token,) = cur.links
    with share_wired(auth_disabled=False) as (client, cur2):
        cur2.links = {token: {"world_id": 1, "kind": "report",
                              "created_by": OWNER, "revoked": False}}
        r = client.get(f"/share/{token}")            # anonymous, no token, no auth
    assert r.status_code == 200
    body = r.text
    assert "Continuity findings" in body             # the real report path
    assert "dead_speaker" in body
    assert "mainnav" not in body                     # no nav
    assert 'id="ask-pane"' not in body               # no ask pane
    assert "surface.js" not in body                  # no triage keyboard/JS
    # every action renders disabled and the footer carries the viral loop
    seal_at = body.find("btn-note-seal")
    assert seal_at == -1 or "disabled" in body[seal_at:seal_at + 60]
    assert 'href="/billing"' in body
    assert "Canon never writes your story" in body


def test_public_share_serves_the_export_bible_html_with_billing_footer():
    with share_wired() as (client, cur):
        cur.links["tok-bible"] = {"world_id": 1, "kind": "bible",
                                  "created_by": OWNER, "revoked": False}
        with patched(share_ui, bible_html=lambda world_id:
                     "<html><body><h1>greyharbor_s1 — Series Bible</h1></body></html>"):
            r = client.get("/share/tok-bible")
    assert r.status_code == 200
    assert "Series Bible" in r.text
    assert 'href="/billing"' in r.text               # footer injected before </body>
    assert r.text.index("/billing") < r.text.index("</body>")


def test_revoked_and_unknown_tokens_404_identically():
    with share_wired(auth_disabled=False) as (client, cur):
        cur.links["tok-live"] = {"world_id": 1, "kind": "report",
                                 "created_by": OWNER, "revoked": False}
        # revoke is owner-gated
        assert client.post("/worlds/1/share/revoke",
                           data={"token": "tok-live"}).status_code == 401
        assert client.post("/worlds/1/share/revoke", data={"token": "tok-live"},
                           headers=bearer(EDITOR)).status_code == 403
        assert not cur.links["tok-live"]["revoked"]
        r = client.post("/worlds/1/share/revoke", data={"token": "tok-live"},
                        headers=bearer(OWNER))
        assert r.status_code == 303
        assert cur.links["tok-live"]["revoked"]
        revoked = client.get("/share/tok-live")
        unknown = client.get("/share/tok-never-existed")
    assert revoked.status_code == 404 and unknown.status_code == 404
    assert revoked.text == unknown.text              # no information leakage


def test_revoke_only_touches_links_of_the_addressed_world():
    with share_wired() as (client, cur):             # dev owner via AUTH_DISABLED
        cur.links["tok-other"] = {"world_id": 2, "kind": "report",
                                  "created_by": OWNER, "revoked": False}
        r = client.post("/worlds/1/share/revoke", data={"token": "tok-other"})
        assert r.status_code == 303                  # idempotent answer
        assert not cur.links["tok-other"]["revoked"]  # but nothing revoked


# =============================================================================
# 4 · Rules composed into the report / findings views
# =============================================================================

RULES_TABLE = [
    # rules_store column order: id, kind, params, label, created_by, created_at, disabled_at
    ("bbbbbbbb-0000-0000-0000-000000000001", "cannot",
     {"predicate": "possesses", "subject_entity_id": 5, "subject_trait": None,
      "object_entity_id": None, "severity": "warning"},
     "no key for Marcus", None, 1, None),
    ("bbbbbbbb-0000-0000-0000-000000000002", "exception",
     {"check_name": "dead_speaker", "entity_id": 6, "pos_from": None, "pos_to": None},
     None, None, 2, None),
]

RULE_VIOLATION = ("rule_cannot", "warning",
                  'Marcus possesses the key in Pilot/sc3 (pos 3), which the rule '
                  '"no key for Marcus" forbids: "mine now".', 3, 23, None)


class RulesFakeCursor:
    """Answers the SQL canon.rules issues for run_rules + apply_exceptions."""

    def __init__(self, rows=RULES_TABLE):
        self.rows = rows
        self._rows = []

    def execute(self, sql, params=None):
        s = " ".join(sql.lower().split())
        if "from world_rules" in s:
            self._rows = [tuple(r) for r in self.rows]
        elif "as check_name" in s:                       # a compiled cannot/only rule
            self._rows = [RULE_VIOLATION]
        elif "a.subject_id" in s and "from assertions" in s:
            self._rows = [(21, 6, 9), (23, 5, 3)]        # aid -> (subject, position)
        elif "from scenes where id" in s:
            self._rows = [(3, 3), (9, 9)]
        else:
            self._rows = []

    def fetchall(self):
        return self._rows

    def fetchone(self):
        return self._rows[0] if self._rows else None


class FakeConn:
    def __init__(self, cursor):
        self._cursor = cursor
        self.closed = False

    def cursor(self):
        return self._cursor

    def close(self):
        self.closed = True


class UndefinedTableError(Exception):
    sqlstate = "42P01"


def test_report_composes_rule_findings_and_applies_exceptions():
    conn = FakeConn(RulesFakeCursor())
    with wired(rules_conn=lambda: conn) as (client, _):
        body = client.get("/worlds/1/report").text
    assert "rule_cannot" in body                         # rule finding contributed
    assert "no key for Marcus" in body                   # rule name in the explanation
    assert "dead_speaker" not in body                    # EXCEPTION filtered the check
    assert conn.closed                                   # short-lived, like everything in ui/db


def test_findings_view_composes_too():
    conn = FakeConn(RulesFakeCursor())
    with wired(rules_conn=lambda: conn) as (client, _):
        body = client.get("/findings?world=greyharbor_s1").text
    assert "rule_cannot" in body
    assert "dead_speaker" not in body
    assert "/findings/None" not in body                  # rule findings carry no row id


def test_missing_world_rules_table_falls_back_to_plain_findings_silently():
    class NoTableCursor:
        def execute(self, sql, params=None):
            raise UndefinedTableError('relation "world_rules" does not exist')

        def fetchall(self):
            return []

    conn = FakeConn(NoTableCursor())
    with wired(rules_conn=lambda: conn) as (client, _):
        body = client.get("/worlds/1/report").text
    assert "dead_speaker" in body                        # plain findings, unchanged
    assert "rule_cannot" not in body


def test_no_database_at_all_falls_back_to_plain_findings():
    with wired(rules_conn=lambda: None) as (client, _):
        body = client.get("/worlds/1/report").text
    assert "dead_speaker" in body
    assert "rule_cannot" not in body


def test_list_findings_composed_normalizes_rule_findings_to_ui_shape():
    conn = FakeConn(RulesFakeCursor())
    saved = (db.list_findings, db.rules_conn)
    try:
        db.list_findings = lambda world_id: [dict(BASE_FINDING)]
        db.rules_conn = lambda: conn
        rows = db.list_findings_composed(1)
    finally:
        db.list_findings, db.rules_conn = saved
    assert [r["check_name"] for r in rows] == ["rule_cannot"]
    rule = rows[0]
    assert rule["id"] is None and rule["sealed"] is False
    assert rule["assertion_a"] == 23 and rule["scene_id"] == 3
    assert "check" not in rule                           # renamed, not duplicated


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

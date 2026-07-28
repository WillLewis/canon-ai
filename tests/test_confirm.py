"""Confirm-queue tests (P3-CONFIRM) — all offline.

FastAPI TestClient over the real app with ui.db's module-level functions faked
(the repo's fake-db pattern; see tests/test_surface.py), plus a fake connection
for the transition/count SQL itself. Covers: the queue renders drafts ordered
by confidence with quotes and script citations; confirm flips draft -> canon
and writes the usage_events audit row; reject flips draft -> rejected; ruling
on a non-draft is a no-op (guarded WHERE, no audit row); viewers get 403 on
rulings and disabled buttons on the read view; the report pill and its count
query; the celebrating empty state; the AUTH_DISABLED dev flow.

    python -m pytest -q tests/test_confirm.py
    python tests/test_confirm.py
"""

import contextlib
import os
import pathlib
import sys
import time

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

import jwt  # PyJWT — ui/requirements.txt  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from ui import auth, db  # noqa: E402
from ui.app import app  # noqa: E402

SECRET = "test-secret-0123456789abcdef0123456789abcdef"
WORLD = {"id": 1, "name": "greyharbor_s1"}
EDITOR = "11111111-1111-1111-1111-111111111111"
VIEWER = "22222222-2222-2222-2222-222222222222"
ROLES = {EDITOR: "editor", VIEWER: "viewer"}

# Apostrophe-free so the raw text survives Jinja autoescaping in assertions.
QUOTE_HIGH = "I will find Danny. That is a promise."
QUOTE_LOW = "The ledger was hers before the fire."

# Two drafts, already in queue order (confidence desc) — the shape the real
# list_draft_assertions query returns (the _ASSERTION_SELECT projection).
DRAFT_HIGH = {
    "id": 11, "predicate": "promised", "polarity": True,
    "valid_during": "[3,)", "object_value": None, "object_assertion_id": None,
    "confidence": 0.82, "status": "draft", "confirmed_by_human": False,
    "superseded_by": None, "supporting_quote": QUOTE_HIGH,
    "subject_id": 5, "subject_name": "Mara", "subject_kind": "character",
    "object_id": 6, "object_name": "Danny", "object_kind": "character",
    "scene_id": 3, "scene_slug": "INT. FISHING BOAT - NIGHT",
    "story_position": 3, "is_flashback": False,
    "episode_title": "Pilot — Greyharbor",
}
DRAFT_LOW = {
    "id": 12, "predicate": "possesses", "polarity": True,
    "valid_during": "[9,)", "object_value": None, "object_assertion_id": None,
    "confidence": 0.61, "status": "draft", "confirmed_by_human": False,
    "superseded_by": None, "supporting_quote": QUOTE_LOW,
    "subject_id": 5, "subject_name": "Mara", "subject_kind": "character",
    "object_id": 7, "object_name": "the ledger", "object_kind": "object",
    "scene_id": 9, "scene_slug": "EXT. HARBOR - DAY",
    "story_position": 9, "is_flashback": False,
    "episode_title": "Ep2 — Undertow",
}
DRAFTS = [DRAFT_HIGH, DRAFT_LOW]

# world_summary shape for the report pill (7 drafts awaiting rulings).
SUMMARY = {
    "totals": {"entities": 12, "assertions": 87, "scenes": 2, "works": 2,
               "findings_live": 0, "findings_sealed": 0},
    "entities_by_kind": [{"kind": "character", "n": 6, "provisional": 1}],
    "assertions_by_status": [{"status": "canon", "n": 80}, {"status": "draft", "n": 7}],
    "findings_by_sev": [],
    "works": [{"id": 1, "title": "Pilot — Greyharbor", "sort_order": 1, "n_scenes": 1}],
}


def make_token(sub=EDITOR, expires_in=3600):
    now = int(time.time())
    return jwt.encode({"sub": sub, "aud": auth.JWT_AUDIENCE, "email": "w@example.com",
                       "iat": now, "exp": now + expires_in}, SECRET, algorithm="HS256")


def bearer(sub):
    return {"Authorization": f"Bearer {make_token(sub=sub)}"}


# --- harness: env + fake db wiring, restored on exit -------------------------

@contextlib.contextmanager
def wired(drafts=None, auth_disabled=True, summary=None, last_pos=9):
    """TestClient over the real app with every db function the confirm queue
    and the report pill touch faked. Yields (client, calls) where calls records
    rule_assertion invocations."""
    drafts = DRAFTS if drafts is None else drafts
    summary = SUMMARY if summary is None else summary
    calls = {"rule": []}
    saved_env = {k: os.environ.get(k) for k in ("SUPABASE_JWT_SECRET", "AUTH_DISABLED")}
    names = ("list_worlds", "member_role", "list_draft_assertions", "count_drafts",
             "last_story_position", "rule_assertion",
             # report-view surface (for the pill test):
             "list_coverage_notes", "list_findings", "world_summary",
             "load_bearing", "entity_names")
    saved_fns = {n: getattr(db, n) for n in names}
    try:
        os.environ["SUPABASE_JWT_SECRET"] = SECRET
        if auth_disabled:
            os.environ["AUTH_DISABLED"] = "1"
        else:
            os.environ.pop("AUTH_DISABLED", None)

        db.list_worlds = lambda: [dict(WORLD)]
        db.member_role = lambda world_id, user_id: (
            ROLES.get(user_id) if world_id == WORLD["id"] else None)
        db.list_draft_assertions = lambda world_id: [dict(a) for a in drafts]
        db.count_drafts = lambda world_id: len(drafts)
        db.last_story_position = lambda world_id: last_pos
        db.rule_assertion = lambda world_id, assertion_id, status, ruled_by=None: (
            calls["rule"].append({"world_id": world_id, "assertion_id": assertion_id,
                                  "status": status, "ruled_by": ruled_by}) or True)
        db.list_coverage_notes = lambda world_id: []
        db.list_findings = lambda world_id: []
        db.world_summary = lambda world_id: summary
        db.load_bearing = lambda world_id, limit=6: []
        db.entity_names = lambda world_id, ids: {}

        yield TestClient(app, follow_redirects=False), calls
    finally:
        for k, v in saved_env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        for n, fn in saved_fns.items():
            setattr(db, n, fn)


# --- fake connection for the real SQL (transition guard, audit row, counts) ---

class FakeCursor:
    def __init__(self, fetchone_results=None):
        self.executed = []          # [(normalized_sql, params)]
        self._ones = list(fetchone_results or [])

    def execute(self, sql, params=None):
        self.executed.append((" ".join(sql.split()), params))

    def fetchone(self):
        return self._ones.pop(0) if self._ones else None

    def fetchall(self):
        return []


class FakeConn:
    def __init__(self, cursor):
        self._cur = cursor
        self.commits = 0

    def cursor(self):
        return self._cur

    def commit(self):
        self.commits += 1


@contextlib.contextmanager
def patched_conn(fetchone_results=None):
    """Route ui.db.db_conn at a scripted fake connection; restore on exit."""
    cur = FakeCursor(fetchone_results)
    conn = FakeConn(cur)

    @contextlib.contextmanager
    def fake_db_conn():
        yield conn

    saved = db.db_conn
    db.db_conn = fake_db_conn
    try:
        yield cur, conn
    finally:
        db.db_conn = saved


# --- queue view ---------------------------------------------------------------

def test_queue_renders_drafts_ordered_by_confidence_with_quotes():
    with wired() as (client, _):
        r = client.get("/worlds/1/confirm")
    assert r.status_code == 200
    body = r.text
    assert "Verify your canon" in body
    # "— N uncertain facts", with N and the plural in spans the undo JS updates.
    assert '<span id="queue-n">2</span> uncertain fact' in body
    assert '<span id="queue-plural">s</span>' in body
    # Both cards, most-confident first (the fake returns queue order; the SQL
    # ordering itself is asserted in the query test below).
    assert body.index('id="draft-11"') < body.index('id="draft-12"')
    assert "conf 0.82" in body and "conf 0.61" in body
    # The plain-English fact, its supporting quote, and the script-view citation.
    assert "Mara promised Danny" in body
    assert QUOTE_HIGH in body and QUOTE_LOW in body
    assert "/worlds/1/script?scene=3" in body and "#scene-3" in body
    # One-tap rulings wired to this world and assertion.
    assert 'action="/worlds/1/assertions/11/confirm"' in body
    assert 'action="/worlds/1/assertions/11/reject"' in body
    # Keyboard affordance advertised.
    assert "<kbd>c</kbd>" in body and "<kbd>u</kbd>" in body


def test_list_draft_assertions_sql_filters_drafts_and_orders_by_confidence():
    with patched_conn() as (cur, _):
        assert db.list_draft_assertions(1) == []
    sql, params = cur.executed[0]
    assert "a.status = 'draft'" in sql
    assert "order by a.confidence desc" in sql
    assert params["w"] == 1


def test_empty_queue_celebrates_and_offers_rerun_report():
    with wired(drafts=[], last_pos=12) as (client, _):
        r = client.get("/worlds/1/confirm")
    assert r.status_code == 200
    body = r.text
    assert "Nothing to verify — your canon is clean through scene 12" in body
    assert "Re-run report" in body
    assert 'href="/worlds/1/report"' in body
    assert 'id="draft-' not in body


def test_viewer_and_anonymous_see_disabled_buttons_editor_enabled():
    with wired(auth_disabled=False) as (client, _):
        viewer_body = client.get("/worlds/1/confirm", headers=bearer(VIEWER)).text
        editor_body = client.get("/worlds/1/confirm", headers=bearer(EDITOR)).text
        anon_body = client.get("/worlds/1/confirm").text  # read view stays open
    assert "btn-confirm" in viewer_body
    assert "disabled" in viewer_body.split("btn-confirm", 1)[1][:40]
    assert "disabled" in anon_body.split("btn-confirm", 1)[1][:40]
    assert "disabled" not in editor_body.split("btn-confirm", 1)[1][:40]


# --- rulings ------------------------------------------------------------------

def test_confirm_endpoint_gates_on_editor_and_flips_draft_to_canon():
    with wired(auth_disabled=False) as (client, calls):
        url = "/worlds/1/assertions/11/confirm"
        assert client.post(url).status_code == 401                    # fail closed
        assert client.post(url, headers=bearer(VIEWER)).status_code == 403
        assert calls["rule"] == []
        r = client.post(url, headers=bearer(EDITOR))
        assert r.status_code == 303
        assert r.headers["location"] == "/worlds/1/confirm"
        assert calls["rule"] == [{"world_id": 1, "assertion_id": 11,
                                  "status": "canon", "ruled_by": EDITOR}]


def test_reject_endpoint_flips_draft_to_rejected():
    with wired(auth_disabled=False) as (client, calls):
        assert client.post("/worlds/1/assertions/12/reject",
                           headers=bearer(VIEWER)).status_code == 403
        r = client.post("/worlds/1/assertions/12/reject", headers=bearer(EDITOR))
        assert r.status_code == 303
        assert calls["rule"] == [{"world_id": 1, "assertion_id": 12,
                                  "status": "rejected", "ruled_by": EDITOR}]


def test_auth_disabled_keeps_local_dev_flow_working():
    with wired(auth_disabled=True) as (client, calls):
        r = client.post("/worlds/1/assertions/11/confirm")            # no token
        assert r.status_code == 303
        assert calls["rule"][-1]["ruled_by"] == auth.DEV_USER.id


def test_confirm_transition_sql_guards_draft_and_writes_audit_row():
    with patched_conn([{"id": 11}]) as (cur, conn):      # UPDATE returned a row
        assert db.rule_assertion(1, 11, "canon", ruled_by=EDITOR) is True
    upd_sql, upd_params = cur.executed[0]
    assert "update assertions" in upd_sql
    assert "status = 'draft'" in upd_sql                 # settled rows never flip
    assert "returning id" in upd_sql
    assert upd_params["s"] == "canon" and upd_params["confirm"] is True
    assert upd_params["id"] == 11 and upd_params["w"] == 1
    # Ruling attribution on the row itself (20260703100000 migration): who + when.
    assert "confirmed_by = %(by)s::uuid" in upd_sql
    assert "confirmed_at = now()" in upd_sql
    assert upd_params["by"] == EDITOR
    # The audit row: usage_events, kind clean (no ':unpriced'), zero tokens.
    ins_sql, ins_params = cur.executed[1]
    assert ins_sql.lower().startswith("insert into usage_events")
    user_id, world_id, kind, tokens_in, tokens_out, cost = ins_params
    assert user_id == EDITOR and world_id == 1
    assert kind == "confirm_ruling"
    assert tokens_in == 0 and tokens_out == 0 and cost == 0
    assert conn.commits == 1


def test_reject_transition_writes_reject_ruling_audit_row():
    with patched_conn([{"id": 12}]) as (cur, _):
        assert db.rule_assertion(1, 12, "rejected", ruled_by=EDITOR) is True
    upd_sql, upd_params = cur.executed[0]
    assert upd_params["s"] == "rejected" and upd_params["confirm"] is False
    assert upd_params["by"] == EDITOR                    # rejects are attributed too
    assert cur.executed[1][1][2] == "reject_ruling"


def test_ruling_on_non_draft_is_a_noop_without_audit_row():
    # UPDATE ... WHERE status='draft' matched nothing: no flip, no audit row.
    with patched_conn([None]) as (cur, conn):
        assert db.rule_assertion(1, 11, "canon", ruled_by=EDITOR) is False
    assert len(cur.executed) == 1                         # the guarded UPDATE only
    assert conn.commits == 1                              # commit of nothing is fine


def test_rule_assertion_refuses_unknown_statuses():
    # Only the two ruling statuses exist; nothing can write 'draft' back, or worse.
    for bad in ("draft", "sealed", "retconned", "open"):
        try:
            db.rule_assertion(1, 11, bad)
        except ValueError:
            pass
        else:
            raise AssertionError(f"rule_assertion must refuse status={bad!r}")


# --- report pill ----------------------------------------------------------------

def test_report_header_shows_verify_pill_with_count():
    with wired() as (client, _):
        body = client.get("/worlds/1/report").text
    assert "Verify your canon (7)" in body                # from world_summary drafts
    assert 'href="/worlds/1/confirm"' in body


def test_report_pill_hidden_when_no_drafts():
    clean = dict(SUMMARY, assertions_by_status=[{"status": "canon", "n": 87}])
    with wired(summary=clean) as (client, _):
        body = client.get("/worlds/1/report").text
    assert "Verify your canon (" not in body


def test_count_drafts_sql_filters_draft_status():
    with patched_conn([{"n": 4}]) as (cur, _):
        assert db.count_drafts(1) == 4
    sql, params = cur.executed[0]
    assert "status = 'draft'" in sql and params["w"] == 1


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

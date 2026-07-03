"""Upload/theater route tests (P3-FRONTDOOR) — all offline.

TestClient over the real app with ui.jobs' wiring seams and ui.db's run
functions faked (the repo pattern from tests/test_surface.py). Covers the
refusal matrix (401 / 429 limit / 400 page cap / 400 COGS / 429 concurrency —
none of which may reach the runner), the happy path (202, record_action only
on success), poll replay + stale-heartbeat surfacing, and the editor-gated
failed-only resume.

    python -m pytest -q tests/test_jobs.py
"""

import contextlib
import os
import pathlib
import sys
import time
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

import jwt  # PyJWT — ui/requirements.txt  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from ops import metering as ops_metering  # noqa: E402
from ui import auth, db, jobs  # noqa: E402
from ui.app import app  # noqa: E402

SECRET = "test-secret-0123456789abcdef0123456789abcdef"
EDITOR = "11111111-1111-1111-1111-111111111111"
VIEWER = "22222222-2222-2222-2222-222222222222"
ROLES = {EDITOR: "editor", VIEWER: "viewer"}
NOW = datetime(2026, 7, 3, 12, 0, 0, tzinfo=timezone.utc)

FOUNTAIN = """Title: Testfall
Episode: 101

INT. DOCK - NIGHT

MARA
I promise to find him.

EXT. HARBOR - DAY

The harbor at dawn.
"""

RUN = {
    "id": "aaaaaaaa-0000-0000-0000-00000000000a", "world_id": 1,
    "user_id": EDITOR, "status": "running", "phase": "extracting",
    "scenes_total": 12, "scenes_done": 3, "facts_total": 40,
    "cost_usd": 0.42, "error": None, "candidates": [{"slug": "INT. DOCK - NIGHT"}],
    "created_at": NOW, "heartbeat_at": NOW,
}

WORLD = {"id": 1, "name": "greyharbor_s1"}


def make_token(sub=EDITOR, expires_in=3600):
    now = int(time.time())
    return jwt.encode({"sub": sub, "aud": auth.JWT_AUDIENCE, "email": "w@example.com",
                       "iat": now, "exp": now + expires_in}, SECRET, algorithm="HS256")


def bearer(sub):
    return {"Authorization": f"Bearer {make_token(sub=sub)}"}


# --- fakes -----------------------------------------------------------------------

class FakeOpsCursor:
    """usage_events counts per action for check_limit; records inserts."""

    def __init__(self, counts=None):
        self.counts = dict(counts or {})
        self.inserts = []
        self._row = (0,)

    def execute(self, sql, params=None):
        if sql.strip().upper().startswith("INSERT"):
            self.inserts.append(params)
            return
        action = params[1]
        n = self.counts.get(action, 0)
        self._row = (n, None) if len(params) == 3 else (n,)

    def fetchone(self):
        return self._row


class FakeConn:
    def __init__(self):
        self.executed = []

    def cursor(self):
        conn = self

        class Cur:
            def execute(self, sql, params=None):
                conn.executed.append((sql, params))

        return Cur()

    def commit(self):
        pass

    def close(self):
        pass


@contextlib.contextmanager
def wired(*, auth_disabled=True, counts=None, run=None, events=None,
          existing_world=None, tier="free"):
    """Patch every seam the jobs routes touch; restore on exit. Yields
    (client, calls) where calls records load_into / spawn / record inserts /
    update_run invocations."""
    calls = {"load_into": [], "spawn": [], "update_run": [], "create_run": [],
             "client_factory": 0}
    ops_cursor = FakeOpsCursor(counts)
    saved_env = {k: os.environ.get(k) for k in
                 ("SUPABASE_JWT_SECRET", "AUTH_DISABLED", "CANON_FREE_MAX_PAGES",
                  "CANON_RUN_COGS_CAP_USD", "CANON_MAX_CONCURRENT_RUNS")}
    jobs_names = ("ops_cursor_factory", "tier_resolver", "conn_factory",
                  "client_factory", "spawn", "active_runs", "canon_ingest")
    db_names = ("get_world", "get_world_by_id", "member_role", "create_run",
                "get_run", "list_run_events", "latest_run_for_world",
                "update_run", "works_with_scenes", "list_worlds")
    saved_jobs = {n: getattr(jobs, n) for n in jobs_names}
    saved_db = {n: getattr(db, n) for n in db_names}
    try:
        os.environ["SUPABASE_JWT_SECRET"] = SECRET
        if auth_disabled:
            os.environ["AUTH_DISABLED"] = "1"
        else:
            os.environ.pop("AUTH_DISABLED", None)

        jobs.ops_cursor_factory = lambda: ops_cursor
        jobs.tier_resolver = lambda user: tier
        jobs.conn_factory = lambda: FakeConn()
        def _no_client():
            calls["client_factory"] += 1
            raise AssertionError("client_factory must never be reached by a refusal")
        jobs.client_factory = _no_client
        jobs.spawn = lambda run_id, target: calls["spawn"].append(run_id)
        jobs.active_runs = lambda: 0
        jobs.canon_ingest = SimpleNamespace(
            order_works=lambda ws: ws,
            load_into=lambda conn, name, works, reset=False: (
                calls["load_into"].append({"world": name, "reset": reset,
                                           "scenes": sum(len(w.scenes) for w in works)})
                or {"world_id": 7, "works": len(works),
                    "scenes": sum(len(w.scenes) for w in works)}),
        )

        db.get_world = lambda name: (dict(existing_world)
                                     if existing_world and name == existing_world["name"]
                                     else None)
        db.get_world_by_id = lambda wid: dict(WORLD) if wid == WORLD["id"] else None
        db.member_role = lambda world_id, user_id: ROLES.get(user_id)
        db.create_run = lambda world_id, user_id, scenes_total=0: (
            calls["create_run"].append({"world_id": world_id, "user_id": user_id,
                                        "scenes_total": scenes_total}) or "run-new")
        db.get_run = lambda run_id: dict(run) if run and run_id == run["id"] else None
        db.list_run_events = lambda run_id, after=0: [
            e for e in (events or []) if e["seq"] > after]
        db.latest_run_for_world = lambda world_id: dict(run) if run else None
        db.update_run = lambda run_id, **fields: (
            calls["update_run"].append({"run_id": run_id, **fields}) or True)
        db.works_with_scenes = lambda world_id: [
            {"id": 1, "title": "E101", "source_file": "e101.fountain", "sort_order": 1,
             "scenes": [{"slug": "INT. DOCK - NIGHT", "story_position": 1,
                         "is_flashback": False, "raw_text": "MARA\nI promise."}]}]
        db.list_worlds = lambda: [dict(WORLD)]

        yield TestClient(app, follow_redirects=False), calls, ops_cursor
    finally:
        for k, v in saved_env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        for n, fn in saved_jobs.items():
            setattr(jobs, n, fn)
        for n, fn in saved_db.items():
            setattr(db, n, fn)


def post_upload(client, body=FOUNTAIN, filename="testfall.fountain", **kw):
    return client.post("/api/uploads", content=body.encode("utf-8"),
                       headers={"X-Canon-Filename": filename, **kw.pop("headers", {})},
                       **kw)


def assert_no_runner(calls, ops_cursor):
    assert calls["spawn"] == []
    assert calls["load_into"] == []
    assert calls["create_run"] == []
    assert calls["client_factory"] == 0
    assert ops_cursor.inserts == []       # denied attempts never spend quota


# --- refusal matrix (none may reach the runner) --------------------------------------

def test_anonymous_upload_is_401():
    with wired(auth_disabled=False) as (client, calls, cur):
        r = post_upload(client)
        assert r.status_code == 401
        assert_no_runner(calls, cur)


def test_rate_limited_script_ingest_is_429():
    with wired(counts={"script_ingest": 1}) as (client, calls, cur):
        r = post_upload(client)
        assert r.status_code == 429
        assert "script_ingest" in r.json()["detail"]
        assert_no_runner(calls, cur)


def test_rate_limited_world_create_is_429():
    with wired(counts={"world_create": 1}) as (client, calls, cur):
        r = post_upload(client)
        assert r.status_code == 429
        assert "world_create" in r.json()["detail"]
        assert_no_runner(calls, cur)


def test_page_cap_is_400():
    os.environ["CANON_FREE_MAX_PAGES"] = "1"
    try:
        big = FOUNTAIN + "\n".join(f"Action line {i}." for i in range(120))
        with wired() as (client, calls, cur):
            r = post_upload(client, body=big)
            assert r.status_code == 400
            assert "pages" in r.json()["detail"]
            assert_no_runner(calls, cur)
    finally:
        os.environ.pop("CANON_FREE_MAX_PAGES", None)


def test_cogs_estimate_over_cap_is_400():
    os.environ["CANON_RUN_COGS_CAP_USD"] = "0.0001"
    try:
        with wired() as (client, calls, cur):
            r = post_upload(client)
            assert r.status_code == 400
            assert "$" in r.json()["detail"]
            assert_no_runner(calls, cur)
    finally:
        os.environ.pop("CANON_RUN_COGS_CAP_USD", None)


def test_concurrency_full_is_429_with_retry_after():
    with wired() as (client, calls, cur):
        jobs.active_runs = lambda: jobs.max_concurrent_runs()
        r = post_upload(client)
        assert r.status_code == 429
        assert r.headers.get("Retry-After")
        assert_no_runner(calls, cur)


def test_unsupported_extension_and_missing_filename_are_400():
    with wired() as (client, calls, cur):
        assert post_upload(client, filename="notes.txt").status_code == 400
        r = client.post("/api/uploads", content=b"x")
        assert r.status_code == 400
        assert_no_runner(calls, cur)


# --- happy path -------------------------------------------------------------------

def test_happy_path_202_spawns_and_records_actions():
    with wired() as (client, calls, cur):
        r = post_upload(client)
        assert r.status_code == 202
        body = r.json()
        assert body == {"run_id": "run-new", "theater_url": "/worlds/7/ingest"}
        assert calls["load_into"] == [{"world": "testfall", "reset": False, "scenes": 2}]
        assert calls["create_run"] == [{"world_id": 7, "user_id": auth.DEV_USER.id,
                                        "scenes_total": 2}]
        assert calls["spawn"] == ["run-new"]
        # record_action only on success: script_ingest + world_create rows
        kinds = [p[2] for p in cur.inserts]
        assert kinds == ["script_ingest", "world_create"]


def test_reupload_into_existing_world_resets_and_skips_world_create():
    with wired(existing_world=WORLD, auth_disabled=False) as (client, calls, cur):
        r = post_upload(client, params={"world": WORLD["name"]},
                        headers=bearer(EDITOR))
        assert r.status_code == 202
        assert calls["load_into"][0]["reset"] is True
        kinds = [p[2] for p in cur.inserts]
        assert kinds == ["script_ingest"]              # no world_create
        # a viewer may not overwrite the world
        r2 = post_upload(client, params={"world": WORLD["name"]},
                         headers=bearer(VIEWER))
        assert r2.status_code == 403


# --- poll endpoint ------------------------------------------------------------------

EVENTS = [
    {"seq": 1, "kind": "phase", "data": {"phase": "extracting"}, "created_at": NOW},
    {"seq": 2, "kind": "scene", "data": {"index": 1, "total": 12}, "created_at": NOW},
    {"seq": 3, "kind": "scene", "data": {"index": 2, "total": 12}, "created_at": NOW},
]


def test_poll_replays_from_zero_then_increments():
    fresh = dict(RUN, heartbeat_at=datetime.now(timezone.utc))
    with wired(run=fresh, events=EVENTS) as (client, calls, _):
        r = client.get(f"/api/runs/{RUN['id']}/events?after=0")
        assert r.status_code == 200
        body = r.json()
        assert [e["seq"] for e in body["events"]] == [1, 2, 3]
        assert body["next"] == 3
        assert body["run"]["status"] == "running"
        r2 = client.get(f"/api/runs/{RUN['id']}/events?after=3")
        assert r2.json()["events"] == []
        assert r2.json()["next"] == 3
        assert calls["update_run"] == []               # healthy run: no writes


def test_poll_unknown_run_and_anonymous_are_404():
    fresh = dict(RUN, heartbeat_at=datetime.now(timezone.utc))
    with wired(run=fresh, events=EVENTS) as (client, _, _cur):
        assert client.get("/api/runs/ffffffff-0000-0000-0000-000000000000/events").status_code == 404
    with wired(auth_disabled=False, run=fresh, events=EVENTS) as (client, _, _cur):
        assert client.get(f"/api/runs/{RUN['id']}/events").status_code == 404


def test_stale_heartbeat_surfaces_as_failed_resumable():
    stale = dict(RUN, heartbeat_at=datetime.now(timezone.utc) - timedelta(seconds=600))
    with wired(run=stale, events=[]) as (client, calls, _):
        body = client.get(f"/api/runs/{RUN['id']}/events?after=0").json()
        assert body["run"]["status"] == "failed"
        assert body["run"]["resumable"] is True
        assert "heartbeat" in body["run"]["error"]
        assert calls["update_run"][0]["status"] == "failed"


# --- resume -----------------------------------------------------------------------

def test_resume_requires_editor_and_failed_status():
    failed = dict(RUN, status="failed", error="api down")
    with wired(auth_disabled=False, run=failed) as (client, calls, _):
        # viewer -> 403
        r = client.post(f"/api/runs/{RUN['id']}/resume", headers=bearer(VIEWER))
        assert r.status_code == 403
        assert calls["spawn"] == []
        # editor -> 202, re-spawned, run row back to running
        r2 = client.post(f"/api/runs/{RUN['id']}/resume", headers=bearer(EDITOR))
        assert r2.status_code == 202
        assert r2.json()["theater_url"] == "/worlds/1/ingest"
        assert calls["spawn"] == [RUN["id"]]
        assert calls["update_run"][0] == {"run_id": RUN["id"],
                                          "status": "running", "error": None}


def test_resume_rejects_non_failed_runs():
    for status in ("running", "done", "aborted"):
        run = dict(RUN, status=status, heartbeat_at=datetime.now(timezone.utc))
        with wired(run=run) as (client, calls, _):
            r = client.post(f"/api/runs/{RUN['id']}/resume")
            assert r.status_code == 409, status
            assert calls["spawn"] == []


def test_resume_unknown_run_is_404():
    with wired() as (client, _, _cur):
        r = client.post("/api/runs/ffffffff-0000-0000-0000-000000000000/resume")
        assert r.status_code == 404


# --- pages ------------------------------------------------------------------------

def test_upload_page_renders_trust_caption_and_limits():
    with wired() as (client, _, _cur):
        r = client.get("/upload")
        assert r.status_code == 200
        assert "never train a model" in r.text
        assert "130 pages" in r.text


def test_theater_page_recovers_latest_run():
    fresh = dict(RUN, heartbeat_at=datetime.now(timezone.utc))
    with wired(run=fresh) as (client, _, _cur):
        r = client.get("/worlds/1/ingest")
        assert r.status_code == 200
        assert RUN["id"] in r.text                    # data-run-id for theater.js
        assert "FACTS LEARNED" in r.text
    with wired(run=None) as (client, _, _cur):
        r = client.get("/worlds/1/ingest")
        assert r.status_code == 200
        assert "Upload a script" in r.text


# --- units ------------------------------------------------------------------------

def test_page_estimate_ceils_lines_over_55():
    work = SimpleNamespace(scenes=[
        SimpleNamespace(raw_text="\n".join(["line"] * 54)),   # 54 + 1 = 55
        SimpleNamespace(raw_text="one line"),                 # 1 + 1 = 2
    ])
    assert jobs.page_estimate(work) == 2                      # ceil(57/55)
    assert jobs.page_estimate(SimpleNamespace(scenes=[])) == 1


def test_estimate_script_cost_matches_pricing_table():
    est = ops_metering.estimate_script_cost(110, "claude-sonnet-5")
    assert abs(float(est) - 1.8216) < 1e-9
    assert float(ops_metering.estimate_script_cost(110, "unknown-model")) == 0.0


def test_world_name_for_slugifies_filenames():
    assert jobs.world_name_for("My Pilot (v3).fountain") == "my_pilot_v3"
    assert jobs.world_name_for("....fdx") == "script"


def test_rebuild_works_duck_types_the_scene_contract():
    from canon.extract import scene_contexts

    works = jobs.rebuild_works([
        {"title": "E101", "source_file": "e101.fountain", "sort_order": 1,
         "scenes": [{"slug": "INT. A - DAY", "story_position": 1,
                     "is_flashback": False, "raw_text": "x"},
                    {"slug": "EXT. B - DAY", "story_position": 2,
                     "is_flashback": True, "raw_text": "y"}]}])
    ctxs = scene_contexts(works)
    assert [(c.story_position, c.slug, c.is_flashback) for c in ctxs] == [
        (1, "INT. A - DAY", False), (2, "EXT. B - DAY", True)]


if __name__ == "__main__":
    import pytest

    raise SystemExit(pytest.main(["-q", __file__]))

"""Check-step tests.

Offline: the checks.sql runner plumbing (splitting, psycopg %-escaping, sealed
filtering, serialization) against a fake connection — always run.

Integration: the real db/checks.sql over a freshly-loaded greyharbor graph in
Postgres, asserting the Phase 0 findings gates. Skipped unless a database is
reachable (CANON_DB_URL, or the local docker default).

    python -m pytest -q
    python tests/test_check.py
"""

import importlib.util
import json
import os
import pathlib
import re
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

from canon import check  # noqa: E402

_spec = importlib.util.spec_from_file_location("run_eval", ROOT / "eval" / "run_eval.py")
run_eval = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(run_eval)

CHECKS_SQL = (ROOT / "db" / "checks.sql").read_text(encoding="utf-8")


# --- offline: runner plumbing ---------------------------------------------

def test_split_checks_finds_all_seven_named_checks():
    parsed = check.split_checks(CHECKS_SQL)
    names = [n for n, _ in parsed]
    assert names == [
        "dead_speaker", "presence_conflict", "premature_knowledge",
        "destroyed_location_use", "dangling_reference", "capability_violation",
        "idle_setup",
    ]


def test_every_check_honors_world_family_config():
    for name, stmt in check.split_checks(CHECKS_SQL):
        assert "world_family_config" in stmt, name
        assert f"'{name}'" in stmt


def test_to_psycopg_escapes_format_percent_and_binds_world_id():
    out = check.to_psycopg("select format('%s', e.name) where d.world_id = :world_id")
    assert "%%s" in out                      # literal % doubled for psycopg
    assert "%(world_id)s" in out
    assert ":world_id" not in out


class FakeCheckCursor:
    def __init__(self, rows_by_check, seals):
        self.rows_by_check = rows_by_check
        self.seals = seals
        self._mode = None

    def execute(self, sql, params=None):
        if "from seals" in sql.lower():
            self._mode = ("seals",)
        else:
            m = re.search(r"'([a-z_]+)'", sql)
            self._mode = ("check", m.group(1) if m else None)

    def fetchall(self):
        if self._mode == ("seals",):
            return self.seals
        if self._mode and self._mode[0] == "check":
            return self.rows_by_check.get(self._mode[1], [])
        return []


class FakeCheckConn:
    def __init__(self, rows_by_check, seals):
        self.cur = FakeCheckCursor(rows_by_check, seals)

    def cursor(self):
        return self.cur

    def rollback(self):
        pass

    def commit(self):
        pass


def test_run_checks_maps_rows_and_applies_seals():
    # row order: check, severity, explanation, scene_id, assertion_a, assertion_b
    rows = {
        "dead_speaker": [("dead_speaker", "critical", "Tobias ... died", 9, 100, None)],
        "capability_violation": [("capability_violation", "warning", "Mara ... drive", 5, 200, 201)],
    }
    seals = [("dead_speaker", 100, 0)]   # (check_name, assertion_a, coalesce_b)
    findings, errors = check.run_checks(FakeCheckConn(rows, seals), 1, CHECKS_SQL)
    assert not errors
    dead = next(f for f in findings if f["check"] == "dead_speaker")
    cap = next(f for f in findings if f["check"] == "capability_violation")
    assert dead["sealed"] is True          # matched a seal
    assert cap["sealed"] is False
    assert dead["assertion_a"] == 100 and cap["assertion_b"] == 201


def test_to_findings_json_and_report():
    findings = [
        {"check": "dead_speaker", "severity": "critical", "explanation": "x",
         "sealed": False, "scene_id": 1, "assertion_a": 2, "assertion_b": None},
        {"check": "idle_setup", "severity": "note", "explanation": "y",
         "sealed": True, "scene_id": 3, "assertion_a": 4, "assertion_b": None},
    ]
    j = check.to_findings_json(findings)["findings"]
    assert j[0]["check"] == "dead_speaker" and j[1]["sealed"] is True
    report = check.render_report(findings)
    assert "1 critical" in report and "1 sealed" in report


# --- integration: real DB --------------------------------------------------

def _db_conn():
    url = os.environ.get("CANON_DB_URL") or "postgresql://postgres:postgres@127.0.0.1:5432/postgres"
    try:
        import psycopg
        return psycopg.connect(url, connect_timeout=2)
    except Exception:
        return None


def test_integration_pipeline_passes_phase0_findings_gates():
    conn = _db_conn()
    if conn is None:
        print("  (skipped: no database reachable)")
        return

    from canon import ingest, resolve, store
    from greyharbor_golden import golden_state

    world = "greyharbor_pytest"
    try:
        files = [str(ROOT / "fixtures" / "greyharbor" / f) for f in ("ep101.fountain", "ep102.fountain")]
        wid = ingest.load_into(conn, world, ingest.parse_works(files), reset=True)["world_id"]
        store.store_state(conn, world, golden_state(), reset=True)

        findings, errors = check.run_checks(conn, wid, CHECKS_SQL)
        assert not errors, errors

        # findings gates via the real grader
        found, missed_planted, fps, traps = run_eval.score_findings(
            check.to_findings_json(findings)["findings"])
        assert set(["P1", "P2", "P3", "P4"]).issubset(set(found)), f"missed {missed_planted}"
        assert not fps, [f[1] for f in fps]
        assert not traps, traps

        # extraction recall via the real grader
        acts = resolve.to_eval_assertions(resolve.state_from_dict(golden_state()))["assertions"]
        recall, _, missed = run_eval.score_extraction(acts)
        assert recall == 1.0, f"missed {missed}"
    finally:
        try:
            conn.rollback()
        except Exception:
            pass
        cur = conn.cursor()
        cur.execute("SELECT id FROM worlds WHERE name = %s", (world,))
        r = cur.fetchone()
        if r:
            store._delete_world_graph(cur, r[0])               # FK-safe teardown
            cur.execute("DELETE FROM worlds WHERE id = %s", (r[0],))  # cascades works/scenes
            conn.commit()
        conn.close()


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

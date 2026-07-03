"""Retcon ripple tests.

The core acceptance path is DB-gated because ripple is intentionally SQL-first:
it should exercise the same ranges, scene_presence rows, findings, and family
toggle table the CLI uses.
"""

import os
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

from canon import check, families, ingest, report, ripple, store  # noqa: E402
from greyharbor_golden import golden_state  # noqa: E402

CHECKS_SQL = (ROOT / "db" / "checks.sql").read_text(encoding="utf-8")


def _db_conn():
    url = os.environ.get("CANON_DB_URL") or "postgresql://postgres:postgres@127.0.0.1:5432/postgres"
    try:
        import psycopg
        return psycopg.connect(url, connect_timeout=2)
    except Exception:
        return None


def _load_world(conn, world):
    files = [str(ROOT / "fixtures" / "greyharbor" / f) for f in ("ep101.fountain", "ep102.fountain")]
    wid = ingest.load_into(conn, world, ingest.parse_works(files), reset=True)["world_id"]
    store.store_state(conn, world, golden_state(), reset=True)
    return wid


def _cleanup(conn, world):
    try:
        conn.rollback()
    except Exception:
        pass
    cur = conn.cursor()
    cur.execute("SELECT id FROM worlds WHERE name = %s", (world,))
    row = cur.fetchone()
    if row:
        store._delete_world_graph(cur, row[0])
        cur.execute("DELETE FROM worlds WHERE id = %s", (row[0],))
        conn.commit()


def test_render_text_handles_empty_report():
    text = ripple.render_text({
        "world": "w",
        "change": {"type": "move_assertion"},
        "summary": {"assertions": 0, "scenes": 0, "check_findings": 0},
        "conflicts": {"assertions": [], "scenes": [], "check_findings": []},
    })

    assert "0 assertion" in text
    assert "Check Findings" in text


def test_integration_ripple_move_death_and_family_toggles():
    conn = _db_conn()
    if conn is None:
        print("  (skipped: no database reachable)")
        return

    world = "greyharbor_ripple_pytest"
    try:
        wid = _load_world(conn, world)
        assertion = ripple.find_assertion(conn, wid, subject="Tobias Voss", predicate="dies")

        rr = ripple.move_assertion(conn, world, assertion, 1)
        assert rr["summary"]["check_findings"] >= 1
        assert any(f["check"] == "dead_speaker" for f in rr["conflicts"]["check_findings"])
        assert any(s["story_position"] == 9 for s in rr["conflicts"]["scenes"])
        assert all(c.get("citation") for c in rr["conflicts"]["assertions"])

        findings, errors = check.run_checks(conn, wid, CHECKS_SQL)
        assert not errors
        assert any(f["check"] == "dead_speaker" for f in findings)

        families.set_enabled(conn, wid, "dead_speaker", False)
        findings_off, errors = check.run_checks(conn, wid, CHECKS_SQL)
        assert not errors
        assert not [f for f in findings_off if f["check"] == "dead_speaker"]
        rr_off = ripple.move_assertion(conn, world, assertion, 1)
        assert not [f for f in rr_off["conflicts"]["check_findings"] if f["check"] == "dead_speaker"]

        families.set_enabled(conn, wid, "dead_speaker", True)
        findings_on, errors = check.run_checks(conn, wid, CHECKS_SQL)
        assert not errors
        assert any(f["check"] == "dead_speaker" for f in findings_on)

        families.set_enabled(conn, wid, "F2", False)
        notes, logs, candidates, _snapshot = report.run_report_notes(
            conn, wid, use_llm=False, threshold=1, persist=False
        )
        _ = logs
        assert "F2" not in candidates
        assert not [n for n in notes if n["family"] == "F2"]
    finally:
        _cleanup(conn, world)
        conn.close()

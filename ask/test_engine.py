from __future__ import annotations

import json
import os
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

from ask import engine


QUESTIONS = json.loads((Path(__file__).with_name("greyharbor_questions.json")).read_text())


def test_scripted_questions_compile_to_cited_sql():
    for case in QUESTIONS["questions"]:
        plan = engine.build_plan(case["question"])
        assert plan is not None, case["question"]
        assert engine.guard_sql(plan.sql) is None
        assert ":world_id" in plan.sql
        assert "scene_id" in plan.sql.lower()


def test_epistemic_at_scene_uses_interval_membership():
    plan = engine.build_plan("what does Dani know at scene 14?")
    assert plan is not None
    assert plan.intent == "epistemic"
    assert plan.params["story_position"] == 14
    assert "a.valid_during @> :story_position" in plan.sql
    assert "a.predicate IN ('knows')" in plan.sql


def test_unsupported_question_refuses_without_sql():
    assert engine.build_plan("write a better ending for Mara") is None


class FakeCursor:
    def __init__(self, columns, rows, cite_rows):
        self.columns = columns
        self.rows = rows
        self.cite_rows = cite_rows
        self.mode = None

    def execute(self, sql, params=None):
        s = sql.lower()
        if s.startswith("set "):
            self.mode = None
        elif "row_number() over" in s:
            self.mode = "citations"
        else:
            self.mode = "main"

    @property
    def description(self):
        return [(c,) for c in self.columns]

    def fetchmany(self, n):
        return self.rows[:n]

    def fetchall(self):
        return self.cite_rows if self.mode == "citations" else []

    def fetchone(self):
        return None


class FakeConn:
    def __init__(self, columns, rows, cite_rows):
        self.cursor_obj = FakeCursor(columns, rows, cite_rows)

    def cursor(self):
        return self.cursor_obj

    def rollback(self):
        pass


def test_answer_requires_resolvable_citation():
    conn = FakeConn(
        ["subject", "predicate", "object", "supporting_quote", "scene_id"],
        [("Cole Brannigan", "knows", "ledger location", "quote", 999)],
        [],
    )
    result = engine.ask_question(conn, 1, "what does Cole know about the ledger?")
    assert result.status == "refused"
    assert "citation" in result.reason


def test_answer_renders_cited_rows():
    conn = FakeConn(
        ["subject", "predicate", "object", "from_pos", "supporting_quote", "scene_id"],
        [("Cole Brannigan", "knows", "ledger location", 7, "under the stone", 12)],
        [(12, "E102 - Thursday Numbers", "EXT. GREYHARBOR DOCKS - DAY", 7, 1)],
    )
    result = engine.ask_question(conn, 1, "what does Cole know about the ledger?")
    rendered = engine.render_answer(result)
    assert result.status == "answered"
    assert "Cole Brannigan | knows | ledger location" in rendered
    assert "[E102/sc1]" in rendered
    assert "cited: E102/sc1" in rendered


def _db_conn():
    urls = [
        os.environ.get("CANON_DB_URL"),
        os.environ.get("DATABASE_URL"),
        "postgresql://postgres:postgres@127.0.0.1:54322/postgres",
        "postgresql://postgres:postgres@127.0.0.1:5432/postgres",
    ]
    try:
        import psycopg
    except Exception:
        return None
    for url in [u for u in urls if u]:
        try:
            return psycopg.connect(url, connect_timeout=2)
        except Exception:
            continue
    return None


def test_integration_greyharbor_scripted_questions_db_gated():
    conn = _db_conn()
    if conn is None:
        print("  (skipped: no database reachable)")
        return

    from canon import ingest, store
    from greyharbor_golden import golden_state

    world = "greyharbor_ask_package_pytest"
    try:
        files = [str(ROOT / f) for f in QUESTIONS["fixture_files"]]
        loaded = ingest.load_into(conn, world, ingest.parse_works(files), reset=True)
        store.store_state(conn, world, golden_state(), reset=True)

        passed = 0
        for case in QUESTIONS["questions"]:
            result = engine.ask_question(conn, loaded["world_id"], case["question"])
            rendered = engine.render_answer(result)
            assert result.status == "answered", (case["id"], result.status, result.reason)
            for text in case["answer_contains"]:
                assert text in rendered, (case["id"], text, rendered)
            for citation in case["citations"]:
                assert any(citation in c for c in result.citations), (case["id"], result.citations)
            passed += 1
        assert passed >= 8

        for case in QUESTIONS["unsupported"]:
            result = engine.ask_question(conn, loaded["world_id"], case["question"])
            assert result.status == case["expected_status"]
            assert "Cannot answer from cited canon" in engine.render_answer(result)
    finally:
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
        conn.close()

"""Ask-the-Bible tests.

Offline: SQL guard, scene-column detection, citation labeling, refusal paths,
and the retry loop — fake client + fake connection; always run.

Integration (DB-gated): the 10 scripted Phase 0 questions (SPEC R5's "8/10 with
correct citations" shape) against a freshly loaded greyharbor graph. Reference
SQL stands in for the LLM (no API key in CI), so this verifies the harness +
schema can answer the whole set with correct citations; LLM text-to-SQL quality
is measured separately once a key is present.

    python -m pytest -q
    python tests/test_ask.py
"""

import json
import os
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

from canon import ask  # noqa: E402


# --- offline: guard ---------------------------------------------------------

def test_guard_accepts_select_and_with():
    for sql in (
        "SELECT 1 FROM assertions a WHERE a.world_id = :world_id",
        "WITH k AS (SELECT 1 FROM assertions WHERE world_id = :world_id) SELECT * FROM k",
        "  select e.name from entities e where e.world_id = :world_id ;",
    ):
        clean, err = ask.guard_sql(sql)
        assert err is None, (sql, err)
        assert not clean.endswith(";")


def test_guard_rejects_writes_multistatement_and_unscoped():
    cases = {
        "UPDATE assertions SET status='canon' WHERE world_id = :world_id": "read-only",
        "SELECT 1 FROM x WHERE world_id = :world_id; DROP TABLE worlds": "multiple statements",
        "DELETE FROM worlds WHERE id = :world_id": "read-only",
        "EXPLAIN SELECT 1 FROM a WHERE world_id = :world_id": "must be a single read-only SELECT",
        "SELECT * FROM assertions": ":world_id",
        "": "empty",
    }
    for sql, want in cases.items():
        _, err = ask.guard_sql(sql)
        assert err is not None and want in err, (sql, err)


def test_scene_col_index():
    assert ask.scene_col_index(["subject", "scene_id"]) == 1
    assert ask.scene_col_index(["a", "ESTABLISHED_IN_SCENE"]) == 1
    assert ask.scene_col_index(["a", "b"]) is None


# --- offline: fake conn/client end-to-end -----------------------------------

class FakeAskCursor:
    """Scripted cursor: main query rows + citation-lookup rows."""

    def __init__(self, columns, rows, cite_rows):
        self.columns, self.rows, self.cite_rows = columns, rows, cite_rows
        self.executed = []
        self._mode = None

    def execute(self, sql, params=None):
        self.executed.append((sql, params))
        s = sql.lower().strip()
        if s.startswith("set "):
            self._mode = None
        elif "row_number() over" in s:
            self._mode = "cite"
        else:
            self._mode = "main"

    @property
    def description(self):
        return [(c,) for c in self.columns]

    def fetchmany(self, n):
        return self.rows[:n]

    def fetchall(self):
        return self.cite_rows if self._mode == "cite" else []


class FakeAskConn:
    def __init__(self, columns, rows, cite_rows):
        self.cur = FakeAskCursor(columns, rows, cite_rows)

    def cursor(self):
        return self.cur

    def rollback(self):
        pass


class FakeAskClient:
    """Returns scripted SQL payloads, one per messages.create call."""

    def __init__(self, sqls):
        self.sqls = list(sqls)
        self.calls = []
        self.messages = self

    def create(self, **kwargs):
        self.calls.append(kwargs)

        class _B:
            type = "text"
            text = json.dumps({"sql": self.sqls.pop(0), "notes": "n"})

        class _R:
            content = [_B()]
            stop_reason = "end_turn"

        return _R()


GOOD_SQL = ("SELECT e.name AS subject, a.predicate, a.object_value, a.supporting_quote, "
            "a.established_in_scene AS scene_id FROM assertions a "
            "JOIN entities e ON e.id = a.subject_id WHERE a.world_id = :world_id")

_COLS = ["subject", "predicate", "object_value", "supporting_quote", "scene_id"]
_ROWS = [("Cole Brannigan", "knows", "ledger location", "under the chapel floor stone", 7)]
_CITES = [(7, "E102 — Thursday Numbers", "EXT. GREYHARBOR DOCKS - DAY", 7, 1)]


def test_ask_answers_with_citation_via_fakes():
    conn = FakeAskConn(_COLS, _ROWS, _CITES)
    client = FakeAskClient([GOOD_SQL])
    r = ask.ask(conn, client, "what does Cole know?", 1, do_narrate=False)
    assert r["status"] == "answered"
    assert r["citations"] == ["E102/sc1 · EXT. GREYHARBOR DOCKS - DAY (pos 7)"]
    assert "[E102/sc1]" in r["row_lines"][0]
    assert '"under the chapel floor stone"' in r["row_lines"][0]
    rendered = ask.render_answer(r, "q")
    assert "cited: E102/sc1" in rendered


def test_ask_retries_once_then_succeeds():
    bad = "UPDATE assertions SET status='canon' WHERE world_id = :world_id"
    conn = FakeAskConn(_COLS, _ROWS, _CITES)
    client = FakeAskClient([bad, GOOD_SQL])
    r = ask.ask(conn, client, "q", 1, do_narrate=False)
    assert r["status"] == "answered"
    assert len(client.calls) == 2
    # corrective feedback was passed into the retry prompt
    assert "REJECTED" in client.calls[1]["messages"][0]["content"]
    assert len(r["attempts"]) == 1 and "read-only" in r["attempts"][0]["error"]


def test_ask_refuses_when_no_citation_column():
    no_scene = "SELECT e.name FROM entities e WHERE e.world_id = :world_id"
    conn = FakeAskConn(["name"], [("Cole",)], [])
    client = FakeAskClient([no_scene, no_scene])
    r = ask.ask(conn, client, "q", 1, do_narrate=False)
    assert r["status"] == "refused"
    assert "scene_id" in r["reason"]
    assert "REFUSED" in ask.render_answer(r, "q")


def test_ask_no_support_on_zero_rows_without_retry():
    conn = FakeAskConn(_COLS, [], [])
    client = FakeAskClient([GOOD_SQL])
    r = ask.ask(conn, client, "q", 1, do_narrate=False)
    assert r["status"] == "no_support"
    assert len(client.calls) == 1          # an empty result is an answer, not an error
    assert "No cited answer" in ask.render_answer(r, "q")


def test_sql_override_skips_llm():
    conn = FakeAskConn(_COLS, _ROWS, _CITES)
    r = ask.ask(conn, None, "q", 1, sql_override=GOOD_SQL, do_narrate=False)
    assert r["status"] == "answered" and r["notes"] == "(provided via --sql)"


def test_uncoercible_scene_id_refuses_instead_of_crashing():
    # a query that aliases something odd (e.g. void/'') as scene_id is unciteable
    conn = FakeAskConn(["x", "scene_id"], [("v", "")], [])
    r = ask.ask(conn, None, "q", 1, sql_override=GOOD_SQL, do_narrate=False)
    assert r["status"] == "refused"
    assert "no resolvable scene citations" in r["reason"]


# --- integration: the 10 scripted Phase 0 questions --------------------------

_ALIAS = ("IN (SELECT e.id FROM entities e LEFT JOIN aliases al ON al.entity_id = e.id "
          "WHERE e.world_id = :world_id AND (e.name ILIKE {p} OR al.alias ILIKE {p}))")


def _subject(pattern):
    return ("a.subject_id " + _ALIAS).format(p=f"'%{pattern}%'")


def _know_sql(extra):
    return ("SELECT e.name AS subject, a.predicate, a.object_value, "
            "lower(a.valid_during) AS from_pos, a.supporting_quote, "
            "a.established_in_scene AS scene_id "
            "FROM assertions a JOIN entities e ON e.id = a.subject_id "
            "WHERE a.world_id = :world_id AND a.status NOT IN ('rejected','retconned') "
            + extra + " ORDER BY from_pos")


QUESTIONS = [
    ("what does Cole know about the ledger, and when?",
     _know_sql(f"AND a.predicate = 'knows' AND {_subject('cole')} AND a.object_value ILIKE '%ledger%'"),
     "E102/sc1", "ledger location"),
    ("when does Tobias die?",
     _know_sql(f"AND a.predicate = 'dies' AND {_subject('tobias')}"),
     "E101/sc5", "dies"),
    ("where is the leather ledger hidden?",
     "SELECT e.name AS subject, a.predicate, o.name AS location, a.supporting_quote, "
     "a.established_in_scene AS scene_id "
     "FROM assertions a JOIN entities e ON e.id = a.subject_id "
     "JOIN entities o ON o.id = a.object_id "
     "WHERE a.world_id = :world_id AND a.predicate = 'located_at' "
     f"AND {_subject('ledger')} AND a.status NOT IN ('rejected','retconned')",
     "E101/sc3", "Chapel on the Point"),
    ("what can't Mara do?",
     _know_sql(f"AND a.predicate = 'cannot' AND {_subject('mara')}"),
     "E101/sc1", "drive"),
    ("who knows the ledger's location, in the order they learned it?",
     _know_sql("AND a.predicate = 'knows' AND a.object_value ILIKE '%ledger location%'"),
     "E101/sc3", "Tobias Voss"),
    ("what does Edda believe about the ledger?",
     _know_sql(f"AND a.predicate = 'believes' AND {_subject('edda')}"),
     "E101/sc2", "real ledger not in office"),
    ("what open promises does Mara have?",
     _know_sql(f"AND a.predicate = 'promised' AND upper_inf(a.valid_during) AND {_subject('mara')}"),
     "E101/sc4", "find Danny"),
    ("what is Cole's job?",
     _know_sql(f"AND a.predicate = 'occupation' AND {_subject('cole')}"),
     "E102/sc1", "deputy"),
    ("what happened to the chapel?",
     _know_sql(f"AND a.predicate = 'destroyed' AND {_subject('chapel')}"),
     "E101/sc6", "burns to its stones"),
    ("where does Tobias appear after his death?",
     "SELECT e.name AS who, s.slug, s.story_position, s.id AS scene_id "
     "FROM scene_presence sp JOIN entities e ON e.id = sp.entity_id "
     "JOIN scenes s ON s.id = sp.scene_id JOIN works w ON w.id = s.work_id "
     "WHERE w.world_id = :world_id AND e.name ILIKE '%tobias%' "
     "AND s.story_position > (SELECT lower(d.valid_during) FROM assertions d "
     "WHERE d.world_id = :world_id AND d.predicate = 'dies' AND d.subject_id = e.id LIMIT 1)",
     "E102/sc3", "Tobias Voss"),
]


def _db_conn():
    url = os.environ.get("CANON_DB_URL") or "postgresql://postgres:postgres@127.0.0.1:5432/postgres"
    try:
        import psycopg
        return psycopg.connect(url, connect_timeout=2)
    except Exception:
        return None


def test_integration_scripted_questions_all_cited():
    conn = _db_conn()
    if conn is None:
        print("  (skipped: no database reachable)")
        return

    from canon import ingest, store
    from greyharbor_golden import golden_state

    world = "greyharbor_ask_pytest"
    try:
        files = [str(ROOT / "fixtures" / "greyharbor" / f) for f in ("ep101.fountain", "ep102.fountain")]
        wid = ingest.load_into(conn, world, ingest.parse_works(files), reset=True)["world_id"]
        store.store_state(conn, world, golden_state(), reset=True)

        passed = []
        for question, ref_sql, want_cite, want_content in QUESTIONS:
            r = ask.ask(conn, None, question, wid, sql_override=ref_sql, do_narrate=False)
            assert r["status"] == "answered", (question, r["status"], r["reason"])
            rendered = ask.render_answer(r, question)
            assert any(want_cite in c for c in r["citations"]), (question, r["citations"])
            assert want_content in rendered, (question, rendered)
            passed.append(question)
        assert len(passed) == 10           # harness target; LLM AC is >= 8/10 with a key

        # refusal path live: query with no scene_id column is rejected by the harness
        r = ask.ask(conn, None, "uncited", wid, do_narrate=False,
                    sql_override="SELECT e.name FROM entities e WHERE e.world_id = :world_id")
        assert r["status"] == "refused"
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

"""Hole-finder tests.

Offline: SQL splitting, text/HTML rendering, category grouping/ordering — always
run. Integration: db/holes.sql over a freshly-loaded greyharbor graph, asserting
the expected gap categories. Skipped unless a database is reachable.

    python -m pytest -q
    python tests/test_holes.py
"""

import os
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

from canon import holes  # noqa: E402

HOLES_SQL = (ROOT / "db" / "holes.sql").read_text(encoding="utf-8")


# --- offline ---------------------------------------------------------------

def test_split_holes_returns_four_selects():
    stmts = holes.split_holes(HOLES_SQL)
    assert len(stmts) == 4
    assert all(s.lower().startswith("select") for s in stmts)
    assert all("--" not in s.splitlines()[0] for s in stmts)  # comments stripped


def _sample():
    return [
        {"category": "unconfirmed_belief", "question": "X believes \"y\"?",
         "detail": "d4", "scene_id": 9, "cite": "E1/sc9", "cite_full": "E1/sc9 · S (pos 9)"},
        {"category": "unknown_entity", "question": "Who is Danny?",
         "detail": "d1", "scene_id": 4, "cite": "E1/sc4", "cite_full": "E1/sc4 · S (pos 4)"},
        {"category": "unsourced_knowledge", "question": "How does Cole know?",
         "detail": "d3", "scene_id": 7, "cite": "E1/sc7", "cite_full": "E1/sc7 · S (pos 7)"},
    ]


def test_group_by_category_uses_canonical_order_and_drops_empty():
    groups = holes.group_by_category(_sample())
    keys = [g[0] for g in groups]
    # canonical order regardless of input order; open_thread absent (no items)
    assert keys == ["unknown_entity", "unsourced_knowledge", "unconfirmed_belief"]


def test_render_text_groups_and_cites():
    out = holes.render_text("greyharbor", _sample())
    assert "3 question(s)" in out
    assert "Unestablished references (1)" in out
    assert "[E1/sc4]" in out
    # high-signal category precedes beliefs
    assert out.index("Unestablished references") < out.index("Unconfirmed beliefs")


def test_render_html_is_self_contained_and_escapes():
    nasty = [{"category": "unknown_entity", "question": 'Who is <b>"Bo"</b> & co?',
              "detail": "d", "scene_id": 1, "cite": "E1/sc1", "cite_full": "x"}]
    page = holes.render_html("greyharbor", nasty, generated_at="2026-06-13")
    assert page.startswith("<!doctype html>") and page.rstrip().endswith("</html>")
    assert "<style>" in page                       # inline CSS, no external deps
    assert "&lt;b&gt;" in page and "&amp; co" in page  # HTML-escaped
    assert "<b>" not in page.split("<style>")[1].split("</style>")[1]  # no raw injection in body
    assert "Canon never writes your story" in page  # trust line present


def test_render_html_empty_state():
    page = holes.render_html("greyharbor", [])
    assert "No gaps found" in page


# --- integration -----------------------------------------------------------

def _db_conn():
    url = os.environ.get("CANON_DB_URL") or "postgresql://postgres:postgres@127.0.0.1:5432/postgres"
    try:
        import psycopg
        return psycopg.connect(url, connect_timeout=2)
    except Exception:
        return None


def test_integration_holes_over_greyharbor():
    conn = _db_conn()
    if conn is None:
        print("  (skipped: no database reachable)")
        return

    from canon import ingest, store
    from greyharbor_golden import golden_state

    world = "greyharbor_holes_pytest"
    try:
        files = [str(ROOT / "fixtures" / "greyharbor" / f) for f in ("ep101.fountain", "ep102.fountain")]
        wid = ingest.load_into(conn, world, ingest.parse_works(files), reset=True)["world_id"]
        store.store_state(conn, world, golden_state(), reset=True)

        found, errors = holes.run_holes(conn, wid, HOLES_SQL)
        assert not errors, errors
        cats = {h["category"] for h in found}

        # the golden graph has an unestablished Danny, an open "find Danny" thread,
        # and Cole's unsourced ledger knowledge
        assert "unknown_entity" in cats
        assert "open_thread" in cats
        assert "unsourced_knowledge" in cats
        assert all(h.get("cite") for h in found)        # every hole cites a scene
        # the unsourced-knowledge hole is about Cole and the ledger
        usk = next(h for h in found if h["category"] == "unsourced_knowledge")
        assert "Cole" in usk["question"] and "ledger" in usk["question"].lower()
    finally:
        try:
            conn.rollback()
        except Exception:
            pass
        cur = conn.cursor()
        cur.execute("SELECT id FROM worlds WHERE name = %s", (world,))
        r = cur.fetchone()
        if r:
            store._delete_world_graph(cur, r[0])
            cur.execute("DELETE FROM worlds WHERE id = %s", (r[0],))
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

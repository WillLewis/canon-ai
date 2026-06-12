"""Ingestion tests — global story positions + DB loader against a fake connection.

Runnable two ways:
    python -m pytest -q
    python tests/test_ingest.py
"""

import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from canon import ingest  # noqa: E402

FIXTURES = pathlib.Path(__file__).resolve().parents[1] / "fixtures" / "greyharbor"
FILES = [str(FIXTURES / "ep101.fountain"), str(FIXTURES / "ep102.fountain")]


class FakeCursor:
    def __init__(self, fetch_queue):
        self.fetch_queue = list(fetch_queue)
        self.calls = []  # (sql, params)

    def execute(self, sql, params=None):
        self.calls.append((sql, params))

    def fetchone(self):
        return self.fetch_queue.pop(0)


class FakeConn:
    def __init__(self, fetch_queue):
        self.cur = FakeCursor(fetch_queue)
        self.committed = False
        self.closed = False

    def cursor(self):
        return self.cur

    def commit(self):
        self.committed = True

    def close(self):
        self.closed = True


def test_works_ordered_by_episode_even_if_reversed_input():
    works = ingest.parse_works(list(reversed(FILES)))  # ep102 first on input
    assert [w.episode for w in works] == [101, 102]
    assert [w.sort_order for w in works] == [101, 102]


def test_global_story_positions_span_works_in_reading_order():
    works = ingest.parse_works(FILES)
    plan = ingest.assign_story_positions(works, start=1)
    positions = [pos for _, rows in plan for _, pos in rows]
    assert positions == list(range(1, 12))           # 6 + 5 = 11, contiguous
    # ep101 occupies 1..6, ep102 occupies 7..11.
    assert [pos for _, pos in plan[0][1]] == [1, 2, 3, 4, 5, 6]
    assert [pos for _, pos in plan[1][1]] == [7, 8, 9, 10, 11]


def test_load_into_fresh_world_inserts_expected_rows():
    works = ingest.parse_works(FILES)
    # SELECT world id -> None; INSERT world -> 1; count works -> 0;
    # INSERT ep101 work -> 10; INSERT ep102 work -> 11.
    conn = FakeConn([None, (1,), (0,), (10,), (11,)])
    result = ingest.load_into(conn, "greyharbor", works, reset=False)

    assert result == {"world_id": 1, "works": 2, "scenes": 11}
    assert conn.committed is True

    calls = conn.cur.calls
    world_inserts = [c for c in calls if c[0].startswith("INSERT INTO worlds")]
    work_inserts = [c for c in calls if c[0].startswith("INSERT INTO works")]
    scene_inserts = [c for c in calls if c[0].startswith("INSERT INTO scenes")]
    assert len(world_inserts) == 1
    assert len(work_inserts) == 2
    assert len(scene_inserts) == 11

    # Story positions written are the global 1..11 sequence, with the right FKs.
    positions = [c[1][2] for c in scene_inserts]
    assert positions == list(range(1, 12))
    work_ids = [c[1][0] for c in scene_inserts]
    assert work_ids == [10] * 6 + [11] * 5
    # No flashbacks in the fixtures.
    assert all(c[1][3] is False for c in scene_inserts)


def test_reset_world_deletes_first():
    works = ingest.parse_works(FILES)
    conn = FakeConn([None, (1,), (0,), (10,), (11,)])
    ingest.load_into(conn, "greyharbor", works, reset=True)
    assert conn.cur.calls[0][0].startswith("DELETE FROM worlds")


def test_duplicate_ingest_without_reset_is_refused():
    works = ingest.parse_works(FILES)
    # Existing world id 5, already has 2 works -> must refuse.
    conn = FakeConn([(5,), (2,)])
    try:
        ingest.load_into(conn, "greyharbor", works, reset=False)
    except RuntimeError as e:
        assert "reset-world" in str(e)
    else:
        raise AssertionError("expected RuntimeError on duplicate ingest")


def test_resolve_db_url_precedence(monkeypatch=None):
    import os

    saved = {k: os.environ.get(k) for k in ingest.DB_URL_ENV_VARS}
    try:
        for k in ingest.DB_URL_ENV_VARS:
            os.environ.pop(k, None)
        assert ingest.resolve_db_url(None) is None
        assert ingest.resolve_db_url("explicit://x") == "explicit://x"
        os.environ["DATABASE_URL"] = "env://y"
        assert ingest.resolve_db_url(None) == "env://y"
        os.environ["CANON_DB_URL"] = "env://x"
        assert ingest.resolve_db_url(None) == "env://x"  # CANON_DB_URL wins
    finally:
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


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

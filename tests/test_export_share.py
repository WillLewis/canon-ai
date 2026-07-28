"""Share-link tests — fake cursor over an in-memory share_links table.

Round trip: create -> resolve -> revoke -> resolve(None); unknown tokens and
double revocation are exercised too. No live Postgres (fake-connection
pattern, as in tests/test_store.py).
"""

import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import pytest  # noqa: E402

from canon.export import share  # noqa: E402


class FakeShareCursor:
    """Implements exactly the three statements share.py issues."""

    def __init__(self):
        self.rows = {}  # token -> {world_id, kind, created_by, revoked_at}
        self.calls = []
        self.rowcount = 0
        self._result = None

    def execute(self, sql, params=None):
        self.calls.append((sql, params))
        s = " ".join(sql.split()).lower()
        if s.startswith("insert into share_links"):
            token, world_id, kind, created_by = params
            if token in self.rows:
                raise RuntimeError("unique violation: share_links.token")
            self.rows[token] = {"world_id": world_id, "kind": kind,
                                "created_by": created_by, "revoked_at": None}
            self.rowcount = 1
            self._result = None
        elif s.startswith("select world_id, kind from share_links"):
            row = self.rows.get(params[0])
            live = row is not None and row["revoked_at"] is None
            self._result = (row["world_id"], row["kind"]) if live else None
        elif s.startswith("update share_links set revoked_at"):
            row = self.rows.get(params[0])
            if row is not None and row["revoked_at"] is None:
                row["revoked_at"] = "now"
                self.rowcount = 1
            else:
                self.rowcount = 0
            self._result = None
        else:
            raise AssertionError(f"unexpected SQL: {sql}")

    def fetchone(self):
        return self._result


def test_create_resolve_revoke_round_trip():
    cur = FakeShareCursor()
    link = share.create_share_link(cur, 7, "bible", created_by="user-uuid")

    assert link["world_id"] == 7 and link["kind"] == "bible"
    assert len(link["token"]) == 43  # secrets.token_urlsafe(32)

    resolved = share.resolve_share_link(cur, link["token"])
    assert resolved == {"world_id": 7, "kind": "bible"}

    assert share.revoke_share_link(cur, link["token"]) is True
    assert share.resolve_share_link(cur, link["token"]) is None


def test_revoking_twice_is_false_and_unknown_token_resolves_none():
    cur = FakeShareCursor()
    link = share.create_share_link(cur, 3, "report")

    assert share.resolve_share_link(cur, "no-such-token") is None
    assert share.revoke_share_link(cur, "no-such-token") is False

    assert share.revoke_share_link(cur, link["token"]) is True
    assert share.revoke_share_link(cur, link["token"]) is False


def test_tokens_are_unique_and_urlsafe():
    cur = FakeShareCursor()
    tokens = {share.create_share_link(cur, 1, "report")["token"] for _ in range(20)}
    assert len(tokens) == 20
    for t in tokens:
        assert all(c.isalnum() or c in "-_" for c in t)


def test_invalid_kind_rejected_before_any_sql():
    cur = FakeShareCursor()
    with pytest.raises(ValueError):
        share.create_share_link(cur, 1, "screenplay")
    assert cur.calls == []


def test_created_by_defaults_to_null():
    cur = FakeShareCursor()
    link = share.create_share_link(cur, 9, "bible")
    assert cur.rows[link["token"]]["created_by"] is None
    # revoked_at starts null = live
    assert cur.rows[link["token"]]["revoked_at"] is None

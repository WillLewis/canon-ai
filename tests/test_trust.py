"""Trust mechanics tests (P3-TRUST) — all offline.

Fake-cursor tests for the full export (zip membership, valid JSON/JSONL,
manifest, world_rules feature-detect) and hard deletion (children-first SQL
order, per-table counts, co-owned survival, billing feature-detect); a
schema-completeness cross-check so a future migration that forgets to
register a table in the export/delete registries fails here; and TestClient
tests over ui/trust_ui.py routes (role gating, typed confirmations, terms
page, export offered before deletion).

    python -m pytest -q tests/test_trust.py
    python tests/test_trust.py
"""

import contextlib
import io
import json
import os
import pathlib
import re
import sys
import time
import zipfile

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import jwt  # PyJWT — ui/requirements.txt  # noqa: E402
from fastapi import FastAPI  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from canon import trust_delete  # noqa: E402
from canon.export import full_export  # noqa: E402
from ui import auth, db, trust_ui  # noqa: E402

SECRET = "test-secret-0123456789abcdef0123456789abcdef"
WORLD = {"id": 1, "name": "greyharbor_s1"}
OWNER = "33333333-3333-3333-3333-333333333333"
EDITOR = "11111111-1111-1111-1111-111111111111"
VIEWER = "22222222-2222-2222-2222-222222222222"
STRANGER = "44444444-4444-4444-4444-444444444444"
ROLES = {OWNER: "owner", EDITOR: "editor", VIEWER: "viewer"}
EMAIL = "writer@example.com"


# ---------------------------------------------------------------------------
# Fake cursors
# ---------------------------------------------------------------------------

class FakeExportCursor:
    """Answers the export module's SQL from an in-memory {table: (cols, rows)}
    map. Rows are tuples + a .description, like a plain psycopg cursor."""

    def __init__(self, tables):
        self.tables = tables
        self.executed = []
        self._rows = []
        self.description = None
        self.rowcount = 0

    def execute(self, sql, params=None):
        s = " ".join(sql.split()).lower()
        self.executed.append((s, params))
        if "information_schema.tables" in s:
            name = params[0]
            self._rows = [(1,)] if name in self.tables else []
            self.description = [("exists",)]
            return
        m = re.search(r"\bfrom\s+([a-z_]+)", s)
        assert m, f"unexpected SQL: {sql}"
        cols, rows = self.tables.get(m.group(1), ((), []))
        self.description = [(c,) for c in cols]
        self._rows = list(rows)
        self.rowcount = len(self._rows)

    def fetchall(self):
        return list(self._rows)

    def fetchone(self):
        return self._rows[0] if self._rows else None


class FakeDeleteCursor:
    """Records every DELETE in order; feature-detect and owned-worlds queries
    are answered from constructor arguments."""

    def __init__(self, counts=None, existing=(), owned=(), billing=()):
        self.counts = counts or {}
        self.existing = set(existing)
        self.owned = list(owned)
        self.billing = list(billing)
        self.deleted = []          # table names, in execution order
        self.delete_params = []
        self.executed = []
        self._rows = []
        self.rowcount = 0

    def execute(self, sql, params=None):
        s = " ".join(sql.split()).lower()
        self.executed.append((s, params))
        if "information_schema.columns" in s:   # billing detect (joins .tables too)
            self._rows = [(t,) for t in self.billing]
        elif "information_schema.tables" in s:
            self._rows = [(1,)] if params[0] in self.existing else []
        elif "owner_id" in s and s.startswith("select"):
            self._rows = [(wid,) for wid in self.owned]
        elif s.startswith("delete from"):
            table = re.match(r"delete from ([a-z_]+)", s).group(1)
            self.deleted.append(table)
            self.delete_params.append(params)
            self.rowcount = self.counts.get(table, 1)
            self._rows = []
        else:
            raise AssertionError(f"unexpected SQL: {sql}")

    def fetchall(self):
        return list(self._rows)

    def fetchone(self):
        return self._rows[0] if self._rows else None


WORLD_TABLE_DATA = {
    "worlds": (("id", "name", "created_at", "owner_id"),
               [(1, "greyharbor_s1", "2026-07-01T00:00:00+00:00", OWNER)]),
    "works": (("id", "world_id", "title", "source_file", "sort_order"),
              [(1, 1, "S1E01", None, 1)]),
    "scenes": (("id", "work_id", "slug", "story_position", "is_flashback", "raw_text"),
               [(3, 1, "INT. BOATHOUSE — NIGHT", 3, False, "MARA\nI'll find Danny.")]),
    "entities": (("id", "world_id", "kind", "name", "dossier", "provisional"),
                 [(5, 1, "character", "Mara", None, False)]),
    "aliases": (("entity_id", "alias", "kind"), [(5, "M.", "nickname")]),
    "assertions": (
        ("id", "world_id", "subject_id", "predicate", "object_value", "polarity",
         "valid_during", "established_in_scene", "supporting_quote", "confidence",
         "status", "cited_scene_slug", "cited_story_position", "cited_work_title"),
        [(11, 1, 5, "promised", "find Danny", True, "[3,)", 3,
          "I'll find Danny.", 0.7, "draft", "INT. BOATHOUSE — NIGHT", 3, "S1E01"),
         (12, 1, 5, "trait", "stubborn", True, "[3,)", 3,
          "", 0.99, "rejected", "INT. BOATHOUSE — NIGHT", 3, "S1E01")],
    ),
    "character_locations": (("assertion_id", "character_id", "location_id", "valid_during"), []),
    "scene_presence": (("scene_id", "entity_id"), [(3, 5)]),
    "findings": (("id", "world_id", "check_name", "severity", "explanation",
                  "scene_id", "assertion_a", "assertion_b", "sealed"),
                 [(7, 1, "dead_speaks", "critical", "Danny speaks after dying.",
                   3, 11, None, False)]),
    "seals": (("world_id", "check_name", "assertion_a", "assertion_b", "reason",
               "created_at", "ruled_by", "ruled_at"),
              [(1, "location_conflict", 11, None, "intentional",
                "2026-07-02", EDITOR, "2026-07-02")]),
    "coverage_notes": (("id", "world_id", "note_key", "family", "summary", "body",
                        "evidence", "salience", "status", "status_changed_by"),
                       [("uuid-1", 1, "f2:k", "F2", "s", "b", "{}", 10, "open", None)]),
    "share_links": (("id", "token", "world_id", "kind", "created_by", "created_at",
                     "revoked_at"),
                    [("uuid-2", "tok", 1, "report", OWNER, "2026-07-02", None)]),
    "world_members": (("world_id", "user_id", "role", "invited_by", "created_at"),
                      [(1, EDITOR, "editor", OWNER, "2026-07-01")]),
}

ACCOUNT_TABLE_DATA = dict(WORLD_TABLE_DATA, **{
    "profiles": (("user_id", "display_name", "created_at"),
                 [(OWNER, "The Writer", "2026-06-30")]),
    "usage_events": (("id", "user_id", "world_id", "kind", "tokens_in", "tokens_out",
                      "cost_usd", "created_at"),
                     [(1, OWNER, 1, "extraction", 100, 50, "0.0012", "2026-07-01")]),
})


@contextlib.contextmanager
def stub_renderers():
    """The renderers have their own tests (test_export_bible/render); here they
    are stubbed so the zip tests need no bible/report SQL faked."""
    saved = (full_export.render_bible_md, full_export.render_report_md)
    full_export.render_bible_md = lambda cur, wid: "# bible stub\n"
    full_export.render_report_md = lambda cur, wid, name: f"# report stub — {name}\n"
    try:
        yield
    finally:
        full_export.render_bible_md, full_export.render_report_md = saved


# ---------------------------------------------------------------------------
# Full export
# ---------------------------------------------------------------------------

def test_export_world_zip_has_every_member_with_valid_json():
    cur = FakeExportCursor(WORLD_TABLE_DATA)
    with stub_renderers():
        data = full_export.export_world(cur, 1)

    zf = zipfile.ZipFile(io.BytesIO(data))
    expected = {f"{t}.jsonl" for t in full_export.WORLD_TABLES}
    expected |= {"manifest.json", "bible.md", "report.md"}
    assert set(zf.namelist()) == expected  # world_rules absent: table not present

    manifest = json.loads(zf.read("manifest.json"))
    assert manifest["format_version"] == full_export.FORMAT_VERSION
    assert manifest["kind"] == "world_export"
    assert manifest["world"]["name"] == "greyharbor_s1"
    assert manifest["exported_at"]

    for table in full_export.WORLD_TABLES:
        lines = zf.read(f"{table}.jsonl").decode("utf-8").splitlines()
        rows = [json.loads(line) for line in lines]  # every line is valid JSON
        assert len(rows) == manifest["counts"][table] == len(WORLD_TABLE_DATA[table][1])

    # Scene text, all assertion statuses, citations, and ruling attribution export.
    scenes = [json.loads(x) for x in zf.read("scenes.jsonl").decode().splitlines()]
    assert scenes[0]["raw_text"].startswith("MARA")
    assertions = [json.loads(x) for x in zf.read("assertions.jsonl").decode().splitlines()]
    assert {a["status"] for a in assertions} == {"draft", "rejected"}
    assert assertions[0]["cited_scene_slug"] == "INT. BOATHOUSE — NIGHT"
    seals = [json.loads(x) for x in zf.read("seals.jsonl").decode().splitlines()]
    assert seals[0]["ruled_by"] == EDITOR  # the writer's judgments are theirs

    assert zf.read("bible.md").decode() == "# bible stub\n"
    assert "report stub" in zf.read("report.md").decode()


def test_export_world_feature_detects_world_rules():
    tables = dict(WORLD_TABLE_DATA)
    tables["world_rules"] = (("id", "world_id", "rule"), [(1, 1, "no ghosts")])
    cur = FakeExportCursor(tables)
    with stub_renderers():
        data = full_export.export_world(cur, 1)
    zf = zipfile.ZipFile(io.BytesIO(data))
    assert "world_rules.jsonl" in zf.namelist()
    manifest = json.loads(zf.read("manifest.json"))
    assert manifest["counts"]["world_rules"] == 1


def test_export_world_unknown_world_raises():
    cur = FakeExportCursor({"worlds": (("id", "name"), [])})
    try:
        with stub_renderers():
            full_export.export_world(cur, 99)
    except ValueError:
        pass
    else:
        raise AssertionError("export of a missing world must raise, not fabricate")


def test_export_account_contains_profile_memberships_usage_and_world_zips():
    cur = FakeExportCursor(ACCOUNT_TABLE_DATA)
    with stub_renderers():
        data = full_export.export_account(cur, OWNER)
    zf = zipfile.ZipFile(io.BytesIO(data))
    names = set(zf.namelist())
    assert {"manifest.json", "profiles.jsonl", "world_members.jsonl",
            "usage_events.jsonl"} <= names

    manifest = json.loads(zf.read("manifest.json"))
    assert manifest["kind"] == "account_export"
    assert manifest["user_id"] == OWNER
    assert len(manifest["owned_worlds"]) == 1
    member = manifest["owned_worlds"][0]["member"]
    assert member in names and member.startswith("worlds/")

    inner = zipfile.ZipFile(io.BytesIO(zf.read(member)))  # a full world export
    assert "manifest.json" in inner.namelist()
    assert "scenes.jsonl" in inner.namelist()


# ---------------------------------------------------------------------------
# Schema completeness — the cross-check that makes the registries a contract.
# A migration adding a table without registering it for export AND delete
# fails here, loudly, before it can leave a hole in someone's exit.
# ---------------------------------------------------------------------------

def _created_tables() -> set[str]:
    texts = [p.read_text(encoding="utf-8")
             for p in sorted((ROOT / "supabase" / "migrations").glob("*.sql"))]
    texts.append((ROOT / "db" / "schema.sql").read_text(encoding="utf-8"))
    tables: set[str] = set()
    for text in texts:
        tables |= set(re.findall(
            r"create table (?:if not exists )?([a-z_][a-z0-9_]*)", text, re.I))
    return tables


def test_every_schema_table_is_registered_for_export():
    created = _created_tables()
    assert created, "no CREATE TABLE found — the schema scan is broken"
    covered = (set(full_export.WORLD_TABLES)
               | set(full_export.OPTIONAL_WORLD_TABLES)
               | set(full_export.ACCOUNT_TABLES)
               | set(full_export.EXPORT_EXEMPT))
    missing = created - covered
    assert not missing, (
        f"tables missing from the full-export registries: {sorted(missing)} — "
        "add them to canon/export/full_export.py (WORLD_TABLES / ACCOUNT_TABLES "
        "or, with a written reason, EXPORT_EXEMPT)")


def test_every_schema_table_is_registered_for_deletion():
    created = _created_tables()
    covered = ({t for t, _ in trust_delete.WORLD_DELETE_ORDER}
               | {t for t, _ in trust_delete.OPTIONAL_WORLD_DELETES}
               | {"profiles", "usage_events", "world_members"})  # delete_account
    missing = created - covered
    assert not missing, (
        f"tables missing from the deletion order: {sorted(missing)} — "
        "add them to canon/trust_delete.py so 'delete everything' stays true")


# ---------------------------------------------------------------------------
# Deletion
# ---------------------------------------------------------------------------

def test_delete_world_children_first_with_counts():
    counts = {"assertions": 42, "scenes": 9, "entities": 6, "worlds": 1,
              "aliases": 4, "seals": 2, "findings": 3}
    cur = FakeDeleteCursor(counts=counts)
    result = trust_delete.delete_world(cur, 7)

    # Exact declared order, world_rules skipped (table absent), worlds last.
    assert cur.deleted == [t for t, _ in trust_delete.WORLD_DELETE_ORDER]
    assert cur.deleted[-1] == "worlds"
    for child, parent in [
        ("character_locations", "assertions"), ("seals", "assertions"),
        ("findings", "assertions"), ("findings", "scenes"),
        ("scene_presence", "scenes"), ("scene_presence", "entities"),
        ("assertions", "entities"), ("assertions", "scenes"),
        ("aliases", "entities"), ("entities", "worlds"),
        ("scenes", "works"), ("works", "worlds"), ("world_members", "worlds"),
    ]:
        assert cur.deleted.index(child) < cur.deleted.index(parent), \
            f"{child} must be deleted before {parent}"

    assert all(p == {"w": 7} for p in cur.delete_params)  # everything world-scoped
    assert result["assertions"] == 42 and result["worlds"] == 1
    assert set(result) == {t for t, _ in trust_delete.WORLD_DELETE_ORDER}


def test_delete_world_includes_world_rules_when_the_table_exists():
    cur = FakeDeleteCursor(existing={"world_rules"})
    result = trust_delete.delete_world(cur, 7)
    assert cur.deleted[0] == "world_rules"  # may cite assertions: goes first
    assert "world_rules" in result


def test_delete_account_removes_solely_owned_world_and_account_rows():
    cur = FakeDeleteCursor(counts={"assertions": 5, "worlds": 1, "profiles": 1,
                                   "usage_events": 3, "world_members": 2}, owned=[7])
    result = trust_delete.delete_account(cur, OWNER)
    assert result["worlds_deleted"] == 1
    assert "worlds" in cur.deleted and result["assertions"] == 5
    # Account rows go after world content; profile is last of all.
    assert cur.deleted[-1] == "profiles"
    assert cur.deleted.index("usage_events") > cur.deleted.index("worlds")
    assert result["profiles"] == 1 and result["usage_events"] == 3


def test_co_owned_world_survives_account_deletion_minus_the_membership():
    cur = FakeDeleteCursor(counts={"world_members": 1, "usage_events": 3,
                                   "profiles": 1}, owned=[])
    result = trust_delete.delete_account(cur, VIEWER)
    assert "worlds" not in cur.deleted            # the co-owned world survives
    assert "assertions" not in cur.deleted        # none of its content is touched
    assert cur.deleted == ["world_members", "usage_events", "profiles"]
    assert result["worlds_deleted"] == 0
    assert result["world_members"] == 1           # only the membership goes


def test_delete_account_feature_detects_billing_tables():
    cur = FakeDeleteCursor(counts={"billing_customers": 1},
                           owned=[], billing=["billing_customers"])
    result = trust_delete.delete_account(cur, OWNER)
    assert "billing_customers" in cur.deleted
    assert cur.deleted.index("billing_customers") < cur.deleted.index("profiles")
    assert result["billing_customers"] == 1


# ---------------------------------------------------------------------------
# Routes — TestClient over the router (ui/app.py wiring is the orchestrator's;
# tests mount the router on a bare app, the repo's offline pattern otherwise).
# ---------------------------------------------------------------------------

class RecordingCursor:
    def __init__(self):
        self.executed = []

    def execute(self, sql, params=None):
        self.executed.append((" ".join(sql.split()).lower(), params))

    def fetchall(self):
        return []

    def fetchone(self):
        return None


class FakeConn:
    def __init__(self):
        self.cur = RecordingCursor()
        self.commits = 0
        self.closed = False

    def cursor(self):
        return self.cur

    def commit(self):
        self.commits += 1

    def close(self):
        self.closed = True


def make_token(sub, email=EMAIL, expires_in=3600):
    now = int(time.time())
    return jwt.encode({"sub": sub, "aud": auth.JWT_AUDIENCE, "email": email,
                       "iat": now, "exp": now + expires_in}, SECRET, algorithm="HS256")


def bearer(sub, **kw):
    return {"Authorization": f"Bearer {make_token(sub, **kw)}"}


@contextlib.contextmanager
def wired(roles=ROLES):
    """Router on a bare app; ui.db and trust_ui's seams faked; env restored."""
    calls = {"export_world": [], "delete_world": [],
             "export_account": [], "delete_account": []}
    conn = FakeConn()
    saved_env = {k: os.environ.get(k) for k in ("SUPABASE_JWT_SECRET", "AUTH_DISABLED")}
    saved_db = (db.list_worlds, db.member_role, db.get_world_by_id)
    saved_ui = (trust_ui._open_conn, trust_ui.export_world, trust_ui.delete_world,
                trust_ui.export_account, trust_ui.delete_account)
    try:
        os.environ["SUPABASE_JWT_SECRET"] = SECRET
        os.environ.pop("AUTH_DISABLED", None)

        db.list_worlds = lambda: [dict(WORLD)]
        db.member_role = lambda world_id, user_id: (
            roles.get(user_id) if world_id == WORLD["id"] else None)
        db.get_world_by_id = lambda world_id: (
            dict(WORLD) if world_id == WORLD["id"] else None)

        trust_ui._open_conn = lambda: conn
        trust_ui.export_world = lambda cur, wid: (
            calls["export_world"].append(wid) or b"ZIP-WORLD")
        trust_ui.delete_world = lambda cur, wid: (
            calls["delete_world"].append(wid) or {"assertions": 42, "scenes": 9,
                                                  "worlds": 1})
        trust_ui.export_account = lambda cur, uid: (
            calls["export_account"].append(uid) or b"ZIP-ACCOUNT")
        trust_ui.delete_account = lambda cur, uid: (
            calls["delete_account"].append(uid) or {"worlds_deleted": 1,
                                                    "usage_events": 3, "profiles": 1})

        test_app = FastAPI()
        test_app.include_router(trust_ui.router)
        yield TestClient(test_app, follow_redirects=False), calls, conn
    finally:
        for k, v in saved_env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        db.list_worlds, db.member_role, db.get_world_by_id = saved_db
        (trust_ui._open_conn, trust_ui.export_world, trust_ui.delete_world,
         trust_ui.export_account, trust_ui.delete_account) = saved_ui


def _delete_sql(conn):
    return [s for s, _ in conn.cur.executed if s.startswith("delete")]


def _audit_kinds(conn):
    return [p[2] for s, p in conn.cur.executed if "usage_events" in s and p]


# --- world export -------------------------------------------------------------

def test_world_export_streams_zip_for_any_member_and_audits():
    with wired() as (client, calls, conn):
        r = client.get("/worlds/1/export.zip", headers=bearer(VIEWER))
    assert r.status_code == 200
    assert r.headers["content-type"] == "application/zip"
    assert "greyharbor_s1-export.zip" in r.headers["content-disposition"]
    assert r.content == b"ZIP-WORLD"
    assert calls["export_world"] == [1]
    assert _audit_kinds(conn) == ["world_export"]
    assert conn.commits == 1


def test_world_export_requires_membership():
    with wired() as (client, calls, _conn):
        assert client.get("/worlds/1/export.zip").status_code == 401
        assert client.get("/worlds/1/export.zip",
                          headers=bearer(STRANGER)).status_code == 403
        assert calls["export_world"] == []


# --- world delete ---------------------------------------------------------------

def test_world_delete_is_owner_only():
    with wired() as (client, calls, conn):
        r = client.post("/worlds/1/delete", headers=bearer(EDITOR),
                        data={"confirm": WORLD["name"]})
        assert r.status_code == 403                      # editor cannot delete
        r = client.get("/worlds/1/delete", headers=bearer(EDITOR))
        assert r.status_code == 403                      # nor see the delete page
        assert calls["delete_world"] == []
        assert _delete_sql(conn) == []


def test_world_delete_confirmation_mismatch_is_400_and_deletes_nothing():
    with wired() as (client, calls, conn):
        r = client.post("/worlds/1/delete", headers=bearer(OWNER),
                        data={"confirm": "greyharbor_s2"})
    assert r.status_code == 400
    assert "Nothing was deleted" in r.text
    assert calls["delete_world"] == []
    assert _delete_sql(conn) == []                       # zero delete SQL executed
    assert conn.commits == 0


def test_world_delete_with_exact_name_renders_counts_and_audits():
    with wired() as (client, calls, conn):
        r = client.post("/worlds/1/delete", headers=bearer(OWNER),
                        data={"confirm": WORLD["name"]})
    assert r.status_code == 200
    assert calls["delete_world"] == [1]
    body = r.text
    assert "assertions" in body and "42" in body         # per-table deleted counts
    assert "worlds" in body
    assert _audit_kinds(conn) == ["world_delete"]
    assert conn.commits == 1


def test_world_delete_confirm_page_offers_export_download_first():
    with wired() as (client, _calls, _conn):
        r = client.get("/worlds/1/delete", headers=bearer(OWNER))
    assert r.status_code == 200
    body = r.text
    assert "Download everything" in body
    # The export download comes before the typed-confirmation field. (The form
    # action can't anchor this: base.html's world picker shares the path.)
    assert body.index("/worlds/1/export.zip") < body.index('name="confirm"'), \
        "the export download must be offered before the delete form"


# --- account export / delete ------------------------------------------------------

def test_account_export_and_delete_confirm_page():
    with wired() as (client, calls, conn):
        r = client.get("/account/export.zip", headers=bearer(OWNER))
        assert r.status_code == 200 and r.content == b"ZIP-ACCOUNT"
        assert calls["export_account"] == [OWNER]
        assert _audit_kinds(conn) == ["account_export"]

        page = client.get("/account/delete", headers=bearer(OWNER))
    assert page.status_code == 200
    body = page.text
    assert body.index("/account/export.zip") < body.index('name="confirm"')


def test_account_delete_confirmation_is_the_exact_email():
    with wired() as (client, calls, conn):
        r = client.post("/account/delete", headers=bearer(OWNER),
                        data={"confirm": "not-my-email@example.com"})
        assert r.status_code == 400
        assert calls["delete_account"] == []
        assert _delete_sql(conn) == []

        r = client.post("/account/delete", headers=bearer(OWNER),
                        data={"confirm": EMAIL})
    assert r.status_code == 200
    assert calls["delete_account"] == [OWNER]
    assert "worlds_deleted" in r.text and "profiles" in r.text
    assert _audit_kinds(conn) == ["account_delete"]


def test_account_routes_require_a_signed_in_user():
    with wired() as (client, calls, _conn):
        assert client.get("/account/export.zip").status_code == 401
        assert client.post("/account/delete",
                           data={"confirm": EMAIL}).status_code == 401
        assert calls["export_account"] == [] and calls["delete_account"] == []


# --- terms -----------------------------------------------------------------------

def test_terms_page_renders_all_four_sections_marked_draft():
    with wired() as (client, _calls, _conn):
        r = client.get("/legal/terms")                   # public, no auth
    assert r.status_code == 200
    body = r.text
    for section in ("your-ip", "no-training", "deletion-guarantee",
                    "no-literary-material"):
        assert f'id="{section}"' in body, f"terms section missing: {section}"
    assert body.count("DRAFT — FOR LEGAL REVIEW") >= 4   # each section is marked
    assert "no-training configuration" in body
    assert "no literary material" in body


if __name__ == "__main__":
    import traceback

    fns = [v for k, v in sorted(globals().items())
           if k.startswith("test_") and callable(v)]
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

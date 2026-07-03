"""Rule builder tests (P3-RULES) — all offline.

Engine: construction validates against the closed predicate vocabulary
(ValueError, never a warning), each kind compiles to parameterized SQL in the
db/checks.sql finding shape and finds its violation on seeded fake rows,
sealed things never flag, EXCEPTION suppresses exactly its target, disabled
rules don't run, CRUD round-trips through a fake world_rules table.

UI: FastAPI TestClient over ui.rules_ui.router with its data functions faked
(tests/test_surface.py pattern) — select-only builder, editor gating,
AUTH_DISABLED dev flow.

Plus the grep-level guarantee that no LLM machinery leaks into the rules
paths (same as tests/test_export_cli.py for canon/export).

    python -m pytest -q tests/test_rules.py
    python tests/test_rules.py
"""

import contextlib
import json
import os
import pathlib
import re
import sys
import time

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import jwt  # PyJWT — ui/requirements.txt  # noqa: E402
import pytest  # noqa: E402
from fastapi import FastAPI  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from canon import cli, rules, rules_store  # noqa: E402
from ui import auth, db, rules_ui  # noqa: E402

SECRET = "test-secret-0123456789abcdef0123456789abcdef"
WORLD = {"id": 1, "name": "greyharbor_s1"}
EDITOR = "11111111-1111-1111-1111-111111111111"
VIEWER = "22222222-2222-2222-2222-222222222222"
ROLES = {EDITOR: "editor", VIEWER: "viewer"}


# --- fakes -------------------------------------------------------------------

class FakeCursor:
    """Cursor whose behavior is a handler(sql, params) -> rows function."""

    def __init__(self, handler):
        self.handler = handler
        self.executed = []          # every (sql, params) pair, in order
        self._rows = []

    def execute(self, sql, params=None):
        self.executed.append((sql, params or {}))
        self._rows = list(self.handler(sql, params or {}) or [])

    def fetchall(self):
        return list(self._rows)

    def fetchone(self):
        return self._rows[0] if self._rows else None


def store_row(rid, kind, params, label=None, disabled_at=None):
    """A world_rules row in rules_store._COLUMNS order (params as jsonb text)."""
    return (rid, kind, json.dumps(params), label, None, 1, disabled_at)


def rules_handler(store_rows, violations=None):
    """Serve world_rules selects (honoring the enabled-only filter) and rule
    violation queries (keyed by the emitted check_name param)."""
    violations = violations or {}

    def handler(sql, params):
        if "from world_rules" in sql:
            rows = list(store_rows)
            if "disabled_at is null" in sql:
                rows = [r for r in rows if r[6] is None]
            return rows
        if "from assertions a" in sql and "check_name" in params:
            return violations.get(params["check_name"], [])
        return []

    return handler


# --- construction: the closed vocabulary is law --------------------------------

def test_construction_rejects_unknown_predicates():
    with pytest.raises(ValueError):
        rules.CannotRule(predicate="flies", subject_entity_id=5)
    with pytest.raises(ValueError):
        rules.OnlyRule(entity_id=3, predicate="teleports")
    with pytest.raises(ValueError):  # same law through the params path the UI uses
        rules.from_params("cannot", {"predicate": "flies", "subject_entity_id": 5})
    # and a known predicate constructs fine
    assert rules.CannotRule(predicate="possesses", subject_entity_id=5).severity == "warning"


def test_construction_rejects_everything_ambiguous():
    # subject scope: exactly one of entity / trait
    with pytest.raises(ValueError):
        rules.CannotRule(predicate="possesses")                       # neither
    with pytest.raises(ValueError):
        rules.CannotRule(predicate="possesses", subject_entity_id=5,
                         subject_trait="ghost")                       # both
    # intransitive predicates take no object
    with pytest.raises(ValueError):
        rules.CannotRule(predicate="dies", subject_entity_id=5, object_entity_id=7)
    with pytest.raises(ValueError):
        rules.OnlyRule(entity_id=3, predicate="destroyed", object_entity_id=7)
    # severity is an enum
    with pytest.raises(ValueError):
        rules.CannotRule(predicate="possesses", subject_entity_id=5, severity="fatal")
    # exceptions: unknown check family, inverted range, non-integer ids
    with pytest.raises(ValueError):
        rules.ExceptionRule(check_name="vibe_check", entity_id=5)
    with pytest.raises(ValueError):
        rules.ExceptionRule(check_name="dead_speaker", entity_id=5,
                            pos_from=9, pos_to=3)
    with pytest.raises(ValueError):
        rules.from_params("exception", {"check_name": "dead_speaker",
                                        "entity_id": "Marcus"})
    # unknown kind
    with pytest.raises(ValueError):
        rules.from_params("always", {"predicate": "possesses"})


def test_check_families_come_from_checks_sql_plus_rule_families():
    families = rules.check_families()
    assert "dead_speaker" in families and "presence_conflict" in families
    assert "rule_cannot" in families and "rule_only" in families


# --- compile + run: each kind finds its violation -------------------------------

def test_cannot_rule_compiles_parameterized_and_finds_its_violation():
    row = store_row("r-1", "cannot", {"predicate": "possesses",
                                      "subject_entity_id": 5, "object_entity_id": 7})
    violation = ("rule_cannot", "warning",
                 'Marcus possesses Lighthouse Key in INT. VAULT (pos 9), which the '
                 'rule "entity #5 cannot possesses entity #7" forbids: "He pockets the key.".',
                 9, 100, None)
    cur = FakeCursor(rules_handler([row], {"rule_cannot": [violation]}))
    findings = rules.run_rules(cur, 1)

    assert len(findings) == 1
    f = findings[0]
    # exact db/checks.sql finding shape, plus the runner's sealed flag
    assert f == {"check": "rule_cannot", "severity": "warning",
                 "explanation": violation[2], "scene_id": 9,
                 "assertion_a": 100, "assertion_b": None, "sealed": False}
    # the violation query is fully parameterized — values travel as params
    sql, params = cur.executed[-1]
    assert params["world_id"] == 1
    assert params["predicate"] == "possesses"
    assert params["subject_entity_id"] == 5
    assert params["object_entity_id"] == 7
    assert "%(predicate)s" in sql and "%(subject_entity_id)s" in sql
    assert "a.polarity" in sql
    assert "possesses" not in sql          # never interpolated into the SQL text


def test_cannot_rule_trait_scope_matches_via_trait_assertions():
    sql, params = rules.CannotRule(predicate="located_at",
                                   subject_trait="ghost").compile()
    assert params["subject_trait"] == "ghost"
    assert "ghost" not in sql              # parameterized, not interpolated
    assert "t.predicate = 'trait'" in sql
    assert "t.object_value = %(subject_trait)s" in sql
    assert "%(subject_entity_id)s" not in sql


def test_only_rule_flags_everyone_but_the_holder():
    row = store_row("r-2", "only", {"entity_id": 3, "predicate": "knows"},
                    label="only Mara knows the ledger")
    violation = ("rule_only", "warning",
                 'Danny knows "the ledger" in EXT. HARBOR (pos 9), but the rule '
                 '"only Mara knows the ledger" allows only Mara: "no quote".',
                 9, 101, None)
    cur = FakeCursor(rules_handler([row], {"rule_only": [violation]}))
    findings = rules.run_rules(cur, 1)
    assert [f["check"] for f in findings] == ["rule_only"]
    assert findings[0]["assertion_a"] == 101

    sql, params = cur.executed[-1]
    assert params["entity_id"] == 3
    assert "a.subject_id <> %(entity_id)s" in sql
    assert params["rule_name"] == "only Mara knows the ledger"   # names the rule


def test_severity_defaults_to_warning_and_is_overridable():
    _, params = rules.CannotRule(predicate="possesses", subject_entity_id=5).compile()
    assert params["severity"] == "warning"
    _, params = rules.OnlyRule(entity_id=3, predicate="knows",
                               severity="critical").compile()
    assert params["severity"] == "critical"


def test_sealed_items_never_flag_in_either_kind():
    for rule in (rules.CannotRule(predicate="possesses", subject_entity_id=5),
                 rules.OnlyRule(entity_id=3, predicate="knows")):
        sql, params = rule.compile()
        # the violating assertion's own writer-ruled statuses are excluded ...
        assert "a.status not in ('sealed', 'rejected', 'retconned')" in sql
        # ... and a seal on the emitted finding suppresses it at the source
        assert "from seals" in sql
        assert "se.check_name = %(check_name)s" in sql
        assert "se.assertion_a = a.id" in sql
        assert params["check_name"] == rule.check_name


# --- exceptions: suppress exactly the target, nothing else ----------------------

def lookup_handler(subjects, scene_positions):
    """subjects: assertion_id -> (subject_id, establishing position)."""

    def handler(sql, params):
        if "from assertions a" in sql:
            return [(aid, s, p) for aid, (s, p) in subjects.items()
                    if aid in params["ids"]]
        if "from scenes" in sql:
            return [(sid, pos) for sid, pos in scene_positions.items()
                    if sid in params["ids"]]
        return []

    return handler


F_DEAD_MARCUS = {"check": "dead_speaker", "severity": "critical",
                 "explanation": "Marcus speaks after death.", "scene_id": 9,
                 "assertion_a": 100, "assertion_b": None, "sealed": False}
F_DEAD_DANNY = {"check": "dead_speaker", "severity": "critical",
                "explanation": "Danny speaks after death.", "scene_id": 9,
                "assertion_a": 101, "assertion_b": None, "sealed": False}
F_PRESENCE_MARCUS = {"check": "presence_conflict", "severity": "critical",
                     "explanation": "Marcus in two places.", "scene_id": 9,
                     "assertion_a": 100, "assertion_b": None, "sealed": False}
SUBJECTS = {100: (5, 3), 101: (6, 3)}       # assertion -> (subject, est. pos)
SCENES = {9: 9, 20: 20}


def test_exception_suppresses_only_its_entity_and_family():
    exc = rules.ExceptionRule(check_name="dead_speaker", entity_id=5)
    cur = FakeCursor(lookup_handler(SUBJECTS, SCENES))
    kept = rules.apply_exceptions(cur, 1,
                                  [F_DEAD_MARCUS, F_DEAD_DANNY, F_PRESENCE_MARCUS],
                                  exceptions=[exc])
    # Marcus's dead_speaker is gone; Danny's (other entity) and Marcus's
    # presence_conflict (other family) both survive.
    assert kept == [F_DEAD_DANNY, F_PRESENCE_MARCUS]


def test_exception_range_is_surgical():
    ranged = rules.ExceptionRule(check_name="dead_speaker", entity_id=5,
                                 pos_from=5, pos_to=15)
    late = dict(F_DEAD_MARCUS, scene_id=20)
    cur = FakeCursor(lookup_handler(SUBJECTS, SCENES))
    kept = rules.apply_exceptions(cur, 1, [F_DEAD_MARCUS, late], exceptions=[ranged])
    assert kept == [late]                   # pos 9 suppressed, pos 20 kept

    # Unknown position: a ranged exception never widens itself by guessing —
    # the finding stays; a rangeless one covers the entity everywhere.
    unknown = dict(F_DEAD_MARCUS, scene_id=None, assertion_a=100)
    cur = FakeCursor(lookup_handler({100: (5, None)}, {}))
    assert rules.apply_exceptions(cur, 1, [unknown], exceptions=[ranged]) == [unknown]
    everywhere = rules.ExceptionRule(check_name="dead_speaker", entity_id=5)
    cur = FakeCursor(lookup_handler({100: (5, None)}, {}))
    assert rules.apply_exceptions(cur, 1, [unknown], exceptions=[everywhere]) == []


def test_exception_accepts_ui_shaped_findings_with_check_name_key():
    exc = rules.ExceptionRule(check_name="dead_speaker", entity_id=5)
    ui_row = {"check_name": "dead_speaker", "severity": "critical",
              "explanation": "x", "scene_id": 9, "assertion_a": 100,
              "assertion_b": None, "sealed": False}
    cur = FakeCursor(lookup_handler(SUBJECTS, SCENES))
    assert rules.apply_exceptions(cur, 1, [ui_row], exceptions=[exc]) == []


def test_compose_findings_merges_checks_and_rules_then_filters():
    exc_row = store_row("r-e", "exception",
                        {"check_name": "dead_speaker", "entity_id": 5})
    cannot_row = store_row("r-c", "cannot",
                           {"predicate": "possesses", "subject_entity_id": 6})
    violation = ("rule_cannot", "warning", "Danny possesses ...", 9, 101, None)

    def handler(sql, params):
        if "from world_rules" in sql:
            return [exc_row, cannot_row]
        if "from assertions a" in sql and "check_name" in params:
            return [violation] if params["check_name"] == "rule_cannot" else []
        return lookup_handler(SUBJECTS, SCENES)(sql, params)

    cur = FakeCursor(handler)
    out = rules.compose_findings(cur, 1, [F_DEAD_MARCUS, F_DEAD_DANNY])
    # Marcus's dead_speaker suppressed by the exception; Danny's check finding
    # and Danny's rule finding both flow through in one checks-shaped list.
    assert [(f["check"], f["assertion_a"]) for f in out] == [
        ("dead_speaker", 101), ("rule_cannot", 101)]


def test_disabled_rule_does_not_run():
    live = store_row("r-on", "cannot", {"predicate": "possesses",
                                        "subject_entity_id": 5})
    dead = store_row("r-off", "only", {"entity_id": 3, "predicate": "knows"},
                     disabled_at="2026-07-03")
    cur = FakeCursor(rules_handler([live, dead], {}))
    rules.run_rules(cur, 1)
    ran = [p.get("check_name") for _, p in cur.executed if "check_name" in p]
    assert ran == ["rule_cannot"]           # the disabled only-rule never ran


def test_exception_rules_never_query():
    exc = store_row("r-e", "exception", {"check_name": "dead_speaker", "entity_id": 5})
    cur = FakeCursor(rules_handler([exc], {}))
    assert rules.run_rules(cur, 1) == []
    assert all("from assertions a" not in sql for sql, _ in cur.executed)


# --- storage: CRUD round-trip over a fake world_rules table ---------------------

class FakeRulesDB:
    """Just enough of the world_rules table for rules_store's exact SQL."""

    def __init__(self):
        self.rows = {}
        self.serial = 0

    def handler(self, sql, params):
        s = " ".join(sql.lower().split())
        if s.startswith("insert into world_rules"):
            self.serial += 1
            rid = f"00000000-0000-0000-0000-{self.serial:012d}"
            self.rows[rid] = {"world_id": params["world_id"], "kind": params["kind"],
                              "params": params["params"], "label": params["label"],
                              "created_by": params["created_by"],
                              "created_at": self.serial, "disabled_at": None}
            return [(rid,)]
        if s.startswith("update world_rules set disabled_at"):
            row = self.rows.get(params["id"])
            if not row or row["world_id"] != params["world_id"]:
                return []
            row["disabled_at"] = None if params["enabled"] else "now"
            return [(params["id"],)]
        if s.startswith("delete from world_rules"):
            row = self.rows.get(params["id"])
            if not row or row["world_id"] != params["world_id"]:
                return []
            del self.rows[params["id"]]
            return [(params["id"],)]
        if "from world_rules" in s:
            rows = [(rid, r["kind"], r["params"], r["label"], r["created_by"],
                     r["created_at"], r["disabled_at"])
                    for rid, r in self.rows.items()
                    if r["world_id"] == params["world_id"]]
            if "id = %(id)s" in sql:
                rows = [r for r in rows if r[0] == params["id"]]
            if "disabled_at is null" in s:
                rows = [r for r in rows if r[6] is None]
            return sorted(rows, key=lambda r: (r[5], r[0]))
        raise AssertionError(f"unexpected sql: {sql}")


def test_crud_round_trip():
    fake = FakeRulesDB()
    cur = FakeCursor(fake.handler)
    rule = rules.CannotRule(predicate="possesses", subject_entity_id=5,
                            object_entity_id=7, label="no key for Marcus")
    rid = rules.save_rule(cur, 1, rule, created_by=EDITOR)

    rows = rules_store.list_rules(cur, 1)
    assert [r["id"] for r in rows] == [rid]
    assert rows[0]["enabled"] is True and rows[0]["label"] == "no key for Marcus"
    # params jsonb round-trips into an identical typed rule
    back = rules.rule_from_row(rows[0])
    assert isinstance(back, rules.CannotRule)
    assert (back.predicate, back.subject_entity_id, back.object_entity_id,
            back.severity, back.label) == ("possesses", 5, 7, "warning",
                                           "no key for Marcus")
    assert rules_store.get_rule(cur, 1, rid)["id"] == rid
    assert rules_store.get_rule(cur, 2, rid) is None          # world-scoped

    assert rules_store.set_rule_enabled(cur, 1, rid, False) is True
    assert rules_store.list_rules(cur, 1, enabled_only=True) == []
    assert rules_store.list_rules(cur, 1)[0]["enabled"] is False
    assert rules_store.set_rule_enabled(cur, 1, rid, True) is True
    assert rules_store.list_rules(cur, 1, enabled_only=True)[0]["id"] == rid

    assert rules_store.delete_rule(cur, 1, rid) is True
    assert rules_store.list_rules(cur, 1) == []
    assert rules_store.delete_rule(cur, 1, rid) is False


def test_store_rejects_unknown_kind():
    with pytest.raises(ValueError):
        rules_store.create_rule(FakeCursor(lambda s, p: []), 1, "always", {})


# --- UI: the builder surface ----------------------------------------------------

app = FastAPI()
app.include_router(rules_ui.router)

ENTITIES = [
    {"id": 5, "kind": "character", "name": "Marcus", "dossier": None,
     "provisional": False, "n_aliases": 0, "n_subject": 3, "n_object": 0},
    {"id": 7, "kind": "object", "name": "Lighthouse Key", "dossier": None,
     "provisional": False, "n_aliases": 0, "n_subject": 0, "n_object": 2},
]

RULE_ROW = {"id": "aaaaaaaa-0000-0000-0000-000000000001", "kind": "cannot",
            "params": {"predicate": "possesses", "subject_entity_id": 5,
                       "subject_trait": None, "object_entity_id": 7,
                       "severity": "warning"},
            "label": "no key for Marcus", "created_by": None,
            "created_at": 1, "disabled_at": None, "enabled": True}
DISABLED_ROW = {"id": "aaaaaaaa-0000-0000-0000-000000000002", "kind": "exception",
                "params": {"check_name": "dead_speaker", "entity_id": 5,
                           "pos_from": None, "pos_to": None},
                "label": None, "created_by": None,
                "created_at": 2, "disabled_at": "2026-07-03", "enabled": False}


def make_token(sub=EDITOR, expires_in=3600):
    now = int(time.time())
    return jwt.encode({"sub": sub, "aud": auth.JWT_AUDIENCE, "email": "w@example.com",
                       "iat": now, "exp": now + expires_in}, SECRET, algorithm="HS256")


def bearer(sub):
    return {"Authorization": f"Bearer {make_token(sub=sub)}"}


@contextlib.contextmanager
def wired(rule_rows=None, auth_disabled=True):
    """TestClient over the router with every data function it touches faked.
    Yields (client, calls) recording store writes."""
    rule_rows = [dict(RULE_ROW), dict(DISABLED_ROW)] if rule_rows is None else rule_rows
    calls = {"create": [], "toggle": [], "delete": []}
    saved_env = {k: os.environ.get(k) for k in ("SUPABASE_JWT_SECRET", "AUTH_DISABLED")}
    db_names = ("list_worlds", "member_role", "entity_names", "list_entities")
    ui_names = ("store_list", "store_create", "store_set_enabled", "store_delete",
                "world_traits", "world_positions")
    saved_db = {n: getattr(db, n) for n in db_names}
    saved_ui = {n: getattr(rules_ui, n) for n in ui_names}
    try:
        os.environ["SUPABASE_JWT_SECRET"] = SECRET
        if auth_disabled:
            os.environ["AUTH_DISABLED"] = "1"
        else:
            os.environ.pop("AUTH_DISABLED", None)

        db.list_worlds = lambda: [dict(WORLD)]
        db.member_role = lambda world_id, user_id: (
            ROLES.get(user_id) if world_id == WORLD["id"] else None)
        db.entity_names = lambda world_id, ids: {5: "Marcus", 7: "Lighthouse Key"}
        db.list_entities = lambda world_id, kind=None, q=None: [dict(e) for e in ENTITIES]
        rules_ui.store_list = lambda world_id: [dict(r) for r in rule_rows]
        rules_ui.store_create = lambda world_id, rule, created_by=None: (
            calls["create"].append({"world_id": world_id, "rule": rule,
                                    "created_by": created_by}) or "rid-new")
        rules_ui.store_set_enabled = lambda world_id, rule_id, enabled: (
            calls["toggle"].append({"world_id": world_id, "rule_id": rule_id,
                                    "enabled": enabled}) or True)
        rules_ui.store_delete = lambda world_id, rule_id: (
            calls["delete"].append({"world_id": world_id, "rule_id": rule_id}) or True)
        rules_ui.world_traits = lambda world_id: ["ghost"]
        rules_ui.world_positions = lambda world_id: [1, 5, 9]

        yield TestClient(app, follow_redirects=False), calls
    finally:
        for k, v in saved_env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        for n, fn in saved_db.items():
            setattr(db, n, fn)
        for n, fn in saved_ui.items():
            setattr(rules_ui, n, fn)


CANNOT_FORM = {"kind": "cannot", "subject_mode": "entity", "subject_entity_id": "5",
               "subject_trait": "", "predicate": "possesses", "object_entity_id": "7",
               "severity": "warning", "label": "no key"}


def test_rules_page_lists_enabled_and_disabled_rules_with_descriptions():
    with wired() as (client, _):
        r = client.get("/worlds/1/rules")
    assert r.status_code == 200
    body = r.text
    assert "Marcus cannot possesses Lighthouse Key" in body    # names resolved
    assert "no key for Marcus" in body                         # writer label
    assert "skip dead_speaker for Marcus" in body              # disabled one still listed
    assert ">disabled<" in body                                # marked as off
    assert "Disable</button>" in body and "Enable</button>" in body  # both toggles offered
    assert f'action="/worlds/1/rules/{RULE_ROW["id"]}/toggle"' in body
    assert f'action="/worlds/1/rules/{RULE_ROW["id"]}/delete"' in body


def test_builder_form_is_selects_over_the_vocabulary_only():
    from extract.schema import PREDICATES

    with wired() as (client, _):
        body = client.get("/worlds/1/rules").text
    # every predicate is offered, as an option, verbatim
    for p in PREDICATES:
        assert f'<option value="{p}"' in body
    # entities and traits come from the world, as options
    assert "Marcus (character)" in body
    assert "Lighthouse Key (object)" in body
    assert '<option value="ghost">' in body
    # check families for the exception kind
    assert '<option value="dead_speaker">' in body
    # positions come from the world's scenes (the range stays select-only)
    assert '<option value="9">' in body
    # the ONLY free-text entry anywhere is the label field
    text_inputs = re.findall(r'<input[^>]*type="text"[^>]*>', body)
    assert len(text_inputs) == 1 and 'name="label"' in text_inputs[0]
    other_inputs = [i for i in re.findall(r"<input[^>]*>", body) if 'type="text"' not in i]
    assert all('type="hidden"' in i for i in other_inputs)
    assert "<textarea" not in body


def test_viewer_sees_builder_disabled_editor_enabled():
    with wired(auth_disabled=False) as (client, _):
        viewer_body = client.get("/worlds/1/rules", headers=bearer(VIEWER)).text
        editor_body = client.get("/worlds/1/rules", headers=bearer(EDITOR)).text
        anon_body = client.get("/worlds/1/rules").text          # reads stay open
    assert "disabled" in viewer_body.split("btn-rule-add", 1)[1][:60]
    assert "disabled" not in editor_body.split("btn-rule-add", 1)[1][:60]
    assert anon_body.count("Marcus cannot possesses") == 1      # still readable
    assert "disabled" in anon_body.split("btn-rule-add", 1)[1][:60]


def test_create_gates_on_editor_and_records_a_typed_rule():
    with wired(auth_disabled=False) as (client, calls):
        assert client.post("/worlds/1/rules", data=CANNOT_FORM).status_code == 401
        assert client.post("/worlds/1/rules", data=CANNOT_FORM,
                           headers=bearer(VIEWER)).status_code == 403
        assert calls["create"] == []
        r = client.post("/worlds/1/rules", data=CANNOT_FORM, headers=bearer(EDITOR))
        assert r.status_code == 303
        assert r.headers["location"] == "/worlds/1/rules"
    rec = calls["create"][0]
    assert rec["world_id"] == 1 and rec["created_by"] == EDITOR
    rule = rec["rule"]
    assert isinstance(rule, rules.CannotRule)
    assert (rule.predicate, rule.subject_entity_id, rule.object_entity_id,
            rule.label) == ("possesses", 5, 7, "no key")


def test_auth_disabled_keeps_the_dev_flow_working():
    with wired(auth_disabled=True) as (client, calls):
        r = client.post("/worlds/1/rules", data=CANNOT_FORM)    # no token
        assert r.status_code == 303
        assert calls["create"][0]["created_by"] == auth.DEV_USER.id


def test_create_rejects_bad_rules_with_400_and_stores_nothing():
    with wired() as (client, calls):
        # unknown predicate (form tampering — the select never offers it)
        r = client.post("/worlds/1/rules", data=dict(CANNOT_FORM, predicate="flies"))
        assert r.status_code == 400 and "rule rejected" in r.text
        # ambiguous subject: trait mode with no trait picked
        r = client.post("/worlds/1/rules",
                        data=dict(CANNOT_FORM, subject_mode="trait", subject_trait=""))
        assert r.status_code == 400
        # exception with an unknown family
        r = client.post("/worlds/1/rules",
                        data={"kind": "exception", "exc_check": "vibe_check",
                              "exc_entity_id": "5", "pos_from": "", "pos_to": ""})
        assert r.status_code == 400
        assert calls["create"] == []


def test_exception_and_only_create_via_their_form_fields():
    with wired() as (client, calls):
        r = client.post("/worlds/1/rules",
                        data={"kind": "exception", "exc_check": "dead_speaker",
                              "exc_entity_id": "5", "pos_from": "", "pos_to": "9",
                              "label": ""})
        assert r.status_code == 303
        r = client.post("/worlds/1/rules",
                        data={"kind": "only", "only_entity_id": "5",
                              "only_predicate": "knows", "only_object_entity_id": "",
                              "severity": "note", "label": ""})
        assert r.status_code == 303
    exc, only = calls["create"][0]["rule"], calls["create"][1]["rule"]
    assert isinstance(exc, rules.ExceptionRule)
    assert (exc.check_name, exc.entity_id, exc.pos_from, exc.pos_to) == (
        "dead_speaker", 5, None, 9)
    assert exc.label is None                                    # blank label -> None
    assert isinstance(only, rules.OnlyRule)
    assert (only.entity_id, only.predicate, only.severity) == (5, "knows", "note")


def test_toggle_and_delete_gate_on_editor_and_record():
    rid = RULE_ROW["id"]
    with wired(auth_disabled=False) as (client, calls):
        assert client.post(f"/worlds/1/rules/{rid}/toggle",
                           data={"enabled": "0"}).status_code == 401
        assert client.post(f"/worlds/1/rules/{rid}/toggle", data={"enabled": "0"},
                           headers=bearer(VIEWER)).status_code == 403
        assert client.post(f"/worlds/1/rules/{rid}/delete",
                           headers=bearer(VIEWER)).status_code == 403
        assert calls["toggle"] == [] and calls["delete"] == []
        r = client.post(f"/worlds/1/rules/{rid}/toggle", data={"enabled": "0"},
                        headers=bearer(EDITOR))
        assert r.status_code == 303
        assert calls["toggle"] == [{"world_id": 1, "rule_id": rid, "enabled": False}]
        r = client.post(f"/worlds/1/rules/{rid}/toggle", data={"enabled": "1"},
                        headers=bearer(EDITOR))
        assert calls["toggle"][-1]["enabled"] is True
        r = client.post(f"/worlds/1/rules/{rid}/delete", headers=bearer(EDITOR))
        assert r.status_code == 303
        assert calls["delete"] == [{"world_id": 1, "rule_id": rid}]


# --- CLI registration ------------------------------------------------------------

def test_cli_registers_rules_list_and_run():
    args = cli.build_parser().parse_args(["rules", "list", "--world", "greyharbor"])
    assert args.func is rules.cmd_rules
    assert args.action == "list" and args.world == "greyharbor"
    args = cli.build_parser().parse_args(["rules", "run", "--world", "greyharbor"])
    assert args.action == "run"
    with pytest.raises(SystemExit):
        cli.build_parser().parse_args(["rules", "audit", "--world", "w"])
    with pytest.raises(SystemExit):
        cli.build_parser().parse_args(["rules", "run"])         # world required


def test_cli_rules_without_database_exits_2(monkeypatch, capsys):
    for var in ("CANON_DB_URL", "DATABASE_URL"):
        monkeypatch.delenv(var, raising=False)
    args = cli.build_parser().parse_args(["rules", "run", "--world", "w"])
    assert args.func(args) == 2
    assert "requires a database" in capsys.readouterr().err


# --- the no-LLM guarantee ---------------------------------------------------------

def test_no_llm_or_anthropic_anywhere_in_the_rules_paths():
    paths = [ROOT / "canon" / "rules.py",
             ROOT / "canon" / "rules_store.py",
             ROOT / "ui" / "rules_ui.py",
             ROOT / "ui" / "templates" / "rules.html"]
    hits = []
    for path in paths:
        text = path.read_text(encoding="utf-8").lower()
        for needle in ("anthropic", "make_client", "messages.create", "llm("):
            if needle in text:
                hits.append((path.name, needle))
    assert hits == [], f"LLM machinery leaked into the rules paths: {hits}"


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))

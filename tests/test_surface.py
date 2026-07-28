"""Note surface tests (P3-SURFACE) — all offline.

FastAPI TestClient over the real app with ui.db's module-level functions faked
(the repo's fake-connection pattern; see tests/test_auth.py). Covers: the
Reader's Report view renders every section in spec order; seal/dismiss gate on
the editor role and write attribution; a dismissed note leaves the open list
and lands in the permanent footer; the ask endpoint renders citations and
refusals; script-view anchors and quote highlighting resolve; the draft-2 diff
counts status transitions.

    python -m pytest -q tests/test_surface.py
    python tests/test_surface.py
"""

import contextlib
import html
import os
import pathlib
import sys
import time

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

import jwt  # PyJWT — ui/requirements.txt  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from ask.engine import AskResult  # noqa: E402  (read-only import, never edited)
from ui import auth, db  # noqa: E402
from ui import notes as note_vm  # noqa: E402
from ui.app import app  # noqa: E402

SECRET = "test-secret-0123456789abcdef0123456789abcdef"
WORLD = {"id": 1, "name": "greyharbor_s1"}
EDITOR = "11111111-1111-1111-1111-111111111111"
VIEWER = "22222222-2222-2222-2222-222222222222"
ROLES = {EDITOR: "editor", VIEWER: "viewer"}

QUOTE = "I'll find Danny. That's a promise."

SCENES = [
    {"id": 3, "slug": "INT. FISHING BOAT - NIGHT", "story_position": 3,
     "is_flashback": False, "raw_text": f"MARA\n{QUOTE}\n\nShe cuts the engine.",
     "work_id": 1, "episode_title": "Pilot — Greyharbor", "sort_order": 1},
    {"id": 9, "slug": "EXT. HARBOR - DAY", "story_position": 9,
     "is_flashback": False, "raw_text": "The harbor at dawn. Danny is gone.",
     "work_id": 2, "episode_title": "Ep2 — Undertow", "sort_order": 2},
]

NOTE_OPEN = {
    "id": "aaaaaaaa-0000-0000-0000-000000000001",
    "note_key": "f2:openkey", "family": "F2",
    "summary": "The promise to find Danny has no later reference.",
    "body": "Set up in Pilot/sc1 and untouched after 20 story positions.",
    "evidence": {
        "citations": [{"assertion_id": 11, "scene_id": 3, "quote": QUOTE, "label": "Pilot/sc1"}],
        "anchor_entity_ids": [5], "anchor_scene_ids": [3],
    },
    "salience": 80, "status": "open",
    "first_seen_run": "run-2", "last_seen_run": "run-2",
    "resolved_at": None, "status_changed_by": None, "status_changed_at": None,
    "created_at": 2, "updated_at": 2,
}
NOTE_DISMISSED = {
    "id": "aaaaaaaa-0000-0000-0000-000000000002",
    "note_key": "f1:dismissedkey", "family": "F1",
    "summary": "Who or what is the Undertow Society?",
    "body": "Referenced provisional entity with no established canon row.",
    "evidence": {"citations": [{"assertion_id": None, "scene_id": 9,
                                "quote": "The harbor at dawn.", "label": "Ep2/sc1"}],
                 "anchor_entity_ids": [], "anchor_scene_ids": [9]},
    "salience": 60, "status": "dismissed",
    "first_seen_run": "run-1", "last_seen_run": "run-1",
    "resolved_at": None, "status_changed_by": EDITOR, "status_changed_at": "2026-07-02",
    "created_at": 1, "updated_at": 1,
}
NOTE_SEALED = {
    "id": "aaaaaaaa-0000-0000-0000-000000000003",
    "note_key": "f3:sealedkey", "family": "F3",
    "summary": "What Mara knows of the ledger stays dormant.",
    "body": "No later action on this fact across Pilot/sc1 through Ep2/sc1.",
    "evidence": {"citations": [{"assertion_id": 12, "scene_id": 3,
                                "quote": QUOTE, "label": "Pilot/sc1"}],
                 "anchor_entity_ids": [5], "anchor_scene_ids": [3]},
    "salience": 55, "status": "sealed",
    "first_seen_run": "run-1", "last_seen_run": "run-1",
    "resolved_at": None, "status_changed_by": EDITOR, "status_changed_at": "2026-07-02",
    "created_at": 1, "updated_at": 1,
}
NOTE_ADDRESSED = {
    "id": "aaaaaaaa-0000-0000-0000-000000000004",
    "note_key": "f2:addressedkey", "family": "F2",
    "summary": "The lighthouse key created in Pilot/sc1 has no later reference.",
    "body": "A later run found the gap closed.",
    "evidence": {"citations": [], "anchor_entity_ids": [], "anchor_scene_ids": [3]},
    "salience": 40, "status": "addressed",
    "first_seen_run": "run-1", "last_seen_run": "run-2",
    "resolved_at": "2026-07-03", "status_changed_by": None, "status_changed_at": None,
    "created_at": 1, "updated_at": 3,
}
NOTES = [NOTE_OPEN, NOTE_DISMISSED, NOTE_SEALED, NOTE_ADDRESSED]

FINDINGS = [
    {"id": 7, "check_name": "dead_speaks", "severity": "critical",
     "explanation": "Danny speaks in Ep2/sc1 after dying at pos 5.",
     "sealed": False, "scene_id": 9, "assertion_a": 21, "assertion_b": 22,
     "scene_slug": "EXT. HARBOR - DAY", "story_position": 9,
     "episode_title": "Ep2 — Undertow", "subject_a": "Danny", "subject_b": None},
    {"id": 8, "check_name": "location_conflict", "severity": "warning",
     "explanation": "Mara is in two places at pos 3.",
     "sealed": True, "scene_id": 3, "assertion_a": 23, "assertion_b": 24,
     "scene_slug": "INT. FISHING BOAT - NIGHT", "story_position": 3,
     "episode_title": "Pilot — Greyharbor", "subject_a": "Mara", "subject_b": None},
]

SUMMARY = {
    "totals": {"entities": 12, "assertions": 87, "scenes": 2, "works": 2,
               "findings_live": 1, "findings_sealed": 1},
    "entities_by_kind": [{"kind": "character", "n": 6, "provisional": 1}],
    "assertions_by_status": [{"status": "canon", "n": 80}, {"status": "draft", "n": 7}],
    "findings_by_sev": [{"severity": "critical", "live": 1, "sealed": 0}],
    "works": [{"id": 1, "title": "Pilot — Greyharbor", "sort_order": 1, "n_scenes": 1}],
}

LOAD_BEARING = [
    {"id": 5, "name": "Mara", "kind": "character", "n_assertions": 12, "n_scenes": 2},
    {"id": 6, "name": "Danny", "kind": "character", "n_assertions": 9, "n_scenes": 2},
]

ANSWERED = AskResult(
    question="what happened to danny",
    status="answered",
    rows=[{"subject": "Danny", "predicate": "dies", "object": None, "from_pos": 5,
           "supporting_quote": "Danny is gone.", "scene_id": 9,
           "citation": "Ep2/sc1 - EXT. HARBOR - DAY (pos 9)", "citation_label": "Ep2/sc1"}],
    citations=["Ep2/sc1 - EXT. HARBOR - DAY (pos 9)"],
)
REFUSED = AskResult(
    question="rank my episodes",
    status="refused",
    reason="cannot map this question to a Phase 0 cited SQL template; no answer shipped",
)


def make_token(sub=EDITOR, expires_in=3600):
    now = int(time.time())
    return jwt.encode({"sub": sub, "aud": auth.JWT_AUDIENCE, "email": "w@example.com",
                       "iat": now, "exp": now + expires_in}, SECRET, algorithm="HS256")


# --- harness: env + fake db wiring, restored on exit -------------------------

@contextlib.contextmanager
def wired(notes=None, auth_disabled=True, ask_result=ANSWERED):
    """TestClient over the real app with every db function the surface touches
    faked. Yields (client, calls) where calls records set_note_status and
    ask_question invocations."""
    notes = NOTES if notes is None else notes
    calls = {"status": [], "ask": []}
    saved_env = {k: os.environ.get(k) for k in ("SUPABASE_JWT_SECRET", "AUTH_DISABLED")}
    names = ("list_worlds", "member_role", "list_coverage_notes", "set_note_status",
             "list_findings", "world_summary", "load_bearing",
             "list_scenes_with_text", "entity_names", "ask_question")
    saved_fns = {n: getattr(db, n) for n in names}
    try:
        os.environ["SUPABASE_JWT_SECRET"] = SECRET
        if auth_disabled:
            os.environ["AUTH_DISABLED"] = "1"
        else:
            os.environ.pop("AUTH_DISABLED", None)

        db.list_worlds = lambda: [dict(WORLD)]
        db.member_role = lambda world_id, user_id: (
            ROLES.get(user_id) if world_id == WORLD["id"] else None)
        db.list_coverage_notes = lambda world_id: [dict(n) for n in notes]
        db.set_note_status = lambda world_id, note_id, status, changed_by=None: (
            calls["status"].append({"world_id": world_id, "note_id": note_id,
                                    "status": status, "changed_by": changed_by}) or True)
        db.list_findings = lambda world_id: [dict(f) for f in FINDINGS]
        db.world_summary = lambda world_id: SUMMARY
        db.load_bearing = lambda world_id, limit=6: LOAD_BEARING
        db.list_scenes_with_text = lambda world_id: [dict(s) for s in SCENES]
        db.entity_names = lambda world_id, ids: {5: "Mara", 6: "Danny"}
        db.ask_question = lambda world_id, question: (
            calls["ask"].append({"world_id": world_id, "question": question}) or ask_result)

        yield TestClient(app, follow_redirects=False), calls
    finally:
        for k, v in saved_env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        for n, fn in saved_fns.items():
            setattr(db, n, fn)


def bearer(sub):
    return {"Authorization": f"Bearer {make_token(sub=sub)}"}


# --- report view --------------------------------------------------------------

def test_report_renders_all_sections_in_spec_order():
    with wired() as (client, _):
        r = client.get("/worlds/1/report")
    assert r.status_code == 200
    body = r.text
    markers = [
        "Continuity findings",
        "Load-bearing canon",
        "Open Questions",
        "Idle Setups",
        "Dormant Knowledge",
        "Unmotivated Turns",
        "Corpus appendix",
    ]
    positions = [body.index(mk) for mk in markers]  # raises if any is missing
    assert positions == sorted(positions), "report sections out of spec order"
    # Deterministic sections carry their data.
    assert "dead_speaks" in body                       # live finding
    assert "Mara" in body and "12 assertions" in body  # load-bearing canon
    assert "87" in body                                # appendix assertion count


def test_report_note_is_collapsed_with_evidence_citations_and_actions():
    with wired() as (client, _):
        body = client.get("/worlds/1/report").text
    # Summary collapsed by default: a details element without `open`.
    i = body.index(f'id="note-{NOTE_OPEN["id"]}"')
    assert " open" not in body[body.rindex("<details", 0, i):body.index(">", i)]
    assert NOTE_OPEN["summary"] in body
    assert NOTE_OPEN["body"] in body                  # evidence inside the card
    assert "Pilot/sc1" in body                        # citation label
    # All four actions, wired to this world and note.
    assert f'action="/worlds/1/notes/{NOTE_OPEN["id"]}/seal"' in body
    assert f'action="/worlds/1/notes/{NOTE_OPEN["id"]}/dismiss"' in body
    assert "/worlds/1/script?scene=3" in body         # Show me
    assert "ask=what%20happened%20to%20Mara" in body  # Ask, scoped to the note's entity


def test_dismissed_and_sealed_notes_render_in_footer_not_open_sections():
    with wired() as (client, _):
        body = client.get("/worlds/1/report").text
    settled_at = body.index('id="settled-notes"')
    # The dismissed note appears exactly once, and only after the footer starts.
    assert body.count(NOTE_DISMISSED["summary"]) == 1
    assert body.index(NOTE_DISMISSED["summary"]) > settled_at
    assert body.count(NOTE_SEALED["summary"]) == 1
    assert body.index(NOTE_SEALED["summary"]) > settled_at
    # Its F1 open section is empty; settled cards carry no action forms.
    f1 = body[body.index('id="family-F1"'):body.index('id="family-F2"')]
    assert "No open notes." in f1
    settled = body[settled_at:]
    assert "/dismiss" not in settled and "/seal" not in settled
    assert "never re-raise" in settled                # permanence, stated


def test_next_render_after_dismissal_drops_note_from_open_list():
    dismissed_now = dict(NOTE_OPEN, status="dismissed", status_changed_by=EDITOR)
    with wired(notes=[dismissed_now]) as (client, _):
        body = client.get("/worlds/1/report").text
    f2 = body[body.index('id="family-F2"'):body.index('id="corpus-appendix"')]
    assert "No open notes." in f2
    assert NOTE_OPEN["summary"] not in f2
    assert body.index(NOTE_OPEN["summary"]) > body.index('id="settled-notes"')


def test_viewer_sees_actions_disabled_editor_sees_them_enabled():
    with wired(auth_disabled=False) as (client, _):
        viewer_body = client.get("/worlds/1/report", headers=bearer(VIEWER)).text
        editor_body = client.get("/worlds/1/report", headers=bearer(EDITOR)).text
        anon_body = client.get("/worlds/1/report").text  # still readable
    assert "btn-note-seal" in viewer_body and "disabled" in viewer_body.split("btn-note-seal", 1)[1][:40]
    assert "disabled" not in editor_body.split("btn-note-seal", 1)[1][:40]
    assert "disabled" in anon_body.split("btn-note-seal", 1)[1][:40]


# --- seal / dismiss endpoints ---------------------------------------------------

def test_seal_and_dismiss_gate_on_editor_and_write_attribution():
    with wired(auth_disabled=False) as (client, calls):
        url = f"/worlds/1/notes/{NOTE_OPEN['id']}/seal"
        assert client.post(url).status_code == 401                       # fail closed
        assert client.post(url, headers=bearer(VIEWER)).status_code == 403
        assert calls["status"] == []
        r = client.post(url, headers=bearer(EDITOR))
        assert r.status_code == 303
        assert r.headers["location"] == "/worlds/1/report"
        assert calls["status"] == [{"world_id": 1, "note_id": NOTE_OPEN["id"],
                                    "status": "sealed", "changed_by": EDITOR}]
        r = client.post(f"/worlds/1/notes/{NOTE_OPEN['id']}/dismiss", headers=bearer(EDITOR))
        assert r.status_code == 303
        assert calls["status"][-1]["status"] == "dismissed"
        assert calls["status"][-1]["changed_by"] == EDITOR


def test_auth_disabled_keeps_local_dev_flow_working():
    with wired(auth_disabled=True) as (client, calls):
        r = client.post(f"/worlds/1/notes/{NOTE_OPEN['id']}/dismiss")     # no token
        assert r.status_code == 303
        assert calls["status"][-1]["changed_by"] == auth.DEV_USER.id


def test_set_note_status_rejects_reopening_vocabulary():
    # The db layer refuses to write anything but the two permanent statuses.
    try:
        db.set_note_status(1, NOTE_OPEN["id"], "open")
    except ValueError:
        pass
    else:
        raise AssertionError("set_note_status must refuse status='open'")


# --- ask pane -------------------------------------------------------------------

def test_ask_endpoint_returns_answer_with_citations_linking_to_script():
    with wired() as (client, calls):
        r = client.post("/worlds/1/ask",
                        data={"question": "what happened to danny", "view": "report"})
    assert r.status_code == 200
    assert calls["ask"] == [{"world_id": 1, "question": "what happened to danny"}]
    body = r.text
    assert "Danny is gone." in body                        # cited quote
    assert "Ep2/sc1" in body                               # citation label
    assert "/worlds/1/script?scene=9" in body              # citation links into script view
    assert "Ep2/sc1 - EXT. HARBOR - DAY (pos 9)" in body   # full citation line


def test_ask_refusal_renders_honestly():
    with wired(ask_result=REFUSED) as (client, _):
        r = client.post("/worlds/1/ask",
                        data={"question": "rank my episodes", "view": "report"})
    assert r.status_code == 200
    assert "Refused:" in r.text
    assert "cannot map this question" in r.text


def test_ask_prefill_via_query_param():
    with wired() as (client, _):
        body = client.get("/worlds/1/report?ask=what+happened+to+Mara").text
    assert 'value="what happened to Mara"' in body


# --- script view ----------------------------------------------------------------

def test_script_view_anchors_gutters_and_citation_targets_resolve():
    with wired() as (client, _):
        r = client.get("/worlds/1/script")
    assert r.status_code == 200
    body = r.text
    assert 'id="scene-3"' in body and 'id="scene-9"' in body
    # Scene 3 carries the open note: gutter marker + annotation group.
    scene3 = body[body.index('id="scene-3"'):body.index('id="scene-9"')]
    assert "gutter-mark" in scene3
    assert 'id="anchors-3"' in body
    anno3 = body[body.index('id="anchors-3"'):]
    assert NOTE_OPEN["summary"] in anno3
    # The note's citation resolves to the scene anchor it cites.
    assert "#scene-3" in anno3
    # Live finding is anchored to its scene; sealed one stays out.
    assert 'id="anchors-9"' in body and "dead_speaks" in body
    assert "location_conflict" not in body


def test_script_view_highlights_the_stored_quote_server_side():
    with wired() as (client, _):
        r = client.get("/worlds/1/script", params={"scene": 3, "quote": QUOTE})
    assert r.status_code == 200
    assert f"<mark>{html.escape(QUOTE)}</mark>" in r.text


def test_script_view_settled_notes_leave_the_gutter():
    with wired(notes=[NOTE_DISMISSED, NOTE_SEALED]) as (client, _):
        body = client.get("/worlds/1/script").text
    assert NOTE_DISMISSED["summary"] not in body
    assert NOTE_SEALED["summary"] not in body


# --- draft-2 diff -----------------------------------------------------------------

def test_diff_view_counts_status_transitions_since_previous_run():
    with wired() as (client, _):
        r = client.get("/worlds/1/report/diff")
    assert r.status_code == 200
    body = r.text
    assert "1 note resolved (addressed) · 1 new · 1 sealed" in body
    assert NOTE_ADDRESSED["summary"] in body     # resolved list
    assert NOTE_OPEN["summary"] in body          # new list (first==last==current run)
    assert NOTE_SEALED["summary"] in body        # sealed list
    assert NOTE_DISMISSED["summary"] in body     # dismissed list


def test_diff_summary_is_pure_and_run_aware():
    diff = note_vm.diff_summary([dict(n) for n in NOTES])
    assert diff["current_run"] == "run-2"
    assert [n["note_key"] for n in diff["new"]] == ["f2:openkey"]
    assert [n["note_key"] for n in diff["resolved"]] == ["f2:addressedkey"]
    assert diff["banner"] == "1 note resolved (addressed) · 1 new · 1 sealed"
    # An old open note (seen in earlier runs too) is not "new".
    old_open = dict(NOTE_OPEN, note_key="f2:old", first_seen_run="run-1")
    diff2 = note_vm.diff_summary([old_open, NOTE_ADDRESSED])
    assert diff2["new"] == []


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

"""Landing + demo-report tests (P3-FRONTDOOR) — all offline.

Anonymous / renders the ratified poster (refusal headline, demo link, no
authenticated chrome); a signed-in user keeps the workbench overview exactly;
/demo/report renders the demo world read-only (share_mode) while logged out,
actions disabled, and 404s when the demo world is absent.

    python -m pytest -q tests/test_landing.py
"""

import contextlib
import os
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from fastapi.testclient import TestClient  # noqa: E402

from ui import db  # noqa: E402
from ui.app import app  # noqa: E402

WORLD = {"id": 1, "name": "greyharbor_s1"}

SUMMARY = {
    "totals": {"entities": 12, "assertions": 87, "scenes": 2, "works": 1,
               "findings_live": 1, "findings_sealed": 0},
    "entities_by_kind": [{"kind": "character", "n": 6, "provisional": 1}],
    "assertions_by_status": [{"status": "canon", "n": 80}, {"status": "draft", "n": 7}],
    "findings_by_sev": [{"severity": "critical", "live": 1, "sealed": 0}],
    "works": [{"id": 1, "title": "Pilot — Greyharbor", "sort_order": 1, "n_scenes": 2}],
}

FINDING = {
    "id": 7, "check_name": "dead_speaker", "severity": "critical",
    "explanation": "Danny speaks in Ep2/sc1 after dying at pos 5.",
    "sealed": False, "scene_id": 9, "assertion_a": 21, "assertion_b": None,
    "scene_slug": "EXT. HARBOR - DAY", "story_position": 9,
    "episode_title": "Ep2", "subject_a": "Danny", "subject_b": None,
}

NOTE = {
    "id": "aaaaaaaa-0000-0000-0000-000000000001",
    "note_key": "f2:openkey", "family": "F2",
    "summary": "The promise to find Danny has no later reference.",
    "body": "Set up in Pilot/sc1 and untouched after 20 story positions.",
    "evidence": {"citations": [{"assertion_id": 11, "scene_id": 3,
                                "quote": "I promise.", "label": "Pilot/sc1"}],
                 "anchor_entity_ids": [5], "anchor_scene_ids": [3]},
    "salience": 80, "status": "open",
    "first_seen_run": "run-1", "last_seen_run": "run-1",
    "resolved_at": None, "status_changed_by": None, "status_changed_at": None,
    "created_at": 1, "updated_at": 1,
}


@contextlib.contextmanager
def wired(*, auth_disabled, demo_world=WORLD, demo_env=None):
    saved_env = {k: os.environ.get(k)
                 for k in ("SUPABASE_JWT_SECRET", "AUTH_DISABLED", "CANON_DEMO_WORLD")}
    names = ("list_worlds", "get_world", "world_summary", "list_coverage_notes",
             "list_findings", "list_findings_composed", "load_bearing",
             "entity_names", "member_role")
    saved = {n: getattr(db, n) for n in names}
    try:
        os.environ.pop("SUPABASE_JWT_SECRET", None)
        if auth_disabled:
            os.environ["AUTH_DISABLED"] = "1"
        else:
            os.environ.pop("AUTH_DISABLED", None)
        if demo_env is None:
            os.environ.pop("CANON_DEMO_WORLD", None)
        else:
            os.environ["CANON_DEMO_WORLD"] = demo_env

        worlds = [dict(demo_world)] if demo_world else []
        db.list_worlds = lambda: [dict(w) for w in worlds]
        db.get_world = lambda name: (dict(demo_world)
                                     if demo_world and name == demo_world["name"]
                                     else None)
        db.world_summary = lambda world_id: SUMMARY
        db.list_coverage_notes = lambda world_id: [dict(NOTE)]
        db.list_findings = lambda world_id: [dict(FINDING)]
        db.list_findings_composed = lambda world_id: [dict(FINDING)]
        db.load_bearing = lambda world_id, limit=6: []
        db.entity_names = lambda world_id, ids: {5: "Mara"}
        db.member_role = lambda world_id, user_id: None

        yield TestClient(app, follow_redirects=False)
    finally:
        for k, v in saved_env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        for n, fn in saved.items():
            setattr(db, n, fn)


# --- the anonymous landing ------------------------------------------------------------

def test_anonymous_root_renders_the_poster():
    with wired(auth_disabled=False) as client:
        r = client.get("/")
        assert r.status_code == 200
        assert "This tool will never write a word of your story." in r.text
        assert 'href="/upload"' in r.text
        assert "first script free" in r.text
        assert 'href="/demo/report"' in r.text                # see a real report
        assert "FACTS LEARNED" in r.text                      # the script-page hero
        assert "prefers-reduced-motion" in r.text             # motion is opt-out safe
        assert "Canon never writes story" in r.text           # covenant footer


def test_anonymous_root_has_no_authenticated_chrome():
    with wired(auth_disabled=False) as client:
        text = client.get("/").text
        assert "Workbench" not in text
        assert "mainnav" not in text                          # base.html nav absent
        assert "worldpick" not in text
        assert 'href="/billing"' not in text


def test_authenticated_root_keeps_the_overview():
    with wired(auth_disabled=True) as client:
        r = client.get("/")
        assert r.status_code == 200
        assert "This tool will never write a word" not in r.text
        assert "Workbench" in r.text                          # the workbench chrome


# --- the logged-out demo report ---------------------------------------------------------

def test_demo_report_renders_logged_out_with_actions_disabled():
    with wired(auth_disabled=False) as client:
        r = client.get("/demo/report")
        assert r.status_code == 200
        # the real report content, cited
        assert NOTE["summary"] in r.text
        assert FINDING["explanation"] in r.text
        # share_mode chrome: no workbench nav, viral-loop footer instead
        assert "mainnav" not in r.text
        assert "run your own script through Canon" in r.text
        # actions disabled: every seal/dismiss button renders disabled
        assert "btn-note-seal" in r.text
        import re

        for m in re.finditer(r"<button[^>]*btn-note-(seal|dismiss)[^>]*>", r.text):
            assert "disabled" in m.group(0)


def test_demo_report_missing_world_is_404():
    with wired(auth_disabled=False, demo_world=None) as client:
        assert client.get("/demo/report").status_code == 404


def test_demo_world_env_override():
    other = {"id": 2, "name": "fbi_pilot"}
    with wired(auth_disabled=False, demo_world=other, demo_env="fbi_pilot") as client:
        assert client.get("/demo/report").status_code == 200
    # env points at a world that is not loaded -> 404, even with others present
    with wired(auth_disabled=False, demo_env="fbi_pilot") as client:
        assert client.get("/demo/report").status_code == 404


if __name__ == "__main__":
    import pytest

    raise SystemExit(pytest.main(["-q", __file__]))

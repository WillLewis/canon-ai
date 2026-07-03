"""Reader's Report engine tests.

These stay offline: the report validator checks a snapshot shaped like the rows
loaded from Postgres, which lets the citation rules be tested without requiring
a live Supabase instance.
"""

import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from canon import report  # noqa: E402


def _scene(scene_id=1, pos=1, label="E101/sc1", raw=None):
    return {
        "id": scene_id,
        "work_title": label.split("/")[0],
        "scene_index": int(label.split("sc")[-1]),
        "label": label,
        "slug": "INT. ROOM - DAY",
        "story_position": pos,
        "raw_text": raw or "Mara says she will find the bell.\nA second line.",
    }


def _entity(eid, name, kind="character", provisional=False):
    return {"id": eid, "name": name, "kind": kind, "aliases": [], "provisional": provisional}


def _assertion(
    aid,
    scene,
    subject,
    predicate="promised",
    object_value="find bell",
    *,
    object_entity=None,
    quote="Mara says she will find the bell.",
    polarity=True,
):
    row = {
        "id": aid,
        "world_id": 1,
        "subject_id": subject["id"],
        "subject_name": subject["name"],
        "subject_kind": subject["kind"],
        "predicate": predicate,
        "object_id": object_entity["id"] if object_entity else None,
        "object_name": object_entity["name"] if object_entity else None,
        "object_kind": object_entity["kind"] if object_entity else None,
        "object_value": object_value,
        "object_assertion_id": None,
        "polarity": polarity,
        "start_pos": scene["story_position"],
        "end_pos": None,
        "start_inf": False,
        "end_inf": True,
        "scene_id": scene["id"],
        "supporting_quote": quote,
        "confidence": 0.99,
        "status": "canon",
        "story_position": scene["story_position"],
        "scene": scene,
    }
    row["signature"] = report.assertion_signature(row)
    return row


def _snapshot(assertions, scenes=None, entities=None):
    scenes = scenes or sorted({a["scene_id"]: a["scene"] for a in assertions}.values(), key=lambda s: s["id"])
    entities = entities or []
    return {
        "world_id": 1,
        "scenes": scenes,
        "scene_by_id": {s["id"]: s for s in scenes},
        "entities": entities,
        "entity_by_id": {e["id"]: e for e in entities},
        "assertions": assertions,
        "assertion_by_id": {a["id"]: a for a in assertions},
        "presence": [],
    }


def test_f2_idle_setup_flags_only_zero_downstream_reference():
    mara = _entity(1, "Mara Voss")
    danny = _entity(2, "Danny Voss")
    s1 = _scene(1, 1, "E101/sc1")
    s2 = _scene(2, 2, "E101/sc2", "Mara says she will find Danny.")
    s9 = _scene(9, 9, "E102/sc3", "Danny is alive.")
    s20 = _scene(20, 20, "E104/sc2", "The harbor is quiet.")
    idle = _assertion(1, s1, mara, object_value="find bell")
    decoy = _assertion(2, s2, mara, object_value="find Danny", quote="Mara says she will find Danny.")
    downstream = _assertion(3, s9, danny, "alive", "alive", quote="Danny is alive.")
    snap = _snapshot([idle, decoy, downstream], [s1, s2, s9, s20], [mara, danny])

    candidates = report.collect_idle_setup_candidates(snap, threshold=5)

    assert len(candidates) == 1
    assert candidates[0]["anchor_assertion_ids"] == [1]
    assert "find bell" in candidates[0]["summary_seed"]


def test_validator_drops_fabricated_quote():
    mara = _entity(1, "Mara Voss")
    scene = _scene()
    assertion = _assertion(1, scene, mara)
    snap = _snapshot([assertion], [scene], [mara])
    candidate = report.collect_idle_setup_candidates(
        {**snap, "scenes": [scene, _scene(2, 20, "E104/sc1", "Later.")]},
        threshold=1,
    )[0]
    draft = [{
        "candidate_id": candidate["candidate_id"],
        "surface": True,
        "summary": "Mara's promise has no later reference.",
        "body": "The setup is not touched again.",
        "salience": 10,
        "motivation_status": None,
        "citations": [{"assertion_id": 1, "scene_id": 1, "quote": "fabricated quote"}],
    }]

    valid, logs = report.validate_draft_notes(snap, draft, [candidate])

    assert valid == []
    assert logs and "quote mismatch" in logs[0].message


def test_note_key_is_stable_across_reloaded_assertion_ids():
    mara = _entity(1, "Mara Voss")
    scene = _scene()
    old = _assertion(1, scene, mara)
    new = _assertion(999, scene, mara)
    snap_old = _snapshot([old], [scene], [mara])
    snap_new = _snapshot([new], [scene], [mara])

    old_key = report.collect_idle_setup_candidates(
        {**snap_old, "scenes": [scene, _scene(2, 20, "E104/sc1", "Later.")]},
        threshold=1,
    )[0]["note_key"]
    new_key = report.collect_idle_setup_candidates(
        {**snap_new, "scenes": [scene, _scene(2, 20, "E104/sc1", "Later.")]},
        threshold=1,
    )[0]["note_key"]

    assert old_key == new_key


def test_dismissed_note_key_suppresses_reloaded_candidate():
    mara = _entity(1, "Mara Voss")
    scene = _scene()
    first = _assertion(1, scene, mara)
    second = _assertion(42, scene, mara)
    later = _scene(2, 20, "E104/sc1", "Later.")
    first_key = report.collect_idle_setup_candidates(
        _snapshot([first], [scene, later], [mara]),
        threshold=1,
    )[0]["note_key"]
    candidates = report.collect_candidates(
        _snapshot([second], [scene, later], [mara]),
        threshold=1,
        suppressed_note_keys={first_key},
    )

    assert candidates["F2"] == []


def test_f4_validator_drops_weak_language():
    mara = _entity(1, "Mara Voss")
    scene = _scene(raw="Mara takes the ledger.")
    action = _assertion(1, scene, mara, "possesses", None, quote="Mara takes the ledger.")
    snap = _snapshot([action], [scene], [mara])
    candidate = report.collect_unmotivated_turn_candidates(snap)[0]
    draft = [{
        "candidate_id": candidate["candidate_id"],
        "surface": True,
        "summary": "Mara's turn has weak established motivation.",
        "body": "The nearest motivation is weak.",
        "salience": 10,
        "motivation_status": "distant",
        "citations": [{"assertion_id": 1, "scene_id": 1, "quote": "Mara takes the ledger."}],
    }]

    valid, logs = report.validate_draft_notes(snap, draft, [candidate])

    assert valid == []
    assert logs and "banned weak" in logs[0].message


def test_rendered_report_body_obeys_line_cap():
    mara = _entity(1, "Mara Voss")
    scene = _scene()
    assertion = _assertion(1, scene, mara)
    snap = _snapshot([assertion], [scene], [mara])
    notes = []
    for i in range(40):
        notes.append({
            "note_key": f"f1:{i}",
            "family": "F1",
            "summary": f"Question {i}?",
            "body": "Evidence sentence.",
            "salience": 10 - i,
            "status": "open",
            "evidence": {"citations": [{"scene_id": 1, "label": "E101/sc1"}]},
        })

    md = report.render_markdown_report("greyharbor", snap, [], notes, body_line_limit=30)

    assert report.body_line_count(md) <= 30
    assert "Corpus Appendix" in md

"""Tests for bench/failure_modes.py — the failure-mode attributor.

Self-contained: builds a tiny synthetic ground truth + dependency map and drives
attribute() directly, exercising each attribution branch (caught, extraction,
resolution, fact-canonicalization, store-range, store-presence, the
premature_knowledge prior-knower split, the capability violating-act split, false
positives, and trap violations). No database or real artifacts required.
"""

from __future__ import annotations

from bench import failure_modes as fm


def _gt():
    return {
        "alias_map": {
            "mara": "Mara Voss", "tobias": "Tobias Voss", "cole": "Cole Brannigan",
            "danny": "Danny Voss", "blue gull hold": "Blue Gull Hold",
            "ferry hold": "Blue Gull Hold", "courthouse archive": "Courthouse Archive",
        },
        "object_value_synonyms": {
            "drive": ["drive", "driving"], "ledger location": ["ledger location"],
            "swim": ["swim", "swimming"],
        },
        "expected_assertions": [
            {"id": "A1", "subject": "Mara Voss", "predicate": "cannot", "object_value": "drive"},
            {"id": "A2", "subject": "Tobias Voss", "predicate": "dies", "object_value": None},
            {"id": "A4", "subject": "Tobias Voss", "predicate": "knows", "object_value": "ledger location"},
            {"id": "A10", "subject": "Cole Brannigan", "predicate": "knows", "object_value": "ledger location"},
            {"id": "A24", "subject": "Danny Voss", "predicate": "cannot", "object_value": "swim"},
            {"id": "A33", "subject": "Danny Voss", "predicate": "located_at", "object_entity": "Blue Gull Hold"},
            {"id": "A34", "subject": "Danny Voss", "predicate": "located_at", "object_entity": "Courthouse Archive"},
        ],
        "expected_findings": [
            {"id": "P1", "check": "dead_speaker", "must_mention": ["Tobias"]},
            {"id": "P2", "check": "premature_knowledge", "must_mention": ["Cole"]},
            {"id": "P4", "check": "capability_violation", "must_mention": ["Mara", "drive"]},
            {"id": "P6", "check": "presence_conflict", "must_mention": ["Danny", "Blue Gull", "Courthouse"]},
            {"id": "P11", "check": "capability_violation", "must_mention": ["Danny", "swim"]},
        ],
        "expected_notes": [],
        "trap_findings_must_not_flag": [
            {"id": "T1", "check": "premature_knowledge", "must_not_mention_all": ["Mara", "arithmetic"]},
        ],
        "thresholds": {"episode_count": 8},
    }


def _fmap():
    return {
        "_comment": "ignored",
        "P1": {"check": "dead_speaker", "requires_all": ["A2"], "presence_dependent": True,
               "downstream": "store_presence_modeling"},
        "P2": {"check": "premature_knowledge", "requires_all": ["A10"],
               "requires_prior_any": ["A4"], "downstream": "sql_check_miss"},
        "P4": {"check": "capability_violation", "requires_all": ["A1"],
               "violation_keyword": "drive", "downstream": "sql_check_miss"},
        "P6": {"check": "presence_conflict", "requires_all": ["A33", "A34"],
               "downstream": "store_range_modeling"},
        "P11": {"check": "capability_violation", "requires_all": ["A24"],
                "violation_keyword": "swim", "downstream": "sql_check_miss"},
    }


def _case(report, fid):
    return next(c for c in report["cases"] if c["id"] == fid)


# convenience assertion builders -------------------------------------------------
def loc(subj, ent):
    return {"subject": subj, "predicate": "located_at", "object_value": None, "object_entity": ent}


def knows(subj, val):
    return {"subject": subj, "predicate": "knows", "object_value": val, "object_entity": None}


def cannot(subj, val):
    return {"subject": subj, "predicate": "cannot", "object_value": val, "object_entity": None}


def dead_finding(name="Tobias Voss"):
    return {"check": "dead_speaker", "severity": "critical", "sealed": False,
            "explanation": f"{name} appears in a scene but died earlier."}


# tests --------------------------------------------------------------------------

def test_caught_is_not_attributed():
    gt, fmap = _gt(), _fmap()
    findings = [dead_finding()]
    resolved = [knows("Tobias Voss", "x")]  # A2 absence doesn't matter; P1 was caught
    rep = fm.attribute(findings, [], resolved, gt, fmap)
    c = _case(rep, "P1")
    assert c["status"] == "caught" and c["layer"] is None
    assert rep["summary"]["planted_caught"] == 1


def test_extraction_miss_when_required_absent_everywhere():
    gt, fmap = _gt(), _fmap()
    rep = fm.attribute([], [], [], gt, fmap)          # P6 missed, A33/A34 nowhere
    assert _case(rep, "P6")["layer"] == fm.EXTRACTION
    assert "P6" in rep["summary"]["miss_histogram"][fm.EXTRACTION]


def test_resolution_miss_when_present_in_candidates_only():
    gt, fmap = _gt(), _fmap()
    candidates = [loc("Danny Voss", "Blue Gull Hold"), loc("Danny Voss", "Courthouse Archive")]
    rep = fm.attribute([], candidates, [], gt, fmap)  # in candidates, gone after resolve
    assert _case(rep, "P6")["layer"] == fm.RESOLUTION


def test_store_range_when_locations_present_both_stages():
    gt, fmap = _gt(), _fmap()
    both = [loc("Danny Voss", "Blue Gull Hold"), loc("Danny Voss", "Courthouse Archive")]
    rep = fm.attribute([], both, both, gt, fmap)      # data present, presence_conflict still missed
    assert _case(rep, "P6")["layer"] == fm.STORE_RANGE


def test_fact_canonicalization_high_overlap_drift():
    gt, fmap = _gt(), _fmap()
    # Cole knows the RIGHT fact, handle merely reworded (high token overlap) -> fact-canon
    resolved = [knows("Cole Brannigan", "location where the ledger sits"),
                knows("Tobias Voss", "ledger location")]
    rep = fm.attribute([], resolved, resolved, gt, fmap)
    assert _case(rep, "P2")["layer"] == fm.FACT_CANON


def test_value_drift_low_overlap_is_extraction_proposition():
    gt, fmap = _gt(), _fmap()
    # Cole 'knows' a DIFFERENT proposition (no overlap with the canonical handle):
    # extraction recorded the wrong fact, so fact identity alone can't recover it (P8 shape).
    resolved = [knows("Cole Brannigan", "the weather turned cold"),
                knows("Tobias Voss", "ledger location")]
    rep = fm.attribute([], resolved, resolved, gt, fmap)
    assert _case(rep, "P2")["layer"] == fm.EXTRACTION_PROP


def test_premature_knowledge_prior_knower_missing_is_extraction():
    gt, fmap = _gt(), _fmap()
    # Cole knows the right thing, but no prior knower (A4) exists -> asymmetry undetectable
    resolved = [knows("Cole Brannigan", "ledger location")]
    rep = fm.attribute([], resolved, resolved, gt, fmap)
    c = _case(rep, "P2")
    assert c["layer"] == fm.EXTRACTION and "prior knower" in c["detail"]


def test_premature_knowledge_all_present_is_sql_check():
    gt, fmap = _gt(), _fmap()
    resolved = [knows("Cole Brannigan", "ledger location"), knows("Tobias Voss", "ledger location")]
    rep = fm.attribute([], resolved, resolved, gt, fmap)
    assert _case(rep, "P2")["layer"] == fm.SQL_CHECK


def test_capability_violating_act_missing_is_extraction():
    gt, fmap = _gt(), _fmap()
    resolved = [cannot("Danny Voss", "swim")]         # rule present, no swimming act extracted
    rep = fm.attribute([], resolved, resolved, gt, fmap)
    c = _case(rep, "P11")
    assert c["layer"] == fm.EXTRACTION and "violating act" in c["detail"]


def test_capability_with_violating_act_is_sql_check():
    gt, fmap = _gt(), _fmap()
    resolved = [cannot("Danny Voss", "swim"),
                {"subject": "Danny Voss", "predicate": "action", "object_value": "swimming hard", "object_entity": None}]
    rep = fm.attribute([], resolved, resolved, gt, fmap)
    assert _case(rep, "P11")["layer"] == fm.SQL_CHECK


def test_false_positive_and_trap_counts():
    gt, fmap = _gt(), _fmap()
    findings = [
        {"check": "presence_conflict", "severity": "critical", "sealed": False,
         "explanation": "Some unrelated overlap nobody planted."},               # FP
        {"check": "premature_knowledge", "severity": "critical", "sealed": False,
         "explanation": "Mara Voss knows arithmetic with no source."},           # trips T1 (+ FP)
    ]
    rep = fm.attribute(findings, [], [], gt, fmap)
    assert rep["summary"]["false_positives"] == 2
    assert rep["summary"]["trap_violations"] == ["T1"]


def test_fact_match_rejects_single_shared_token():
    # the conservative matcher behind the grounded probes: a single shared token (e.g. a
    # name) must NOT trigger a fact link — this is what stopped P8's phantom 'cole' merge.
    syn = {"cole payment": ["cole payment"],
           "stove pipe cache": ["pipe sleeve behind the stove"]}
    assert not fm._fact_match("cole went home that night", "cole payment", syn)   # shares only 'cole'
    assert fm._fact_match("hidden in the pipe sleeve behind stove", "stove pipe cache", syn)  # 4 shared


def test_fact_match_single_token_expected_handle():
    syn = {"drive": ["drive", "driving"]}
    assert fm._fact_match("driving hard down the cliff road", "drive", syn)
    assert not fm._fact_match("walked slowly to the door", "drive", syn)


def test_intransitive_present_with_stray_value_is_not_fact_canon():
    # P1 (dead_speaker) requires A2 (Tobias dies — intransitive). A `dies` row exists but
    # carries a stray object_value, so the value-matcher misses it; the check keys on the
    # predicate, so this is downstream (store_presence), NOT fact_canonicalization/entity-drift.
    gt, fmap = _gt(), _fmap()
    resolved = [{"subject": "Tobias Voss", "predicate": "dies",
                 "object_value": "killed offscreen", "object_entity": None}]
    rep = fm.attribute([], resolved, resolved, gt, fmap)
    c = _case(rep, "P1")
    assert c["layer"] != fm.FACT_CANON and c["layer"] == fm.STORE_PRESENCE


def test_resolution_drift_attributed_to_resolution_not_drift():
    # A10 correct in candidates but drifted in resolution -> RESOLUTION, not a drift layer.
    gt, fmap = _gt(), _fmap()
    candidates = [knows("Cole Brannigan", "ledger location")]
    resolved = [knows("Cole Brannigan", "somewhere unrelated")]
    rep = fm.attribute([], candidates, resolved, gt, fmap)
    assert _case(rep, "P2")["layer"] == fm.RESOLUTION


def test_capability_entry_with_empty_requires_all_does_not_crash():
    # malformed map entry (empty requires_all) must not IndexError the whole attribution run.
    gt = _gt()
    fmap = {"PZ": {"check": "capability_violation", "violation_keyword": "drive", "requires_all": []}}
    rep = fm.attribute([], [], [], gt, fmap)
    assert _case(rep, "PZ")["status"] == "missed"


def test_render_tolerates_missing_check_and_detail():
    # a case with check=None / detail=None must not TypeError the human-readable report.
    report = {"summary": {"planted_caught": 0, "planted_total": 1, "miss_histogram": {},
                          "false_positives": 0, "fp_by_check": {}, "trap_violations": []},
              "cases": [{"id": "PZ", "check": None, "planted": True, "status": "missed",
                         "layer": fm.SQL_CHECK, "detail": None, "grounded": False}]}
    assert "PZ" in fm.render(report)

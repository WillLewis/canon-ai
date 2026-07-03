"""Coverage-note eval contract tests."""

import importlib.util
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

_spec = importlib.util.spec_from_file_location("run_eval", ROOT / "eval" / "run_eval.py")
run_eval = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(run_eval)


def test_answer_key_parses_coverage_items_and_decoys():
    gt = run_eval.parse_answer_key(ROOT / "fixtures" / "greyharbor" / "answer-key.md")

    assert [c["id"] for c in gt["expected_coverage"]] == ["C1", "C2", "C3", "C4"]
    assert [d["id"] for d in gt["coverage_decoys_must_not_flag"]] == ["D1", "D2", "D3"]


def test_score_coverage_matches_expected_and_catches_decoy():
    old = run_eval.GT
    run_eval.GT = run_eval.parse_answer_key(ROOT / "fixtures" / "greyharbor" / "answer-key.md")
    try:
        notes = [
            {
                "family": "F1",
                "summary": "Who or what is Danny Voss?",
                "body": "Referenced by the skiff.",
                "status": "open",
                "evidence": {"citations": [{"label": "E101/sc4", "quote": "Mara finds her brother's skiff, the DANNY-LEE, tied off and empty."}]},
            },
            {
                "family": "F2",
                "summary": "Mara's promise to find Danny has no later reference.",
                "body": "Set up in E101/sc4.",
                "status": "open",
                "evidence": {"citations": [{"label": "E101/sc4", "quote": "I'll find you, Danny. Whatever it takes."}]},
            },
            {
                "family": "F3",
                "summary": "Cole's knowledge of the ledger location stays dormant.",
                "body": "Cole states the ledger location in E102/sc1.",
                "status": "open",
                "evidence": {"citations": [{"label": "E102/sc1", "quote": "It's still under the chapel floor stone where your uncle hid it."}]},
            },
            {
                "family": "F4",
                "summary": "Mara's drive has absent established motivation.",
                "body": "Mara drives in E102/sc4.",
                "status": "open",
                "evidence": {"citations": [{"label": "E102/sc4", "quote": "Mara behind the wheel of Tobias's pickup, driving hard along the cliff road, ledger on the passenger seat."}]},
            },
            {
                "family": "F3",
                "summary": "Mara's ledger knowledge stays dormant.",
                "body": "Mara pries up the loose floor stone in E102/sc2.",
                "status": "open",
                "evidence": {"citations": [{"label": "E102/sc2", "quote": "Mara pries up the loose floor stone."}]},
            },
        ]

        found, missed, fps, decoys = run_eval.score_coverage(notes)

        assert found == ["C1", "C2", "C3", "C4"]
        assert missed == []
        assert len(fps) == 1
        assert decoys and decoys[0][0] == "D2"
    finally:
        run_eval.GT = old

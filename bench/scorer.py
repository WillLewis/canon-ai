"""Vendor-neutral benchmark scoring — the source of truth (no Braintrust needed).

Given a system's continuity findings and the answer key, compute per-case verdicts
(was each planted error caught? was each trap respected?) and summary metrics
(recall, false positives, trap-respect). Reuses eval/run_eval.py's matching so
Canon and the chatbot are graded identically. Braintrust (bench/braintrust_eval.py)
is a visualization layer over these numbers; this module stands alone and is what
a blog post should cite as the result.
"""

from __future__ import annotations

import importlib.util
import pathlib

ROOT = pathlib.Path(__file__).resolve().parents[1]
_spec = importlib.util.spec_from_file_location("run_eval", ROOT / "eval" / "run_eval.py")
run_eval = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(run_eval)


def evaluate(findings: list, gt: dict) -> dict:
    """Score one system's findings against the answer key `gt`.

    Returns {"cases": [...per planted error / trap...], "summary": {...}}.
    Each case carries `flagged` (did the system raise it) and `correct` (did that
    match the key) so the Braintrust layer can render a per-case green/red table.
    """
    run_eval.GT = gt
    found, _missed, fps, trap_hits = run_eval.score_findings(findings)
    found = set(found)
    violated = {th[0] for th in trap_hits}

    cases: list = []
    for f in gt["expected_findings"]:
        flagged = f["id"] in found
        cases.append({
            "id": f["id"], "kind": "planted", "check": f["check"], "expected": "flag",
            "flagged": flagged, "correct": flagged,
            "where": f.get("where"), "must_mention": f.get("must_mention"),
        })
    for t in gt["trap_findings_must_not_flag"]:
        flagged = t["id"] in violated
        cases.append({
            "id": t["id"], "kind": "trap", "check": t.get("check"), "expected": "no-flag",
            "flagged": flagged, "correct": not flagged,
            "where": t.get("where"), "description": t.get("description"),
        })

    planted = [c for c in cases if c["kind"] == "planted"]
    traps = [c for c in cases if c["kind"] == "trap"]
    n_ep = gt["thresholds"]["episode_count"]
    summary = {
        "recall": round(sum(c["correct"] for c in planted) / len(planted), 3) if planted else 0.0,
        "planted_found": sum(c["correct"] for c in planted),
        "planted_total": len(planted),
        "missed": [c["id"] for c in planted if not c["correct"]],
        "false_positives": len(fps),
        "fp_per_episode": round(len(fps) / n_ep, 2),
        "fp_samples": [f.get("explanation", "")[:90] for f in fps][:6],
        "traps_respected": sum(c["correct"] for c in traps),
        "traps_total": len(traps),
        "trap_violations": [c["id"] for c in traps if not c["correct"]],
    }
    return {"cases": cases, "summary": summary}


def merge_runs(evals: list) -> dict:
    """Aggregate repeated runs of a non-deterministic system (the chatbot) — the
    consistency story: caught-every-run vs caught-some-run, mean recall, mean FP."""
    if not evals:
        return {}
    planted_ids = [c["id"] for c in evals[0]["cases"] if c["kind"] == "planted"]
    caught_counts = {pid: 0 for pid in planted_ids}
    for ev in evals:
        for c in ev["cases"]:
            if c["kind"] == "planted" and c["correct"]:
                caught_counts[c["id"]] += 1
    n = len(evals)
    every = [p for p, k in caught_counts.items() if k == n]
    some = [p for p, k in caught_counts.items() if 0 < k < n]
    never = [p for p, k in caught_counts.items() if k == 0]
    return {
        "runs": n,
        "recall_mean": round(sum(e["summary"]["recall"] for e in evals) / n, 3),
        "recall_every_run": round(len(every) / len(planted_ids), 3) if planted_ids else 0.0,
        "caught_every_run": sorted(every),
        "flaky": sorted(some),         # caught some runs, missed others — the variance Canon lacks
        "never_caught": sorted(never),
        "fp_per_episode_mean": round(sum(e["summary"]["fp_per_episode"] for e in evals) / n, 2),
        "trap_violations_any": sorted({t for e in evals for t in e["summary"]["trap_violations"]}),
    }

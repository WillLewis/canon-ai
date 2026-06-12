#!/usr/bin/env python3
"""
Canon AI — Phase 0 evaluation harness.

Scores the pipeline against fixtures/greyharbor ground truth and prints
PASS/FAIL against the Phase 0 exit criteria (PLAN.md).

This file also defines the I/O CONTRACT the pipeline must produce:

  out/assertions.json   {"assertions": [{"subject": str, "predicate": str,
                          "object_value": str|null, "object_entity": str|null, ...}]}
  out/findings.json     {"findings": [{"check": str, "severity": str,
                          "explanation": str, "sealed": bool, ...}]}

Usage:
  python eval/run_eval.py --assertions out/assertions.json --findings out/findings.json
  python eval/run_eval.py --demo        # run against bundled sample output

Run after EVERY extraction-prompt, schema, or check change.
Exit code 0 = all Phase 0 gates pass; 1 = any gate fails.
"""
import argparse, json, re, sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
GT = json.loads((ROOT / "expected_assertions.json").read_text())


def norm(s):
    return re.sub(r"[^a-z0-9 ]", "", (s or "").lower()).strip()


def canon_entity(name, alias_map):
    return alias_map.get(norm(name), name if name is None else name.strip())


def value_matches(expected_key, actual_value, synonyms):
    if expected_key is None:
        return actual_value in (None, "",)
    a = norm(actual_value)
    if not a:
        return False
    syns = {norm(k): v for k, v in synonyms.items()}
    for syn in syns.get(norm(expected_key), [expected_key]):
        s = norm(syn)
        if s and (s in a or a in s):
            return True
    return False


def score_extraction(actual_assertions):
    alias_map = {norm(k): v for k, v in GT["alias_map"].items()}
    synonyms = GT["object_value_synonyms"]
    matched, missed = [], []
    for exp in GT["expected_assertions"]:
        hit = None
        for act in actual_assertions:
            if canon_entity(act.get("subject"), alias_map) != exp["subject"]:
                continue
            if norm(act.get("predicate")) != norm(exp["predicate"]):
                continue
            if "object_entity" in exp and exp.get("object_entity"):
                if canon_entity(act.get("object_entity"), alias_map) == exp["object_entity"]:
                    hit = act; break
            elif value_matches(exp.get("object_value"), act.get("object_value"), synonyms):
                hit = act; break
            elif exp.get("object_value") is None and not act.get("object_value") and not act.get("object_entity"):
                hit = act; break
        (matched if hit else missed).append(exp["id"])
    recall = len(matched) / len(GT["expected_assertions"])
    return recall, matched, missed


def score_findings(actual_findings):
    live = [f for f in actual_findings if not f.get("sealed")]

    def mentions(f, terms):
        text = norm(f.get("explanation", ""))
        return all(norm(t) in text for t in terms)

    found, missed_planted = [], []
    consumed = set()
    for exp in GT["expected_findings"] + GT["expected_notes"]:
        hit = next((i for i, f in enumerate(live)
                    if i not in consumed
                    and norm(f.get("check", "")) == norm(exp["check"])
                    and mentions(f, exp["must_mention"])), None)
        if hit is None:
            missed_planted.append(exp["id"])
        else:
            consumed.add(hit); found.append(exp["id"])

    trap_hits = []
    for trap in GT["trap_findings_must_not_flag"]:
        for f in live:
            if trap["check"] not in ("*", f.get("check")):
                continue
            if mentions(f, trap["must_not_mention_all"]):
                trap_hits.append((trap["id"], f.get("explanation", "")[:90]))

    # false positives: critical/warning findings not consumed by any expectation
    fps = [f for i, f in enumerate(live)
           if i not in consumed and f.get("severity") in ("critical", "warning")]
    return found, missed_planted, fps, trap_hits


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--assertions", type=Path)
    ap.add_argument("--findings", type=Path)
    ap.add_argument("--demo", action="store_true", help="score the bundled sample output")
    args = ap.parse_args()

    if args.demo:
        args.assertions = ROOT / "sample_output" / "assertions.json"
        args.findings = ROOT / "sample_output" / "findings.json"
    if not (args.assertions and args.findings):
        ap.error("provide --assertions and --findings, or --demo")

    acts = json.loads(args.assertions.read_text())["assertions"]
    finds = json.loads(args.findings.read_text())["findings"]
    th = GT["thresholds"]

    recall, matched, missed = score_extraction(acts)
    found, missed_planted, fps, trap_hits = score_findings(finds)
    planted_ids = {e["id"] for e in GT["expected_findings"]}
    planted_found = [f for f in found if f in planted_ids]
    planted_recall = len(planted_found) / len(planted_ids)
    fp_per_ep = len(fps) / th["episode_count"]

    gates = {
        f"extraction recall ≥ {th['extraction_recall_min']:.0%}":
            recall >= th["extraction_recall_min"],
        "all planted errors found (P1–P4)":
            planted_recall >= th["planted_findings_recall_min"],
        f"false positives ≤ {th['false_positives_per_episode_max']}/episode":
            fp_per_ep <= th["false_positives_per_episode_max"],
        "no trap flags fired (T1–T2)":
            not trap_hits,
    }

    print("=" * 62)
    print("CANON AI — PHASE 0 EVAL  (fixtures/greyharbor)")
    print("=" * 62)
    print(f"Extraction recall : {recall:.0%}  ({len(matched)}/{len(GT['expected_assertions'])})"
          + (f"   missed: {', '.join(missed)}" if missed else ""))
    print(f"Planted errors    : {len(planted_found)}/{len(planted_ids)} found"
          + (f"   missed: {', '.join(missed_planted)}" if missed_planted else ""))
    print(f"False positives   : {len(fps)} total ({fp_per_ep:.1f}/episode)")
    for f in fps[:5]:
        print(f"    FP[{f.get('check')}] {f.get('explanation','')[:80]}")
    if trap_hits:
        for tid, txt in trap_hits:
            print(f"    TRAP {tid} fired: {txt}")
    print("-" * 62)
    ok = True
    for name, passed in gates.items():
        print(f"  [{'PASS' if passed else 'FAIL'}] {name}")
        ok &= passed
    print("=" * 62)
    print("PHASE 0 GATES: " + ("ALL PASS ✓" if ok else "NOT YET — see misses above"))
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()

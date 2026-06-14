"""Chatbot baseline for the Canon-vs-LLM benchmark.

The "paste your whole bible into Claude" comparison. Give a frontier LLM the
entire segmented corpus (planted annotations stripped — same text the ingester
feeds Canon) and ask it to do Canon's jobs: find continuity errors. Then score it
with the SAME grader Canon is scored with (eval/run_eval.py). Run it N times to
expose run-to-run variance — the thing Canon is immune to by construction.

This isolates the founder's question: what does the deterministic assertion graph
buy over a long-context chatbot? The gap should widen with corpus size and with
the distance between a fact and its contradiction (cross-episode planted errors).

Usage:
  python bench/baseline.py --files fixtures/greyharbor/*.fountain \\
      --expected eval/expected_assertions.json --runs 3
"""

from __future__ import annotations

import argparse
import glob
import importlib.util
import json
import pathlib
import re
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from canon import extract, ingest  # noqa: E402
from canon.extract import first_text  # noqa: E402

_spec = importlib.util.spec_from_file_location("run_eval", ROOT / "eval" / "run_eval.py")
run_eval = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(run_eval)

CHECK_TYPES = [
    "dead_speaker", "presence_conflict", "premature_knowledge",
    "destroyed_location_use", "capability_violation", "other",
]

SYSTEM_PROMPT = (
    "You are a meticulous continuity editor for a serialized TV show. You will be given the "
    "FULL script of every episode so far. Find every CONTINUITY ERROR: a place where the "
    "script contradicts something the canon established earlier — a dead character speaking "
    "or acting, a destroyed location used again, a character doing something canon established "
    "they cannot do, a character knowing something they were never shown learning, a character "
    "in two places at once. Report ONLY genuine contradictions you can point to in the text, "
    "and for each name the contradicted fact AND its source. Be thorough across ALL episodes — "
    "errors are often separated from what they contradict by several episodes."
)


def errors_schema() -> dict:
    return {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "errors": {
                "type": "array",
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "properties": {
                        "check": {"type": "string", "enum": CHECK_TYPES},
                        "who": {"type": "string"},
                        "where": {"type": "string"},
                        "explanation": {"type": "string"},
                        "contradicts": {"type": "string"},
                    },
                    "required": ["check", "who", "where", "explanation", "contradicts"],
                },
            }
        },
        "required": ["errors"],
    }


# answer-leak tripwire: the corpus handed to the chatbot must never contain a
# planted-error marker. If the ingester ever fails to strip one, fail loudly
# rather than silently feed Claude the answers.
_LEAK_RE = re.compile(r"\[\s*(PLANTED|TRAP)\b", re.IGNORECASE)


def corpus_text(files: list) -> str:
    """Segmented, planted-annotation-stripped corpus — the same text Canon ingests."""
    works = ingest.parse_works(files)
    parts: list = []
    for w in works:
        head = w.title.split()[0]
        parts.append(f"================ {w.title} ================")
        for sc in w.scenes:
            parts.append(f"[{head}/sc{sc.scene_index}]")
            parts.append(sc.raw_text)
    text = "\n\n".join(parts)
    leaked = _LEAK_RE.findall(text)
    if leaked:
        raise RuntimeError(
            f"refusing to run: corpus still contains {len(leaked)} answer marker(s) "
            "([PLANTED]/[TRAP]) — the ingester didn't strip them; this would hand the "
            "chatbot the answers and invalidate the benchmark."
        )
    return text


def detect_errors(client, corpus: str, model: str = extract.DEFAULT_MODEL,
                  effort: str = "high") -> list:
    oc: dict = {"format": {"type": "json_schema", "schema": errors_schema()}}
    if effort:
        oc["effort"] = effort
    resp = client.messages.create(
        model=model, max_tokens=12000, system=SYSTEM_PROMPT,
        messages=[{"role": "user", "content":
                   corpus + "\n\n---\nReturn every continuity error as structured output."}],
        output_config=oc, thinking={"type": "adaptive"},
    )
    text = first_text(resp)
    if text is None:
        raise RuntimeError("no text block in baseline response")
    return json.loads(text)["errors"]


def to_findings(errors: list) -> list:
    """Map the LLM's errors into the findings shape eval/run_eval.py scores."""
    sev = {"capability_violation": "warning"}
    out = []
    for e in errors:
        who = (e.get("who") or "").strip()
        expl = f"{who}: {e.get('explanation', '')} (contradicts: {e.get('contradicts', '')})"
        out.append({
            "check": e.get("check", "other"),
            "severity": sev.get(e.get("check"), "critical"),
            "explanation": expl, "sealed": False,
            "scene_id": None, "assertion_a": None, "assertion_b": None,
        })
    return out


def run(files: list, expected_path: str, runs: int, model: str) -> dict:
    # point the grader at the chosen answer key
    run_eval.GT = json.loads(pathlib.Path(expected_path).read_text())
    corpus = corpus_text(files)
    client = extract.make_client()
    planted_ids = {e["id"] for e in run_eval.GT["expected_findings"]}
    n_planted = len(planted_ids)
    n_eps = run_eval.GT["thresholds"]["episode_count"]

    results = []
    for i in range(runs):
        errors = detect_errors(client, corpus, model=model)
        found, missed, fps, traps = run_eval.score_findings(to_findings(errors))
        planted_found = [f for f in found if f in planted_ids]
        results.append({
            "raw": len(errors),
            "planted_found": sorted(planted_found),
            "missed": sorted(missed),
            "fp": len(fps), "traps": len(traps),
        })
        print(f"  run {i+1}: planted {len(planted_found)}/{n_planted} "
              f"(missed {','.join(r for r in sorted(missed) if r in planted_ids) or '-'}), "
              f"raw claims {len(errors)}, false-positives {len(fps)}, traps fired {len(traps)}")

    # variance: which planted errors were NOT found on every run
    everyrun = set(planted_ids)
    union = set()
    for r in results:
        everyrun &= set(r["planted_found"])
        union |= set(r["planted_found"])
    flaky = sorted(union - everyrun)
    print(f"\n  corpus: {n_eps} episode(s), {len(corpus)} chars | model {model}")
    print(f"  planted found on EVERY run: {len(everyrun)}/{n_planted}  "
          f"| found on some run: {len(union)}/{n_planted}"
          + (f"  | flaky across runs: {','.join(flaky)}" if flaky else "  | (consistent)"))
    avg_fp = sum(r["fp"] for r in results) / len(results)
    print(f"  avg false-positives/run: {avg_fp:.1f} ({avg_fp / n_eps:.1f}/episode)  "
          f"| trap violations: {sum(r['traps'] for r in results)}")
    return {"results": results, "consistent": sorted(everyrun), "flaky": flaky}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--files", nargs="+", required=True)
    ap.add_argument("--expected", default="eval/expected_assertions.json")
    ap.add_argument("--runs", type=int, default=3)
    ap.add_argument("--model", default=extract.DEFAULT_MODEL)
    args = ap.parse_args(argv)
    files = []
    for f in args.files:
        files.extend(sorted(glob.glob(f)) if any(c in f for c in "*?[") else [f])
    print(f"chatbot baseline ({args.model}) — {len(files)} file(s), {args.runs} run(s)")
    run(files, args.expected, args.runs, args.model)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

"""End-to-end benchmark: Canon (deterministic graph) vs a chatbot (full script in
context), graded identically by bench/scorer.py. Prints a comparison + a blog-ready
markdown table, writes a vendor-neutral results JSON, and can push both systems to
Braintrust for the shareable dashboard.

Canon's findings come from a world already loaded in Postgres (ingest -> extract ->
resolve -> store); the chatbot's come from bench/baseline.py. Run Canon ONCE — this
is a held-out set, so no tuning against it.

  python bench/run_benchmark.py --world greyharbor_s1 \\
      --files fixtures/greyharbor/*.fountain \\
      --expected eval/expected_greyharbor_s1.json --runs 3 --braintrust
"""

from __future__ import annotations

import argparse
import glob
import json
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from bench import baseline, scorer  # noqa: E402
from canon import check as canon_check  # noqa: E402
from canon import extract, ingest  # noqa: E402


def canon_findings(world: str, db_url: str, checks_sql: str) -> list:
    conn = ingest.connect(db_url)
    try:
        cur = conn.cursor()
        cur.execute("SELECT id FROM worlds WHERE name = %s", (world,))
        row = cur.fetchone()
        conn.rollback()
        if not row:
            raise RuntimeError(f"world '{world}' not loaded — run ingest+store first")
        findings, errors = canon_check.run_checks(conn, row[0], checks_sql)
        if errors:
            print(f"  (canon check warnings: {errors})", file=sys.stderr)
        return canon_check.to_findings_json(findings)["findings"]
    finally:
        conn.close()


def markdown_table(canon_eval: dict, claude_agg: dict, n_ep: int) -> str:
    cs, l = canon_eval["summary"], claude_agg
    rows = [
        "| metric | Canon | chatbot (Claude) |",
        "|---|---|---|",
        f"| planted errors found | **{cs['planted_found']}/{cs['planted_total']}** "
        f"({cs['recall']:.0%}) | {l['recall_mean']:.0%} avg, "
        f"{l['recall_every_run']:.0%} every run |",
        f"| false positives / episode | {cs['fp_per_episode']} | {l['fp_per_episode_mean']} |",
        f"| traps respected | {cs['traps_respected']}/{cs['traps_total']} | "
        f"{'all' if not l['trap_violations_any'] else 'violated ' + ','.join(l['trap_violations_any'])} |",
        f"| run-to-run consistency | deterministic (identical every run) | "
        f"flaky on {len(l['flaky'])} error(s): {','.join(l['flaky']) or 'none'} |",
        f"| missed | {','.join(cs['missed']) or 'none'} | "
        f"never caught: {','.join(l['never_caught']) or 'none'} |",
    ]
    return "\n".join(rows)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--world", required=True)
    ap.add_argument("--files", nargs="+", required=True)
    ap.add_argument("--expected", default="eval/expected_greyharbor_s1.json")
    ap.add_argument("--runs", type=int, default=3)
    ap.add_argument("--model", default=extract.DEFAULT_MODEL)
    ap.add_argument("--db-url", default=None)
    ap.add_argument("--braintrust", action="store_true", help="also push to Braintrust")
    ap.add_argument("--out", default="bench/results.json")
    args = ap.parse_args(argv)

    files = []
    for f in args.files:
        files.extend(sorted(glob.glob(f)) if any(c in f for c in "*?[") else [f])
    gt = json.loads(pathlib.Path(args.expected).read_text())
    n_ep = gt["thresholds"]["episode_count"]
    db_url = ingest.resolve_db_url(args.db_url)

    print(f"Benchmark: {len(files)} files, {n_ep} episodes, {len(gt['expected_findings'])} planted errors\n")

    print("Canon (deterministic graph):")
    canon_eval = scorer.evaluate(canon_findings(args.world, db_url, (ROOT / "db" / "checks.sql").read_text()), gt)
    cs = canon_eval["summary"]
    print(f"  recall {cs['recall']:.0%} ({cs['planted_found']}/{cs['planted_total']}), "
          f"FP/ep {cs['fp_per_episode']}, traps {cs['traps_respected']}/{cs['traps_total']}, "
          f"missed {cs['missed'] or 'none'}\n")

    print(f"Chatbot baseline ({args.model}), {args.runs} run(s):")
    client = extract.make_client()
    corpus = baseline.corpus_text(files)
    claude_evals = []
    for i in range(args.runs):
        errs = baseline.detect_errors(client, corpus, model=args.model)
        ev = scorer.evaluate(baseline.to_findings(errs), gt)
        claude_evals.append(ev)
        print(f"  run {i+1}: recall {ev['summary']['recall']:.0%} "
              f"({ev['summary']['planted_found']}/{ev['summary']['planted_total']}), "
              f"FP/ep {ev['summary']['fp_per_episode']}, missed {ev['summary']['missed'] or 'none'}")
    claude_agg = scorer.merge_runs(claude_evals)

    table = markdown_table(canon_eval, claude_agg, n_ep)
    print("\n" + table + "\n")

    pathlib.Path(args.out).parent.mkdir(parents=True, exist_ok=True)
    pathlib.Path(args.out).write_text(json.dumps({
        "episodes": n_ep, "model": args.model,
        "canon": canon_eval, "claude_runs": claude_evals, "claude_aggregate": claude_agg,
        "markdown": table,
    }, indent=2))
    print(f"wrote {args.out}")

    if args.braintrust:
        from bench import braintrust_eval as bt
        print("\npushing to Braintrust...")
        bt.push("canon", canon_eval, extra_metadata={"episodes": n_ep, "deterministic": True})
        for i, ev in enumerate(claude_evals, 1):
            bt.push(f"claude-run{i}", ev, extra_metadata={"episodes": n_ep, "model": args.model})
        print(f"  done — project '{bt.DEFAULT_PROJECT}'")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

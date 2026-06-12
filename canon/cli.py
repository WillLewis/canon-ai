"""canon CLI. Phase 0 implements the `ingest` subcommand (extract/ask/check next).

    python -m canon ingest fixtures/greyharbor/*.fountain --world greyharbor --dry-run
    python -m canon ingest fixtures/greyharbor/*.fountain --world greyharbor --reset-world
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

from . import extract as extract_mod
from . import ingest as ingest_mod
from . import resolve as resolve_mod


def cmd_ingest(args: argparse.Namespace) -> int:
    files = ingest_mod.expand_inputs(args.files)
    if not files:
        print("error: no input files found", file=sys.stderr)
        return 2
    missing = [f for f in files if not os.path.isfile(f)]
    if missing:
        print(f"error: file(s) not found: {', '.join(missing)}", file=sys.stderr)
        return 2
    works = ingest_mod.parse_works(files)

    db_url = ingest_mod.resolve_db_url(args.db_url)
    if args.dry_run or db_url is None:
        if db_url is None and not args.dry_run:
            print(
                "note: no database URL set (CANON_DB_URL / DATABASE_URL / --db-url); "
                "showing a dry run.\n",
                file=sys.stderr,
            )
        print(ingest_mod.render_dry_run(args.world, works))
        return 0

    try:
        conn = ingest_mod.connect(db_url)
    except RuntimeError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    try:
        result = ingest_mod.load_into(conn, args.world, works, reset=args.reset_world)
    except RuntimeError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    finally:
        try:
            conn.close()
        except Exception:
            pass

    print(
        f"ingested {result['scenes']} scene(s) across {result['works']} work(s) "
        f"into world '{args.world}' (id={result['world_id']})."
    )
    return 0


def cmd_extract(args: argparse.Namespace) -> int:
    files = ingest_mod.expand_inputs(args.files)
    if not files:
        print("error: no input files found", file=sys.stderr)
        return 2
    missing = [f for f in files if not os.path.isfile(f)]
    if missing:
        print(f"error: file(s) not found: {', '.join(missing)}", file=sys.stderr)
        return 2
    works = ingest_mod.parse_works(files)

    if args.dry_run or not extract_mod.has_credentials():
        if not extract_mod.has_credentials() and not args.dry_run:
            print(
                "note: no API credentials set (ANTHROPIC_API_KEY / ANTHROPIC_AUTH_TOKEN); "
                "showing the prompts that would be sent.\n",
                file=sys.stderr,
            )
        ctxs = extract_mod.scene_contexts(works)
        print(extract_mod.render_dry_run(ctxs, model=args.model, effort=args.effort, limit=args.limit))
        return 0

    try:
        client = extract_mod.make_client()
    except RuntimeError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1

    def on_result(ex: "extract_mod.SceneExtraction") -> None:
        drops = sum(ex.dropped.values())
        tail = f" ({drops} dropped)" if drops else ""
        print(f"  pos {ex.story_position:>3}  {ex.slug}: {len(ex.assertions)} kept{tail}", file=sys.stderr)

    try:
        results = extract_mod.run_extraction(
            client, works,
            model=args.model, effort=args.effort,
            thinking=not args.no_thinking, verify_quotes=not args.no_verify_quotes,
            limit=args.limit, on_result=on_result,
        )
    except RuntimeError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1

    out = extract_mod.to_json(args.world, args.model, results)
    text = json.dumps(out, indent=2, ensure_ascii=False)
    total = sum(len(r.assertions) for r in results)
    if args.out:
        Path(args.out).parent.mkdir(parents=True, exist_ok=True)
        Path(args.out).write_text(text, encoding="utf-8")
        print(f"wrote {args.out}: {len(results)} scene(s), {total} candidate assertion(s).")
    else:
        print(text)
    return 0


def _write_resolution_outputs(state, args) -> None:
    if args.state:
        Path(args.state).parent.mkdir(parents=True, exist_ok=True)
        Path(args.state).write_text(
            json.dumps(resolve_mod.state_to_dict(state), indent=2, ensure_ascii=False),
            encoding="utf-8",
        )
        print(f"wrote {args.state}")
    if args.out:
        Path(args.out).parent.mkdir(parents=True, exist_ok=True)
        Path(args.out).write_text(
            json.dumps(resolve_mod.to_eval_assertions(state), indent=2, ensure_ascii=False),
            encoding="utf-8",
        )
        print(f"wrote {args.out}: {len(state.assertions)} resolved assertion(s)")


def cmd_resolve(args: argparse.Namespace) -> int:
    try:
        candidates = json.loads(Path(args.candidates).read_text(encoding="utf-8"))
    except FileNotFoundError:
        print(f"error: candidates file not found: {args.candidates}", file=sys.stderr)
        return 2

    client = None
    if not args.no_llm:
        if extract_mod.has_credentials():
            try:
                client = extract_mod.make_client()
            except RuntimeError as e:
                print(f"error: {e}", file=sys.stderr)
                return 1
        else:
            print(
                "note: no API credentials (ANTHROPIC_API_KEY / ANTHROPIC_AUTH_TOKEN); "
                "resolving deterministically and queueing the rest for `canon confirm`.\n",
                file=sys.stderr,
            )

    state = resolve_mod.resolve_candidates(
        candidates, world=args.world or "", client=client,
        model=args.model, effort=args.effort,
    )

    if args.state or args.out:
        _write_resolution_outputs(state, args)
    else:
        print(resolve_mod.render_summary(state))
    if state.queue:
        print(f"\n{len(state.queue)} item(s) need confirmation — run `canon confirm`.",
              file=sys.stderr)
    return 0


def _confirm_prompt(item, state) -> str:
    print("\n" + "-" * 60)
    print(f'unresolved: "{item.surface}"  [{item.kind_hint}]  ({item.reason})')
    for o in item.occurrences:
        pred = f", {o['predicate']}" if o.get("predicate") else ""
        print(f"    {o['scene']} ({o['role']}{pred})")
    if item.candidates:
        for i, c in enumerate(item.candidates, 1):
            print(f"    [{i}] {c}")
    known = [e.name for e in state.registry.entities if not e.provisional]
    if known:
        print(f"    existing: {', '.join(known)}")
    print("    actions: <n> pick | = <Name> assign | new <Name> | <kind> | keep | skip")
    try:
        return input("> ").strip()
    except EOFError:
        return "skip"


def cmd_confirm(args: argparse.Namespace) -> int:
    try:
        state = resolve_mod.state_from_dict(json.loads(Path(args.state).read_text(encoding="utf-8")))
    except FileNotFoundError:
        print(f"error: state file not found: {args.state}", file=sys.stderr)
        return 2
    if not state.queue:
        print("confirm queue is empty — nothing to do.")
        return 0
    resolved = resolve_mod.walk_queue(state, _confirm_prompt)
    _write_state_inplace(state, args)
    print(f"\nresolved {resolved}; {len(state.queue)} still queued.")
    return 0


def cmd_merge(args: argparse.Namespace) -> int:
    try:
        state = resolve_mod.state_from_dict(json.loads(Path(args.state).read_text(encoding="utf-8")))
    except FileNotFoundError:
        print(f"error: state file not found: {args.state}", file=sys.stderr)
        return 2
    if not resolve_mod.merge_entities(state, args.keep, args.drop, args.reason):
        print(f"error: could not merge (check names): keep='{args.keep}' drop='{args.drop}'",
              file=sys.stderr)
        return 1
    _write_state_inplace(state, args)
    print(f"merged '{args.drop}' into '{args.keep}'.")
    return 0


def _write_state_inplace(state, args) -> None:
    Path(args.state).write_text(
        json.dumps(resolve_mod.state_to_dict(state), indent=2, ensure_ascii=False),
        encoding="utf-8",
    )
    if getattr(args, "out", None):
        Path(args.out).parent.mkdir(parents=True, exist_ok=True)
        Path(args.out).write_text(
            json.dumps(resolve_mod.to_eval_assertions(state), indent=2, ensure_ascii=False),
            encoding="utf-8",
        )
        print(f"wrote {args.out}: {len(state.assertions)} resolved assertion(s)")


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="canon", description="Canon AI CLI")
    sub = p.add_subparsers(dest="command", required=True)

    ing = sub.add_parser(
        "ingest", help="parse script files into scenes and load them into Postgres"
    )
    ing.add_argument("files", nargs="+", help="Fountain files or directories")
    ing.add_argument("--world", required=True, help="world (show/book) name")
    ing.add_argument(
        "--db-url",
        default=None,
        help="Postgres URL (else CANON_DB_URL / DATABASE_URL; supabase local: "
        "postgresql://postgres:postgres@127.0.0.1:54322/postgres)",
    )
    ing.add_argument(
        "--dry-run", action="store_true", help="parse and print a preview; no DB writes"
    )
    ing.add_argument(
        "--reset-world",
        action="store_true",
        help="delete and recreate the world before loading (clean fixture reload)",
    )
    ing.set_defaults(func=cmd_ingest)

    ext = sub.add_parser(
        "extract",
        help="LLM structured extraction: scenes -> candidate assertions JSON (Stage 2)",
    )
    ext.add_argument("files", nargs="+", help="Fountain files or directories")
    ext.add_argument("--world", required=True, help="world (show/book) name")
    ext.add_argument("--model", default=extract_mod.DEFAULT_MODEL, help="Claude model id")
    ext.add_argument(
        "--effort", default=extract_mod.DEFAULT_EFFORT,
        choices=["low", "medium", "high", "xhigh", "max"], help="thinking/effort level",
    )
    ext.add_argument("--no-thinking", action="store_true", help="disable adaptive thinking")
    ext.add_argument(
        "--no-verify-quotes", action="store_true",
        help="keep assertions whose supporting_quote can't be found in the scene",
    )
    ext.add_argument("--limit", type=int, default=None, help="extract only the first N scenes")
    ext.add_argument("--out", default=None, help="write candidate JSON here (default: stdout)")
    ext.add_argument("--dry-run", action="store_true", help="print prompts; no API calls")
    ext.set_defaults(func=cmd_extract)

    res = sub.add_parser(
        "resolve",
        help="entity resolution: candidate assertions -> resolved assertions + entities (Stage 3)",
    )
    res.add_argument("candidates", help="candidate assertions JSON (from `canon extract`)")
    res.add_argument("--world", default=None, help="world name (default: from the candidates file)")
    res.add_argument("--model", default=extract_mod.DEFAULT_MODEL, help="Claude model id")
    res.add_argument("--effort", default=extract_mod.DEFAULT_EFFORT,
                     choices=["low", "medium", "high", "xhigh", "max"], help="effort level")
    res.add_argument("--no-llm", action="store_true",
                     help="deterministic only (exact/fuzzy); queue everything else")
    res.add_argument("--state", default=None, help="write the resolution state JSON here")
    res.add_argument("--out", default=None, help="write eval-contract assertions JSON here")
    res.set_defaults(func=cmd_resolve)

    con = sub.add_parser("confirm", help="walk the confirm queue in a resolution state file")
    con.add_argument("--state", required=True, help="resolution state JSON (from `canon resolve`)")
    con.add_argument("--out", default=None, help="also re-export eval-contract assertions here")
    con.set_defaults(func=cmd_confirm)

    mrg = sub.add_parser("merge", help="merge two entities (human-only collision resolution)")
    mrg.add_argument("--state", required=True, help="resolution state JSON")
    mrg.add_argument("--keep", required=True, help="canonical name (or alias) of the entity to keep")
    mrg.add_argument("--drop", required=True, help="entity to fold into --keep")
    mrg.add_argument("--reason", default="manual merge", help="provenance note")
    mrg.add_argument("--out", default=None, help="also re-export eval-contract assertions here")
    mrg.set_defaults(func=cmd_merge)
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return args.func(args)

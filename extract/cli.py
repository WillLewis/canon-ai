"""Command line interface for the P0-EXTRACT workstream."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from . import extraction as extraction_mod
from . import gate as gate_mod
from . import resolution as resolution_mod
from .llm import DEFAULT_EFFORT, DEFAULT_MODEL, has_credentials, make_client
from .models import load_scenes
from .resolution import state_from_dict, state_to_dict
from .schema import AUTO_ACCEPT_CONFIDENCE


def _load_json(path: str | Path) -> dict[str, Any]:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def _write_json(path: str | Path, payload: dict[str, Any]) -> None:
    out = Path(path)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")


def cmd_extract(args: argparse.Namespace) -> int:
    try:
        input_world, scenes = load_scenes(args.scenes)
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        print(f"error: could not load scenes: {exc}", file=sys.stderr)
        return 2

    world = args.world or input_world
    if args.dry_run or not has_credentials():
        if not has_credentials() and not args.dry_run:
            print(
                "note: no Anthropic credentials set; showing prompts instead of calling the API.\n",
                file=sys.stderr,
            )
        print(extraction_mod.render_dry_run(scenes, model=args.model, effort=args.effort, limit=args.limit))
        return 0

    try:
        client = make_client()
    except RuntimeError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    def on_result(result) -> None:
        drops = sum(result.dropped.values())
        suffix = f" ({drops} dropped)" if drops else ""
        print(f"  {result.scene_id}: {len(result.assertions)} kept{suffix}", file=sys.stderr)

    try:
        results = extraction_mod.run_extraction(
            client,
            scenes,
            model=args.model,
            effort=args.effort,
            thinking=not args.no_thinking,
            verify_quotes=not args.no_verify_quotes,
            limit=args.limit,
            on_result=on_result,
        )
    except (RuntimeError, json.JSONDecodeError) as exc:
        print(f"error: extraction failed: {exc}", file=sys.stderr)
        return 1

    payload = extraction_mod.to_candidate_json(world, args.model, results)
    if args.out:
        _write_json(args.out, payload)
        total = sum(len(scene.assertions) for scene in results)
        print(f"wrote {args.out}: {len(results)} scene(s), {total} candidate assertion(s).")
    else:
        print(json.dumps(payload, indent=2, ensure_ascii=False))
    return 0


def cmd_resolve(args: argparse.Namespace) -> int:
    try:
        candidates = _load_json(args.candidates)
        alias_table = _load_json(args.aliases) if args.aliases else None
    except (OSError, json.JSONDecodeError) as exc:
        print(f"error: could not load input JSON: {exc}", file=sys.stderr)
        return 2

    client = None
    if not args.no_llm:
        if has_credentials():
            try:
                client = make_client()
            except RuntimeError as exc:
                print(f"error: {exc}", file=sys.stderr)
                return 1
        else:
            print(
                "note: no Anthropic credentials; using exact/fuzzy resolution and queueing the rest.\n",
                file=sys.stderr,
            )

    try:
        state = resolution_mod.resolve_candidates(
            candidates,
            world=args.world or "",
            alias_table=alias_table,
            client=client,
            model=args.model,
            effort=args.effort,
            thinking=not args.no_thinking,
            conf_auto=args.conf_auto,
        )
    except RuntimeError as exc:
        print(f"error: resolution failed: {exc}", file=sys.stderr)
        return 1

    if args.state:
        _write_json(args.state, state_to_dict(state))
        print(f"wrote {args.state}")
    if args.out:
        _write_json(args.out, resolution_mod.to_resolved_assertions(state))
        print(f"wrote {args.out}: {len(state.assertions)} resolved assertion(s)")
    if not args.state and not args.out:
        print(resolution_mod.render_summary(state))
    if state.entity_queue:
        print(f"{len(state.entity_queue)} entity item(s) need confirmation.", file=sys.stderr)
    return 0


def _write_state_and_outputs(state, args) -> None:
    _write_json(args.state, state_to_dict(state))
    print(f"wrote {args.state}")
    if getattr(args, "out", None):
        _write_json(args.out, resolution_mod.to_resolved_assertions(state))
        print(f"wrote {args.out}: {len(state.assertions)} assertion(s)")
    if getattr(args, "eval_out", None):
        _write_json(args.eval_out, gate_mod.to_eval_assertions(state))
        print(f"wrote {args.eval_out}: eval assertion contract")


def cmd_gate(args: argparse.Namespace) -> int:
    try:
        state = state_from_dict(_load_json(args.state))
    except (OSError, json.JSONDecodeError, TypeError, KeyError) as exc:
        print(f"error: could not load state: {exc}", file=sys.stderr)
        return 2
    gate_mod.gate_state(state, threshold=args.threshold)
    _write_state_and_outputs(state, args)
    if state.assertion_queue:
        print(f"{len(state.assertion_queue)} assertion(s) need confirmation.", file=sys.stderr)
    return 0


def _prompt_entity(item, state) -> str:
    print("\n" + "-" * 70)
    print(f'unresolved entity: "{item.surface}" [{item.kind_hint}] ({item.reason})')
    for occurrence in item.occurrences:
        pred = f", {occurrence['predicate']}" if occurrence.get("predicate") else ""
        print(f"  {occurrence['scene']} ({occurrence['role']}{pred})")
        if occurrence.get("quote"):
            print(f"    quote: {occurrence['quote']}")
    if item.candidates:
        for idx, candidate in enumerate(item.candidates, start=1):
            print(f"  [{idx}] {candidate}")
    known = [entity.name for entity in state.registry.entities if not entity.provisional]
    if known:
        print(f"  existing: {', '.join(known)}")
    print("  actions: <n> pick | = <Name> assign | new <Name> | <kind> | keep | skip")
    try:
        return input("> ").strip()
    except EOFError:
        return "skip"


def cmd_confirm_entities(args: argparse.Namespace) -> int:
    try:
        state = state_from_dict(_load_json(args.state))
    except (OSError, json.JSONDecodeError, TypeError, KeyError) as exc:
        print(f"error: could not load state: {exc}", file=sys.stderr)
        return 2
    if not state.entity_queue:
        print("entity queue is empty.")
        return 0
    resolved = resolution_mod.walk_entity_queue(state, _prompt_entity)
    _write_state_and_outputs(state, args)
    print(f"resolved {resolved}; {len(state.entity_queue)} entity item(s) still queued.")
    return 0


def _prompt_assertion(item, _state) -> str:
    print("\n" + "-" * 70)
    print(f"low-confidence assertion #{item['assertion_index']} ({item['confidence']:.2f})")
    print(f"  scene: {item.get('scene') or item.get('scene_id')}")
    print(f"  assertion: {item['summary']}")
    print(f"  quote: {item.get('supporting_quote')}")
    print('  actions: accept | reject | edit {"field": "value"} | skip')
    try:
        return input("> ").strip()
    except EOFError:
        return "skip"


def cmd_confirm_assertions(args: argparse.Namespace) -> int:
    try:
        state = state_from_dict(_load_json(args.state))
    except (OSError, json.JSONDecodeError, TypeError, KeyError) as exc:
        print(f"error: could not load state: {exc}", file=sys.stderr)
        return 2
    if not state.assertion_queue:
        print("assertion queue is empty.")
        return 0
    resolved = gate_mod.walk_assertion_queue(state, _prompt_assertion)
    _write_state_and_outputs(state, args)
    print(f"resolved {resolved}; {len(state.assertion_queue)} assertion(s) still queued.")
    return 0


def cmd_merge(args: argparse.Namespace) -> int:
    try:
        state = state_from_dict(_load_json(args.state))
    except (OSError, json.JSONDecodeError, TypeError, KeyError) as exc:
        print(f"error: could not load state: {exc}", file=sys.stderr)
        return 2
    if not resolution_mod.merge_entities(state, args.keep, args.drop, args.reason):
        print(f"error: could not merge keep={args.keep!r} drop={args.drop!r}", file=sys.stderr)
        return 1
    _write_state_and_outputs(state, args)
    print(f"merged {args.drop!r} into {args.keep!r}.")
    return 0


def cmd_summary(args: argparse.Namespace) -> int:
    try:
        state = state_from_dict(_load_json(args.state))
    except (OSError, json.JSONDecodeError, TypeError, KeyError) as exc:
        print(f"error: could not load state: {exc}", file=sys.stderr)
        return 2
    print(resolution_mod.render_summary(state))
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m extract", description="Canon AI P0 extraction workstream")
    sub = parser.add_subparsers(dest="command", required=True)

    ext = sub.add_parser("extract", help="scene JSON -> candidate assertion JSON via Anthropic structured outputs")
    ext.add_argument("scenes", help="scene records JSON")
    ext.add_argument("--world", default=None, help="world name (default: from scene JSON)")
    ext.add_argument("--model", default=DEFAULT_MODEL)
    ext.add_argument("--effort", default=DEFAULT_EFFORT, choices=["low", "medium", "high", "xhigh", "max"])
    ext.add_argument("--no-thinking", action="store_true")
    ext.add_argument("--no-verify-quotes", action="store_true")
    ext.add_argument("--limit", type=int, default=None)
    ext.add_argument("--out", default=None)
    ext.add_argument("--dry-run", action="store_true")
    ext.set_defaults(func=cmd_extract)

    res = sub.add_parser("resolve", help="candidate assertion JSON -> resolved entity/assertion state")
    res.add_argument("candidates", help="candidate JSON from extract")
    res.add_argument("--aliases", default=None, help="optional seeded entity/alias table JSON")
    res.add_argument("--world", default=None)
    res.add_argument("--model", default=DEFAULT_MODEL)
    res.add_argument("--effort", default=DEFAULT_EFFORT, choices=["low", "medium", "high", "xhigh", "max"])
    res.add_argument("--no-thinking", action="store_true")
    res.add_argument("--no-llm", action="store_true", help="exact/fuzzy only; queue ambiguous role refs")
    res.add_argument("--conf-auto", type=float, default=AUTO_ACCEPT_CONFIDENCE)
    res.add_argument("--state", default=None, help="write full resolution state JSON")
    res.add_argument("--out", default=None, help="write resolved assertions JSON")
    res.set_defaults(func=cmd_resolve)

    gate = sub.add_parser("gate", help="apply confidence gate to a resolution state")
    gate.add_argument("--state", required=True)
    gate.add_argument("--threshold", type=float, default=AUTO_ACCEPT_CONFIDENCE)
    gate.add_argument("--out", default=None, help="write resolved assertions JSON")
    gate.add_argument("--eval-out", default=None, help="write eval/run_eval.py assertion contract")
    gate.set_defaults(func=cmd_gate)

    ce = sub.add_parser("confirm-entities", help="walk entity confirmation queue")
    ce.add_argument("--state", required=True)
    ce.add_argument("--out", default=None)
    ce.add_argument("--eval-out", default=None)
    ce.set_defaults(func=cmd_confirm_entities)

    ca = sub.add_parser("confirm-assertions", help="walk low-confidence assertion queue")
    ca.add_argument("--state", required=True)
    ca.add_argument("--out", default=None)
    ca.add_argument("--eval-out", default=None)
    ca.set_defaults(func=cmd_confirm_assertions)

    merge = sub.add_parser("merge", help="human-only entity merge")
    merge.add_argument("--state", required=True)
    merge.add_argument("--keep", required=True)
    merge.add_argument("--drop", required=True)
    merge.add_argument("--reason", default="manual merge")
    merge.add_argument("--out", default=None)
    merge.add_argument("--eval-out", default=None)
    merge.set_defaults(func=cmd_merge)

    summary = sub.add_parser("summary", help="summarize a resolution state")
    summary.add_argument("--state", required=True)
    summary.set_defaults(func=cmd_summary)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return args.func(args)

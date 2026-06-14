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

from . import ask as ask_mod
from . import check as check_mod
from . import extract as extract_mod
from . import holes as holes_mod
from . import ingest as ingest_mod
from . import resolve as resolve_mod
from . import store as store_mod


def cmd_ingest(args: argparse.Namespace) -> int:
    files = ingest_mod.expand_inputs(args.files)
    if not files:
        print("error: no input files found", file=sys.stderr)
        return 2
    missing = [f for f in files if not os.path.isfile(f)]
    if missing:
        print(f"error: file(s) not found: {', '.join(missing)}", file=sys.stderr)
        return 2
    try:
        works = ingest_mod.parse_works(files)
    except RuntimeError as e:
        print(f"error: {e}", file=sys.stderr)
        return 2

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
    try:
        works = ingest_mod.parse_works(files)
    except RuntimeError as e:
        print(f"error: {e}", file=sys.stderr)
        return 2

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


def cmd_store(args: argparse.Namespace) -> int:
    try:
        state_dict = json.loads(Path(args.state).read_text(encoding="utf-8"))
    except FileNotFoundError:
        print(f"error: state file not found: {args.state}", file=sys.stderr)
        return 2

    db_url = ingest_mod.resolve_db_url(args.db_url)
    if args.dry_run or db_url is None:
        if db_url is None and not args.dry_run:
            print(
                "note: no database URL set (CANON_DB_URL / DATABASE_URL / --db-url); "
                "showing a dry run.\n",
                file=sys.stderr,
            )
        print(store_mod.render_dry_run(state_dict, args.world, args.conf_canon))
        return 0

    try:
        conn = ingest_mod.connect(db_url)
    except RuntimeError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    try:
        r = store_mod.store_state(conn, args.world, state_dict,
                                  reset=args.reset, conf_canon=args.conf_canon)
    except RuntimeError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    finally:
        try:
            conn.close()
        except Exception:
            pass

    print(
        f"loaded into world '{args.world}' (id={r['world_id']}): "
        f"{r['entities']} entities, {r['aliases']} aliases, {r['assertions']} assertions, "
        f"{r['scene_presence']} scene-presence rows, {r['character_locations']} character-locations"
        + (f"  ({r['skipped']} assertion(s) skipped — unknown subject/scene)" if r["skipped"] else "")
    )
    return 0


def cmd_check(args: argparse.Namespace) -> int:
    db_url = ingest_mod.resolve_db_url(args.db_url)
    if db_url is None:
        print("error: check requires a database (set CANON_DB_URL / DATABASE_URL / --db-url)",
              file=sys.stderr)
        return 2
    try:
        checks_sql = Path(args.checks).read_text(encoding="utf-8")
    except FileNotFoundError:
        print(f"error: checks file not found: {args.checks}", file=sys.stderr)
        return 2
    try:
        conn = ingest_mod.connect(db_url)
    except RuntimeError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    try:
        cur = conn.cursor()
        cur.execute("SELECT id FROM worlds WHERE name = %s", (args.world,))
        row = cur.fetchone()
        if not row:
            print(f"error: world '{args.world}' not found — run `canon ingest`/`canon store` first.",
                  file=sys.stderr)
            return 1
        world_id = row[0]
        findings, errors = check_mod.run_checks(conn, world_id, checks_sql)
        if not args.no_persist:
            check_mod.persist_findings(conn, world_id, findings)
    finally:
        try:
            conn.close()
        except Exception:
            pass

    for name, err in errors:
        print(f"warning: check '{name}' errored: {err}", file=sys.stderr)
    if args.out:
        Path(args.out).parent.mkdir(parents=True, exist_ok=True)
        Path(args.out).write_text(
            json.dumps(check_mod.to_findings_json(findings), indent=2, ensure_ascii=False),
            encoding="utf-8",
        )
        live = sum(1 for f in findings if not f["sealed"])
        print(f"wrote {args.out}: {len(findings)} finding(s) ({live} live)")
    else:
        print(check_mod.render_report(findings))
    return 0


def cmd_ask(args: argparse.Namespace) -> int:
    if args.dry_run:
        print("=== SYSTEM PROMPT ===")
        print(ask_mod.ASK_SYSTEM_PROMPT)
        print("=== USER PROMPT ===")
        print(ask_mod.build_sql_prompt(args.question))
        return 0

    db_url = ingest_mod.resolve_db_url(args.db_url)
    if db_url is None:
        print("error: ask requires a database (set CANON_DB_URL / DATABASE_URL / --db-url)",
              file=sys.stderr)
        return 2

    client = None
    if args.sql is None:
        if not extract_mod.has_credentials():
            print(
                "error: ask needs API credentials to write SQL from your question "
                "(ANTHROPIC_API_KEY / ANTHROPIC_AUTH_TOKEN). Alternatives: --sql to "
                "run a query directly, or --dry-run to inspect the prompts.",
                file=sys.stderr,
            )
            return 2
        try:
            client = extract_mod.make_client()
        except RuntimeError as e:
            print(f"error: {e}", file=sys.stderr)
            return 1

    try:
        conn = ingest_mod.connect(db_url)
    except RuntimeError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    try:
        cur = conn.cursor()
        cur.execute("SELECT id FROM worlds WHERE name = %s", (args.world,))
        row = cur.fetchone()
        conn.rollback()
        if not row:
            print(f"error: world '{args.world}' not found — run the pipeline first.", file=sys.stderr)
            return 1
        result = ask_mod.ask(
            conn, client, args.question, row[0],
            model=args.model, effort=args.effort, limit=args.limit,
            sql_override=args.sql, do_narrate=not args.no_narrate,
        )
    finally:
        try:
            conn.close()
        except Exception:
            pass

    print(ask_mod.render_answer(result, args.question, show_sql=args.show_sql))
    return 0 if result["status"] in ("answered", "no_support") else 2


def cmd_holes(args: argparse.Namespace) -> int:
    from pathlib import Path as _Path

    db_url = ingest_mod.resolve_db_url(args.db_url)
    if db_url is None:
        print("error: holes requires a database (set CANON_DB_URL / DATABASE_URL / --db-url)",
              file=sys.stderr)
        return 2
    try:
        conn = ingest_mod.connect(db_url)
    except RuntimeError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    try:
        cur = conn.cursor()
        cur.execute("SELECT id FROM worlds WHERE name = %s", (args.world,))
        row = cur.fetchone()
        conn.rollback()
        if not row:
            print(f"error: world '{args.world}' not found — load it first.", file=sys.stderr)
            return 1
        holes_sql = _Path(args.holes).read_text(encoding="utf-8")
        found, errors = holes_mod.run_holes(conn, row[0], holes_sql)
    finally:
        try:
            conn.close()
        except Exception:
            pass

    for err in errors:
        print(f"warning: hole query failed: {err}", file=sys.stderr)
    print(holes_mod.render_text(args.world, found))

    if args.html:
        out = _Path(args.html)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(holes_mod.render_html(args.world, found, args.stamp or ""), encoding="utf-8")
        print(f"\nwrote {out}  ({len(found)} question(s))")
        if args.open:
            import subprocess
            opener = "open" if sys.platform == "darwin" else "xdg-open"
            try:
                subprocess.run([opener, str(out)], check=False)
            except FileNotFoundError:
                print(f"(could not auto-open; open {out} manually)", file=sys.stderr)
    return 0


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

    sto = sub.add_parser(
        "store", help="load resolved entities + assertions into Postgres (after `canon ingest`)",
    )
    sto.add_argument("--state", required=True, help="resolution state JSON (from `canon resolve`)")
    sto.add_argument("--world", required=True, help="world name (its scenes must already be ingested)")
    sto.add_argument("--db-url", default=None,
                     help="Postgres URL (else CANON_DB_URL / DATABASE_URL)")
    sto.add_argument("--reset", action="store_true",
                     help="clear this world's entities/assertions/scene_presence before loading")
    sto.add_argument("--conf-canon", type=float, default=store_mod.CONF_CANON,
                     help="confidence at/above which an assertion loads as 'canon' (else 'draft')")
    sto.add_argument("--dry-run", action="store_true", help="summarize; no database writes")
    sto.set_defaults(func=cmd_store)

    chk = sub.add_parser("check", help="run db/checks.sql over a world's graph -> findings (Stage 5)")
    chk.add_argument("--world", required=True, help="world name (must be loaded via `canon store`)")
    chk.add_argument("--db-url", default=None, help="Postgres URL (else CANON_DB_URL / DATABASE_URL)")
    chk.add_argument("--checks", default="db/checks.sql", help="path to the checks SQL file")
    chk.add_argument("--out", default=None, help="write eval-contract findings JSON here (default: report)")
    chk.add_argument("--no-persist", action="store_true", help="don't write rows to the findings table")
    chk.set_defaults(func=cmd_check)

    ask = sub.add_parser(
        "ask", help="ask the bible: NL question -> SQL -> cited answer (refuses uncited)",
    )
    ask.add_argument("question", help="natural-language question about your canon")
    ask.add_argument("--world", required=True, help="world name (must be loaded)")
    ask.add_argument("--db-url", default=None, help="Postgres URL (else CANON_DB_URL / DATABASE_URL)")
    ask.add_argument("--model", default=extract_mod.DEFAULT_MODEL, help="Claude model id")
    ask.add_argument("--effort", default=extract_mod.DEFAULT_EFFORT,
                     choices=["low", "medium", "high", "xhigh", "max"], help="effort level")
    ask.add_argument("--limit", type=int, default=ask_mod.MAX_ROWS, help="max rows")
    ask.add_argument("--sql", default=None,
                     help="run this SQL directly (skips the LLM; same guards + citation rules)")
    ask.add_argument("--no-narrate", action="store_true",
                     help="skip LLM narration; deterministic row rendering only")
    ask.add_argument("--show-sql", action="store_true", help="print the executed SQL")
    ask.add_argument("--dry-run", action="store_true", help="print the prompts; no DB, no API")
    ask.set_defaults(func=cmd_ask)

    hol = sub.add_parser(
        "holes", help="hole-finder: questions your world doc doesn't answer (gaps over the graph)",
    )
    hol.add_argument("--world", required=True, help="world name (must be loaded)")
    hol.add_argument("--db-url", default=None, help="Postgres URL (else CANON_DB_URL / DATABASE_URL)")
    hol.add_argument("--holes", default="db/holes.sql", help="path to the hole queries")
    hol.add_argument("--html", default=None, help="also write a self-contained HTML report here")
    hol.add_argument("--open", action="store_true", help="open the HTML report in a browser")
    hol.add_argument("--stamp", default=None, help="optional date stamp shown in the report")
    hol.set_defaults(func=cmd_holes)
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return args.func(args)

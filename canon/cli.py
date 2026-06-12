"""canon CLI. Phase 0 implements the `ingest` subcommand (extract/ask/check next).

    python -m canon ingest fixtures/greyharbor/*.fountain --world greyharbor --dry-run
    python -m canon ingest fixtures/greyharbor/*.fountain --world greyharbor --reset-world
"""

from __future__ import annotations

import argparse
import os
import sys

from . import ingest as ingest_mod


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
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return args.func(args)

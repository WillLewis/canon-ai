"""Command line entrypoint for `python -m ask`."""

from __future__ import annotations

import argparse
import json
import sys

from canon import ingest as ingest_mod

from .engine import ask_question, render_answer, world_id_for_name


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="python -m ask",
        description="Ask the Bible: natural-language question -> read-only SQL -> cited answer.",
    )
    p.add_argument("question", help="natural-language question about the loaded canon graph")
    p.add_argument("--world", required=True, help="world name already loaded in Postgres")
    p.add_argument("--db-url", default=None, help="Postgres URL, else CANON_DB_URL / DATABASE_URL")
    p.add_argument("--limit", type=int, default=50, help="maximum cited rows to render")
    p.add_argument("--show-sql", action="store_true", help="show the generated SQL template")
    p.add_argument("--json", action="store_true", help="emit a machine-readable result")
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    db_url = ingest_mod.resolve_db_url(args.db_url)
    if db_url is None:
        print(
            "error: ask requires a database (set CANON_DB_URL / DATABASE_URL / --db-url)",
            file=sys.stderr,
        )
        return 2

    try:
        conn = ingest_mod.connect(db_url)
    except RuntimeError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1

    try:
        world_id = world_id_for_name(conn, args.world)
        if world_id is None:
            print(f"error: world '{args.world}' not found", file=sys.stderr)
            return 1
        result = ask_question(conn, world_id, args.question, limit=args.limit)
    finally:
        try:
            conn.close()
        except Exception:
            pass

    if args.json:
        print(json.dumps(result.to_dict(), indent=2, ensure_ascii=False))
    else:
        print(render_answer(result, show_sql=args.show_sql))

    return 0 if result.status in ("answered", "no_support") else 2

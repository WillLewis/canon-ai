"""`canon export` subcommand — bible/report to markdown or print-ready HTML.

Registered from canon/cli.py (one-line hook there; all export logic lives in
this package). The whole path is deterministic: SQL assembly + rendering, no
LLM calls and no API credentials needed. The report artifact renders the
already-persisted coverage_notes rows plus a fresh db/checks.sql run — it
never regenerates notes.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from .. import check as check_mod
from .. import ingest as ingest_mod
from .. import report as report_mod
from .bible import build_bible
from .render import render_bible_html, render_bible_markdown, render_pdf, render_report_html


def load_coverage_notes(conn, world_id: int) -> list[dict]:
    """Persisted Reader's Report notes for a world (open ones render)."""

    import json

    cur = conn.cursor()
    try:
        cur.execute(
            "SELECT note_key, family, summary, body, evidence, salience, status "
            "FROM coverage_notes WHERE world_id = %s",
            (world_id,),
        )
        rows = cur.fetchall()
    except Exception:
        try:
            conn.rollback()
        except Exception:
            pass
        return []
    notes = []
    for note_key, family, summary, body, evidence, salience, status in rows:
        if isinstance(evidence, str):
            try:
                evidence = json.loads(evidence)
            except ValueError:
                evidence = {}
        notes.append({
            "note_key": note_key,
            "family": family,
            "summary": summary,
            "body": body,
            "evidence": evidence or {},
            "salience": salience or 0,
            "status": status,
        })
    return notes


def _world_id(conn, world_name: str) -> int | None:
    cur = conn.cursor()
    cur.execute("SELECT id FROM worlds WHERE name = %s", (world_name,))
    row = cur.fetchone()
    try:
        conn.rollback()
    except Exception:
        pass
    return row[0] if row else None


def _read_optional_sql(path: str, label: str) -> str | None:
    p = Path(path)
    if not p.is_file():
        print(f"warning: {label} file not found at {path}; skipping that section.", file=sys.stderr)
        return None
    return p.read_text(encoding="utf-8")


def cmd_export(args: argparse.Namespace) -> int:
    db_url = ingest_mod.resolve_db_url(args.db_url)
    if db_url is None:
        print(
            "error: export requires a database (set CANON_DB_URL / DATABASE_URL / --db-url)",
            file=sys.stderr,
        )
        return 2

    try:
        conn = ingest_mod.connect(db_url)
    except RuntimeError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1

    try:
        world_id = _world_id(conn, args.world)
        if world_id is None:
            print(f"error: world '{args.world}' not found — load it first.", file=sys.stderr)
            return 1

        if args.artifact == "bible":
            holes_sql = _read_optional_sql(args.holes, "holes SQL")
            bible, hole_errors = build_bible(conn, world_id, holes_sql=holes_sql)
            for err in hole_errors:
                print(f"warning: hole query failed: {err}", file=sys.stderr)
            markdown = render_bible_markdown(bible)
            html_text = render_bible_html(bible)
        else:  # report
            checks_sql = _read_optional_sql(args.checks, "checks SQL")
            findings: list[dict] = []
            if checks_sql:
                findings, check_errors = check_mod.run_checks(conn, world_id, checks_sql)
                for name, err in check_errors:
                    print(f"warning: check '{name}' errored: {err}", file=sys.stderr)
            snapshot = report_mod.load_world_snapshot(conn, world_id)
            notes = load_coverage_notes(conn, world_id)
            markdown = report_mod.render_markdown_report(args.world, snapshot, findings, notes)
            html_text = render_report_html(args.world, markdown)
    finally:
        try:
            conn.close()
        except Exception:
            pass

    text = markdown if args.format == "md" else html_text
    if args.out:
        out = Path(args.out)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(text, encoding="utf-8")
        print(f"wrote {out} ({args.artifact}, {args.format})")
    else:
        print(text)

    if args.pdf:
        try:
            Path(args.pdf).parent.mkdir(parents=True, exist_ok=True)
            render_pdf(html_text, args.pdf)
            print(f"wrote {args.pdf} (pdf)")
        except RuntimeError as e:
            print(f"error: {e}", file=sys.stderr)
            return 1
    return 0


def register(sub) -> None:
    """Attach the `export` subcommand to the canon CLI's subparsers."""

    exp = sub.add_parser(
        "export",
        help="export a shareable artifact: series bible or Reader's Report (md/html)",
    )
    exp.add_argument("artifact", choices=["bible", "report"], help="which artifact to export")
    exp.add_argument("--world", required=True, help="world name (must be loaded)")
    exp.add_argument("--format", choices=["md", "html"], default="md",
                     help="markdown or standalone print-ready HTML (default: md)")
    exp.add_argument("--out", default=None, help="write the artifact here (default: stdout)")
    exp.add_argument("--db-url", default=None,
                     help="Postgres URL (else CANON_DB_URL / DATABASE_URL)")
    exp.add_argument("--holes", default="db/holes.sql",
                     help="gap queries powering the bible's Open Questions section")
    exp.add_argument("--checks", default="db/checks.sql",
                     help="checks SQL powering the report's Continuity Findings section")
    exp.add_argument("--pdf", default=None,
                     help="also write a PDF here (optional; requires weasyprint, "
                          "otherwise use the browser's print-to-PDF on --format html)")
    exp.set_defaults(func=cmd_export)

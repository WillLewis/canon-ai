"""`canon export` CLI tests — arg parsing and the no-LLM guarantee.

Offline: parsing only (no database, no network). Also enforces the
grep-level guarantee that nothing under canon/export/ imports anthropic
or calls an LLM.
"""

import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import pytest  # noqa: E402

from canon import cli  # noqa: E402
from canon.export import cli as export_cli  # noqa: E402


def test_export_bible_args_parse_with_defaults():
    args = cli.build_parser().parse_args(["export", "bible", "--world", "greyharbor"])
    assert args.func is export_cli.cmd_export
    assert args.artifact == "bible"
    assert args.world == "greyharbor"
    assert args.format == "md"
    assert args.out is None
    assert args.holes == "db/holes.sql"
    assert args.checks == "db/checks.sql"
    assert args.pdf is None


def test_export_report_html_with_out_path():
    args = cli.build_parser().parse_args(
        ["export", "report", "--world", "w", "--format", "html", "--out", "artifacts/r.html"]
    )
    assert args.artifact == "report"
    assert args.format == "html"
    assert args.out == "artifacts/r.html"


def test_export_rejects_unknown_artifact_and_format():
    parser = cli.build_parser()
    with pytest.raises(SystemExit):
        parser.parse_args(["export", "synopsis", "--world", "w"])  # never a generator
    with pytest.raises(SystemExit):
        parser.parse_args(["export", "bible", "--world", "w", "--format", "pdf"])


def test_export_requires_world():
    with pytest.raises(SystemExit):
        cli.build_parser().parse_args(["export", "bible"])


def test_export_without_database_exits_2(monkeypatch, capsys):
    for var in ("CANON_DB_URL", "DATABASE_URL"):
        monkeypatch.delenv(var, raising=False)
    args = cli.build_parser().parse_args(["export", "bible", "--world", "w"])
    assert args.func(args) == 2
    assert "requires a database" in capsys.readouterr().err


def test_no_llm_or_anthropic_anywhere_under_canon_export():
    export_dir = ROOT / "canon" / "export"
    hits = []
    for path in export_dir.rglob("*.py"):
        text = path.read_text(encoding="utf-8").lower()
        for needle in ("anthropic", "make_client", "messages.create", "llm("):
            if needle in text:
                hits.append((path.name, needle))
    assert hits == [], f"LLM machinery leaked into canon/export/: {hits}"

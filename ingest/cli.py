from __future__ import annotations

import argparse
import sys
from pathlib import Path

from .pipeline import ingest_files, records_to_json


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m ingest",
        description="Segment Fountain, PDF, and docx scripts into Canon scene-record JSON.",
    )
    parser.add_argument("files", nargs="+", help="script files, directories, or shell globs")
    parser.add_argument("--out", help="write JSON to this path instead of stdout")
    parser.add_argument(
        "--compact",
        action="store_true",
        help="emit compact JSON instead of pretty-printed JSON",
    )
    parser.add_argument(
        "--llm-fallback",
        action="store_true",
        help="allow API-based re-segmentation if PDF/docx layout parsing finds no scenes",
    )
    parser.add_argument(
        "--llm-model",
        help="Anthropic model to use for LLM re-segmentation fallback",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        records = ingest_files(
            args.files,
            llm_fallback=args.llm_fallback,
            llm_model=args.llm_model,
        )
    except RuntimeError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    text = records_to_json(records, indent=None if args.compact else 2)
    if args.out:
        path = Path(args.out)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text + "\n", encoding="utf-8")
        print(f"wrote {path}: {len(records)} scene record(s)", file=sys.stderr)
    else:
        print(text)
    return 0

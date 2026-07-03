from __future__ import annotations

import glob
import json
import os
import re
from dataclasses import asdict
from pathlib import Path

from .fountain import parse_fountain
from .models import ParsedWork, SceneRecord

SCRIPT_EXTS = (".fountain", ".fdx", ".pdf", ".docx")


def expand_inputs(paths: list[str]) -> list[str]:
    files: list[str] = []
    for path in paths:
        if os.path.isdir(path):
            for ext in SCRIPT_EXTS:
                files.extend(sorted(glob.glob(os.path.join(path, f"*{ext}"))))
        else:
            matches = glob.glob(path)
            files.extend(sorted(matches) if matches else [path])

    seen: set[str] = set()
    deduped: list[str] = []
    for file in files:
        if file not in seen:
            seen.add(file)
            deduped.append(file)
    return deduped


def parse_one(
    path: str,
    llm_fallback: bool = False,
    llm_model: str | None = None,
) -> ParsedWork:
    ext = Path(path).suffix.lower()
    if ext == ".fountain":
        return parse_fountain(Path(path).read_text(encoding="utf-8"), source_file=path)
    if ext == ".fdx":
        from .fdx import parse_fdx

        return parse_fdx(path)
    if ext == ".pdf":
        from .documents import parse_pdf

        return parse_pdf(path, llm_fallback=llm_fallback, llm_model=llm_model)
    if ext == ".docx":
        from .documents import parse_docx

        return parse_docx(path, llm_fallback=llm_fallback, llm_model=llm_model)
    raise RuntimeError(
        f"unsupported script format '{ext}' ({path}); supported: {', '.join(SCRIPT_EXTS)}"
    )


def order_works(works: list[ParsedWork]) -> list[ParsedWork]:
    def key(work: ParsedWork) -> tuple[int, int | str]:
        if work.episode is not None:
            return (0, work.episode)
        return (1, work.source_file)

    ordered = sorted(works, key=key)
    for index, work in enumerate(ordered, start=1):
        if work.sort_order is None:
            work.sort_order = index
    return ordered


def parse_works(
    files: list[str],
    llm_fallback: bool = False,
    llm_model: str | None = None,
) -> list[ParsedWork]:
    return order_works(
        [parse_one(file, llm_fallback=llm_fallback, llm_model=llm_model) for file in files]
    )


def _stable_work_key(work: ParsedWork) -> str:
    if work.episode is not None:
        return f"E{work.episode}"
    stem = Path(work.source_file).stem or work.title or "work"
    key = re.sub(r"[^A-Za-z0-9_-]+", "-", stem).strip("-")
    return key or "work"


def scene_records(works: list[ParsedWork], start: int = 1) -> list[SceneRecord]:
    records: list[SceneRecord] = []
    position = start
    for work in works:
        work_key = _stable_work_key(work)
        for scene in work.scenes:
            records.append(
                SceneRecord(
                    scene_id=f"{work_key}/sc{scene.scene_index}",
                    slug=scene.slug,
                    story_position=position,
                    is_flashback=scene.is_flashback,
                    raw_text=scene.raw_text,
                )
            )
            position += 1
    return records


def ingest_files(
    paths: list[str],
    llm_fallback: bool = False,
    llm_model: str | None = None,
) -> list[SceneRecord]:
    files = expand_inputs(paths)
    if not files:
        raise RuntimeError("no input files found")
    missing = [file for file in files if not os.path.isfile(file)]
    if missing:
        raise RuntimeError(f"file(s) not found: {', '.join(missing)}")
    works = parse_works(files, llm_fallback=llm_fallback, llm_model=llm_model)
    return scene_records(works)


def records_to_json(records: list[SceneRecord], indent: int | None = 2) -> str:
    return json.dumps([asdict(record) for record in records], indent=indent, ensure_ascii=False)

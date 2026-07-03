"""Fountain script segmentation for the scene-record contract.

Fountain files use native scene headings. The parser keeps raw scene text as
close to the source as possible while stripping only legacy `[PLANTED: ...]`
fixture annotations, which are not script content.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Optional

from .models import ParsedWork, Scene

_SCENE_PREFIX_RE = re.compile(
    r"^(INT|EXT|EST|INT\.?/EXT|EXT\.?/INT|I/E|E/I)\b",
    re.IGNORECASE,
)
_SCENE_NUMBER_RE = re.compile(r"\s*#[^#]+#\s*$")
_TITLE_KEY_RE = re.compile(r"^([A-Za-z][A-Za-z0-9 _-]*):\s?(.*)$")
_PLANTED_SUB_RE = re.compile(r"\[PLANTED:[^\]]*\]", re.IGNORECASE)
_FLASHBACK_START_RE = re.compile(r"^(BEGIN\s+FLASHBACK|FLASHBACK)\b", re.IGNORECASE)
_FLASHBACK_END_RE = re.compile(
    r"^(END\s+(OF\s+)?FLASHBACK|BACK\s+TO\s+PRESENT)\b",
    re.IGNORECASE,
)


def is_scene_heading(stripped: str) -> bool:
    if not stripped:
        return False
    if stripped.startswith(".") and not stripped.startswith(".."):
        return True
    return bool(_SCENE_PREFIX_RE.match(stripped))


def normalize_slug(stripped: str) -> str:
    slug = stripped
    if slug.startswith(".") and not slug.startswith(".."):
        slug = slug[1:].strip()
    slug = _SCENE_NUMBER_RE.sub("", slug)
    return slug.strip()


def strip_planted_annotation(line: str) -> tuple[str, int]:
    count = len(_PLANTED_SUB_RE.findall(line))
    cleaned = _PLANTED_SUB_RE.sub("", line)
    if "[PLANTED:" in cleaned.upper():
        cleaned = ""
        count += 1
    if count:
        cleaned = cleaned.strip()
    return cleaned, count


def clean_scene_text(lines: list[str]) -> str:
    """Trim outer blank lines and right-side whitespace, preserving inner lines."""
    while lines and not lines[0].strip():
        lines.pop(0)
    while lines and not lines[-1].strip():
        lines.pop()
    return "\n".join(line.rstrip() for line in lines)


def _parse_title_page(lines: list[str]) -> tuple[dict[str, str], int]:
    title_page: dict[str, str] = {}
    i = 0
    while i < len(lines) and not lines[i].strip():
        i += 1
    if i >= len(lines) or not _TITLE_KEY_RE.match(lines[i].strip()):
        return title_page, 0

    last_key: Optional[str] = None
    while i < len(lines):
        line = lines[i]
        if not line.strip():
            i += 1
            break
        match = _TITLE_KEY_RE.match(line.strip())
        if match:
            last_key = match.group(1).strip().lower()
            title_page[last_key] = match.group(2).strip()
        elif last_key:
            title_page[last_key] = (title_page[last_key] + " " + line.strip()).strip()
        i += 1
    return title_page, i


def _episode_number(title_page: dict[str, str]) -> Optional[int]:
    episode = title_page.get("episode")
    if not episode:
        return None
    match = re.match(r"\s*(\d+)", episode)
    return int(match.group(1)) if match else None


def _derive_title(title_page: dict[str, str], source_file: str, episode: Optional[int]) -> str:
    ep_field = title_page.get("episode")
    episode_title = None
    if ep_field:
        match = re.search(r'"([^"]+)"', ep_field)
        if match:
            episode_title = match.group(1)
    if episode is not None and episode_title:
        return f"E{episode} - {episode_title}"
    if episode is not None:
        return f"E{episode}"
    if title_page.get("title"):
        return title_page["title"]
    return Path(source_file).stem or "untitled"


def parse_fountain(text: str, source_file: str = "") -> ParsedWork:
    lines = text.splitlines()
    title_page, start = _parse_title_page(lines)
    episode = _episode_number(title_page)
    work = ParsedWork(
        source_file=source_file,
        title=_derive_title(title_page, source_file, episode),
        show_title=title_page.get("title"),
        episode=episode,
        sort_order=episode,
        title_page=title_page,
    )

    scenes_raw: list[tuple[str, bool, list[str]]] = []
    cur_slug: Optional[str] = None
    cur_flashback = False
    cur_lines: list[str] = []
    in_flashback = False
    prev_blank = True

    def flush() -> None:
        if cur_slug is not None:
            scenes_raw.append((cur_slug, cur_flashback, cur_lines.copy()))

    for original in lines[start:]:
        raw, stripped_count = strip_planted_annotation(original)
        work.stripped_planted += stripped_count
        stripped = raw.strip()

        if stripped.isupper() and _FLASHBACK_END_RE.match(stripped):
            in_flashback = False
            prev_blank = False
            continue
        if (
            stripped.isupper()
            and _FLASHBACK_START_RE.match(stripped)
            and not is_scene_heading(stripped)
        ):
            in_flashback = True
            prev_blank = False
            continue

        if prev_blank and is_scene_heading(stripped):
            flush()
            cur_slug = normalize_slug(stripped)
            cur_flashback = in_flashback or ("FLASHBACK" in cur_slug.upper())
            cur_lines = [raw]
            prev_blank = False
            continue

        if cur_slug is not None:
            cur_lines.append(raw)
        prev_blank = stripped == ""

    flush()

    for index, (slug, is_flashback, body_lines) in enumerate(scenes_raw, start=1):
        work.scenes.append(
            Scene(
                scene_index=index,
                slug=slug,
                is_flashback=is_flashback,
                raw_text=clean_scene_text(body_lines),
            )
        )
    return work

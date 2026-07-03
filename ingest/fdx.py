"""Final Draft (.fdx) XML segmentation for the scene-record contract."""

from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Optional

from .fountain import (
    _FLASHBACK_END_RE,
    _FLASHBACK_START_RE,
    _SCENE_PREFIX_RE,
    _TITLE_KEY_RE,
    _derive_title,
    _episode_number,
    clean_scene_text,
    normalize_slug,
    strip_planted_annotation,
)
from .models import ParsedWork, Scene

_LEADING_SCENE_NUMBER_RE = re.compile(
    r"^\s*[A-Za-z]?\d+[A-Za-z]?(?:[-.]\d+)?\s+(.+)$"
)

_SKIPPED_CONTAINER_TAGS = {
    "note",
    "notes",
    "scriptnote",
    "scriptnotes",
    "synopsis",
    "synopses",
    "unanchoredscriptnotes",
}
_SKIPPED_INLINE_TAGS = _SKIPPED_CONTAINER_TAGS | {
    "dynamiclabel",
    "pagebreak",
}


def _local_name(name: str) -> str:
    if "}" in name:
        return name.rsplit("}", 1)[1]
    return name


def _attr(element: ET.Element, name: str) -> str:
    wanted = name.casefold()
    for key, value in element.attrib.items():
        if _local_name(key).casefold() == wanted:
            return value
    return ""


def _paragraph_type(paragraph: ET.Element) -> str:
    return _attr(paragraph, "Type").strip()


def _has_ancestor(
    element: ET.Element,
    parents: dict[int, ET.Element],
    names: set[str],
) -> bool:
    current = parents.get(id(element))
    while current is not None:
        if _local_name(current.tag).casefold() in names:
            return True
        current = parents.get(id(current))
    return False


def _paragraph_text(paragraph: ET.Element) -> str:
    pieces: list[str] = []

    def visit(element: ET.Element) -> None:
        tag = _local_name(element.tag).casefold()
        if tag in _SKIPPED_INLINE_TAGS:
            return
        if tag == "text":
            pieces.append(element.text or "")
            return
        for child in element:
            visit(child)

    visit(paragraph)
    if pieces:
        return "".join(pieces)
    if len(paragraph) == 0 and paragraph.text:
        return paragraph.text.strip()
    return ""


def _skip_paragraph(paragraph: ET.Element) -> bool:
    ptype = _paragraph_type(paragraph).casefold()
    return "note" in ptype or "synopsis" in ptype


def _iter_script_paragraphs(element: ET.Element):
    for child in element:
        tag = _local_name(child.tag).casefold()
        if tag in _SKIPPED_CONTAINER_TAGS:
            continue
        if tag == "paragraph":
            if not _skip_paragraph(child):
                yield child
            continue
        yield from _iter_script_paragraphs(child)


def _find_script_content(root: ET.Element) -> Optional[ET.Element]:
    parents = {id(child): parent for parent in root.iter() for child in parent}
    title_or_note = _SKIPPED_CONTAINER_TAGS | {"titlepage"}
    candidates = [
        element
        for element in root.iter()
        if _local_name(element.tag).casefold() == "content"
        and not _has_ancestor(element, parents, title_or_note)
    ]
    for content in candidates:
        if any(
            _paragraph_type(paragraph).casefold() == "scene heading"
            for paragraph in _iter_script_paragraphs(content)
        ):
            return content
    return candidates[0] if candidates else None


def _parse_title_page(root: ET.Element) -> dict[str, str]:
    title_page: dict[str, str] = {}
    last_key: Optional[str] = None
    for title in root.iter():
        if _local_name(title.tag).casefold() != "titlepage":
            continue
        contents = [
            child for child in title if _local_name(child.tag).casefold() == "content"
        ]
        for content in contents:
            for paragraph in content.iter():
                if _local_name(paragraph.tag).casefold() != "paragraph":
                    continue
                line = _paragraph_text(paragraph).strip()
                if not line:
                    continue
                match = _TITLE_KEY_RE.match(line)
                if match:
                    last_key = match.group(1).strip().lower()
                    title_page[last_key] = match.group(2).strip()
                elif last_key:
                    title_page[last_key] = (title_page[last_key] + " " + line).strip()
    return title_page


def _episode_from_filename(source_file: str) -> Optional[int]:
    match = re.search(r"(\d+)", Path(source_file).stem)
    return int(match.group(1)) if match else None


def _work_from_metadata(source_file: str, title_page: dict[str, str]) -> ParsedWork:
    episode = _episode_number(title_page)
    if episode is None:
        episode = _episode_from_filename(source_file)
    return ParsedWork(
        source_file=source_file,
        title=_derive_title(title_page, source_file, episode),
        show_title=title_page.get("title"),
        episode=episode,
        sort_order=episode,
        title_page=title_page,
    )


def _normalize_fdx_slug(text: str) -> str:
    slug = normalize_slug(text)
    match = _LEADING_SCENE_NUMBER_RE.match(slug)
    if match and _SCENE_PREFIX_RE.match(match.group(1)):
        slug = match.group(1).strip()
    return slug


def _omission_attr_seen(paragraph: ET.Element) -> bool:
    for element in paragraph.iter():
        for key, value in element.attrib.items():
            key_name = _local_name(key).casefold()
            value_norm = value.strip().casefold()
            if key_name in {"omit", "omitted", "omission"} and value_norm in {
                "1",
                "true",
                "yes",
                "omitted",
            }:
                return True
            if key_name in {"status", "state"} and value_norm == "omitted":
                return True
    return False


def _is_omitted_scene(paragraph: ET.Element, slug: str) -> bool:
    if _omission_attr_seen(paragraph):
        return True
    normalized = re.sub(r"\s+", " ", slug).strip().upper().rstrip(".")
    if normalized in {"OMIT", "OMITTED", "SCENE OMITTED"}:
        return True
    return "OMITTED" in normalized and not _SCENE_PREFIX_RE.match(slug)


def parse_fdx(path: str) -> ParsedWork:
    try:
        root = ET.parse(path).getroot()
    except ET.ParseError as exc:
        raise RuntimeError(f"{path}: invalid Final Draft XML") from exc

    work = _work_from_metadata(path, _parse_title_page(root))
    content = _find_script_content(root)
    if content is None:
        return work

    scenes_raw: list[tuple[str, bool, list[str]]] = []
    cur_slug: Optional[str] = None
    cur_flashback = False
    cur_lines: list[str] = []
    in_flashback = False

    def flush() -> None:
        if cur_slug is not None:
            scenes_raw.append((cur_slug, cur_flashback, cur_lines.copy()))

    for paragraph in _iter_script_paragraphs(content):
        ptype = _paragraph_type(paragraph).casefold()
        raw, stripped_count = strip_planted_annotation(_paragraph_text(paragraph))
        work.stripped_planted += stripped_count
        stripped = raw.strip()

        if ptype == "scene heading":
            flush()
            slug = _normalize_fdx_slug(stripped)
            if not slug or _is_omitted_scene(paragraph, slug):
                cur_slug = None
                cur_lines = []
                cur_flashback = False
                continue
            cur_slug = slug
            cur_flashback = in_flashback or ("FLASHBACK" in slug.upper())
            cur_lines = [raw]
            continue

        if stripped.isupper() and _FLASHBACK_END_RE.match(stripped):
            in_flashback = False
            continue
        if stripped.isupper() and _FLASHBACK_START_RE.match(stripped):
            in_flashback = True
            continue

        if cur_slug is not None:
            cur_lines.append(raw)

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

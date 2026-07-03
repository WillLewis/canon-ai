"""PDF and docx script segmentation.

The pragmatic v0 path is layout/text parsing first. If extraction returns text
but no scene headings can be identified, callers may opt into an LLM
re-segmentation fallback. Scanned PDFs still fail clearly because this module
does not do OCR.
"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Optional

from .fountain import (
    _FLASHBACK_END_RE,
    _FLASHBACK_START_RE,
    _SCENE_PREFIX_RE,
    clean_scene_text,
    normalize_slug,
    strip_planted_annotation,
)
from .models import ParsedWork, Scene

_NOISE_RES = (
    re.compile(r"^\s*\d+\.?\s*$"),
    re.compile(r"^\s*\(?\s*(CONTINUED|MORE)\s*:?\s*\)?\s*(\(\d+\))?\s*$", re.I),
    re.compile(r"^\s*page\s+\d+(\s+of\s+\d+)?\s*$", re.I),
)

LLM_FALLBACK_ENV = "CANON_INGEST_LLM_FALLBACK"
LLM_MODEL_ENV = "CANON_INGEST_LLM_MODEL"
MAX_LLM_TOKENS = 16000


def _is_noise(line: str) -> bool:
    return any(pattern.match(line) for pattern in _NOISE_RES)


def _is_doc_heading(stripped: str) -> bool:
    if not stripped or not _SCENE_PREFIX_RE.match(stripped):
        return False
    return stripped == stripped.upper()


def _work_from_filename(source_file: str) -> ParsedWork:
    stem = Path(source_file).stem
    match = re.search(r"(\d+)", stem)
    episode = int(match.group(1)) if match else None
    return ParsedWork(
        source_file=source_file,
        title=stem,
        show_title=None,
        episode=episode,
        sort_order=episode,
    )


def segment_layout_lines(lines: list[str], source_file: str) -> ParsedWork:
    work = _work_from_filename(source_file)
    scenes_raw: list[tuple[str, bool, list[str]]] = []
    cur_slug: Optional[str] = None
    cur_flashback = False
    cur_lines: list[str] = []
    in_flashback = False

    def flush() -> None:
        if cur_slug is not None:
            scenes_raw.append((cur_slug, cur_flashback, cur_lines.copy()))

    for original in lines:
        raw, stripped_count = strip_planted_annotation(original)
        work.stripped_planted += stripped_count
        stripped = raw.strip()

        if _is_noise(stripped):
            continue
        if stripped.isupper() and _FLASHBACK_END_RE.match(stripped):
            in_flashback = False
            continue
        if stripped.isupper() and _FLASHBACK_START_RE.match(stripped) and not _is_doc_heading(stripped):
            in_flashback = True
            continue
        if _is_doc_heading(stripped):
            flush()
            cur_slug = normalize_slug(stripped)
            cur_flashback = in_flashback or ("FLASHBACK" in cur_slug.upper())
            cur_lines = [raw]
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


def _llm_enabled(explicit: bool) -> bool:
    return explicit or os.environ.get(LLM_FALLBACK_ENV) == "1"


def _model_name(model: str | None) -> str:
    selected = model or os.environ.get(LLM_MODEL_ENV)
    if not selected:
        raise RuntimeError(
            "LLM re-segmentation fallback requires --llm-model or "
            f"{LLM_MODEL_ENV}; automatic model selection is intentionally avoided."
        )
    return selected


def _first_text_block(response) -> str:
    for block in getattr(response, "content", None) or []:
        if getattr(block, "type", None) == "text":
            return block.text
    raise RuntimeError("LLM fallback returned no text block")


def _segment_with_llm(text: str, source_file: str, model: str | None) -> ParsedWork:
    try:
        import anthropic
    except ModuleNotFoundError as exc:
        raise RuntimeError(
            "anthropic is not installed; install requirements or rerun without LLM fallback."
        ) from exc

    prompt = (
        "Segment the script text into scenes for Canon AI ingestion.\n"
        "Do not write, invent, continue, summarize, or paraphrase story content.\n"
        "Return strict JSON with this shape only:\n"
        '{"scenes":[{"slug":"INT. LOCATION - TIME","is_flashback":false,'
        '"raw_text":"scene heading and body copied exactly from the input"}]}\n'
        "Every raw_text value must be copied from the input text.\n\n"
        "SCRIPT TEXT:\n"
        "<<<\n"
        f"{text}\n"
        ">>>"
    )
    client = anthropic.Anthropic()
    response = client.messages.create(
        model=_model_name(model),
        max_tokens=MAX_LLM_TOKENS,
        system=(
            "You are a segmentation engine. You never generate story material; "
            "you only identify scene boundaries in user-supplied script text."
        ),
        messages=[{"role": "user", "content": prompt}],
    )
    try:
        payload = json.loads(_first_text_block(response))
    except json.JSONDecodeError as exc:
        raise RuntimeError("LLM fallback did not return valid JSON") from exc

    work = _work_from_filename(source_file)
    for index, item in enumerate(payload.get("scenes") or [], start=1):
        raw_text = str(item.get("raw_text") or "").strip()
        slug = normalize_slug(str(item.get("slug") or "").strip())
        if not slug or not raw_text:
            continue
        if " ".join(raw_text.split()) not in " ".join(text.split()):
            raise RuntimeError(
                f"LLM fallback produced unverifiable text for {source_file}; refusing output."
            )
        work.scenes.append(
            Scene(
                scene_index=index,
                slug=slug,
                is_flashback=bool(item.get("is_flashback")),
                raw_text=raw_text,
            )
        )
    if not work.scenes:
        raise RuntimeError(f"{source_file}: LLM fallback found no scenes")
    return work


def _layout_or_llm(
    lines: list[str],
    source_file: str,
    llm_fallback: bool,
    llm_model: str | None,
) -> ParsedWork:
    work = segment_layout_lines(lines, source_file)
    if work.scenes:
        return work
    if not _llm_enabled(llm_fallback):
        raise RuntimeError(
            f"{source_file}: no scene headings found by layout parser. "
            f"Set --llm-fallback and --llm-model, or {LLM_FALLBACK_ENV}=1 and "
            f"{LLM_MODEL_ENV}, to try LLM re-segmentation."
        )
    return _segment_with_llm("\n".join(lines), source_file, llm_model)


def parse_pdf(path: str, llm_fallback: bool = False, llm_model: str | None = None) -> ParsedWork:
    try:
        from pypdf import PdfReader
    except ModuleNotFoundError as exc:
        raise RuntimeError(
            "pypdf is not installed; install requirements to ingest PDFs."
        ) from exc

    reader = PdfReader(path)
    lines: list[str] = []
    extracted_any = False
    for page in reader.pages:
        text = page.extract_text() or ""
        if text.strip():
            extracted_any = True
        lines.extend(text.splitlines())
        lines.append("")
    if not extracted_any:
        raise RuntimeError(f"{path}: no extractable text; OCR/scanned PDF ingestion is out of scope.")
    return _layout_or_llm(lines, path, llm_fallback, llm_model)


def parse_docx(path: str, llm_fallback: bool = False, llm_model: str | None = None) -> ParsedWork:
    try:
        from docx import Document
    except ModuleNotFoundError as exc:
        raise RuntimeError(
            "python-docx is not installed; install requirements to ingest .docx files."
        ) from exc

    document = Document(path)
    lines = [paragraph.text for paragraph in document.paragraphs]
    return _layout_or_llm(lines, path, llm_fallback, llm_model)

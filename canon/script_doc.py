"""PDF / docx segmentation — Canon AI ingestion, Stage 1 (docs/extraction.md).

Same job as canon/fountain.py for non-Fountain containers: document in ->
scenes out (slug, is_flashback, raw_text), reusing the Fountain module's heading
grammar, flashback transitions, planted-annotation stripping, and dataclasses.

v0 is the "layout parse" path from docs/extraction.md: extract text lines
(pypdf per page / python-docx paragraphs), drop screenplay-PDF noise (page
numbers, CONTINUED/MORE markers), and segment on scene headings. Two deliberate
differences from Fountain mode, because PDF text extraction is lossy:
  - a heading needs the INT./EXT. prefix AND an all-caps line, but NOT a
    preceding blank line (extractors often drop blank lines);
  - there is no title page / forced-`.` heading grammar; anything before the
    first heading is skipped.
Work metadata comes from the filename: title = stem, episode = first integer in
the stem (ep102.pdf -> 102), which `order_works` uses for story-position order.

The LLM re-segmentation fallback for messy PDFs (docs/extraction.md) is NOT
built — time-boxed per SPEC open questions; this covers cleanly-extracting
text-based scripts. Scanned/image PDFs raise a clear error instead.
"""

from __future__ import annotations

import re
from pathlib import Path

from .fountain import (
    ParsedWork,
    Scene,
    _FLASHBACK_END_RE,
    _FLASHBACK_START_RE,
    _PLANTED_SUB_RE,
    _SCENE_PREFIX_RE,
    _clean_block,
    _normalize_slug,
)

# Screenplay-PDF furniture that is not script content.
_NOISE_RES = (
    re.compile(r"^\s*\d+\.?\s*$"),                       # bare page numbers / "12."
    re.compile(r"^\s*\(?\s*(CONTINUED|MORE)\s*:?\s*\)?\s*(\(\d+\))?\s*$", re.I),
    re.compile(r"^\s*page\s+\d+(\s+of\s+\d+)?\s*$", re.I),
)


def _is_noise(line: str) -> bool:
    return any(rx.match(line) for rx in _NOISE_RES)


def _is_doc_heading(stripped: str) -> bool:
    """Doc-mode heading: INT./EXT.-prefixed AND fully uppercase. No blank-line
    requirement (PDF extraction drops blanks); the caps requirement keeps prose
    mentions like 'into the interior' or mixed-case dialogue from matching."""
    if not stripped or not _SCENE_PREFIX_RE.match(stripped):
        return False
    return stripped == stripped.upper()


def segment_lines(lines: list, source_file: str) -> ParsedWork:
    """Segment extracted document lines into scenes (shared by PDF and docx)."""
    stem = Path(source_file).stem
    m = re.search(r"(\d+)", stem)
    episode = int(m.group(1)) if m else None
    work = ParsedWork(
        source_file=source_file,
        title=stem,
        show_title=None,
        episode=episode,
        sort_order=episode,
    )

    scenes_raw: list[tuple] = []   # (slug, is_flashback, body_lines)
    cur_slug = None
    cur_flashback = False
    cur_lines: list = []
    in_flashback = False

    def flush() -> None:
        if cur_slug is not None:
            scenes_raw.append((cur_slug, cur_flashback, cur_lines))

    for original in lines:
        n_tags = len(_PLANTED_SUB_RE.findall(original))
        raw = _PLANTED_SUB_RE.sub("", original)
        if "[PLANTED:" in raw.upper():
            raw = ""
            n_tags += 1
        if n_tags:
            raw = raw.strip()
        work.stripped_planted += n_tags

        stripped = raw.strip()
        if _is_noise(stripped):
            continue

        if stripped.isupper() and _FLASHBACK_END_RE.match(stripped):
            in_flashback = False
            continue
        if (stripped.isupper() and _FLASHBACK_START_RE.match(stripped)
                and not _is_doc_heading(stripped)):
            in_flashback = True
            continue

        if _is_doc_heading(stripped):
            flush()
            cur_slug = _normalize_slug(stripped)
            cur_flashback = in_flashback or ("FLASHBACK" in cur_slug.upper())
            cur_lines = [raw]
            continue

        if cur_slug is not None:
            cur_lines.append(raw)
        # anything before the first heading (title page, contact block) is skipped

    flush()

    for i, (slug, fb, body) in enumerate(scenes_raw, start=1):
        work.scenes.append(Scene(i, slug, fb, _clean_block(body)))
    return work


def parse_pdf(path: str) -> ParsedWork:
    try:
        from pypdf import PdfReader
    except ModuleNotFoundError as e:
        raise RuntimeError(
            "pypdf is not installed — run `pip install -r requirements.txt` to ingest PDFs."
        ) from e
    reader = PdfReader(path)
    lines: list = []
    extracted_any = False
    for page in reader.pages:
        text = page.extract_text() or ""
        if text.strip():
            extracted_any = True
        lines.extend(text.splitlines())
        lines.append("")  # page boundary: keep page-top headings detached
    if not extracted_any:
        raise RuntimeError(
            f"{path}: no extractable text (scanned/image PDF?). The LLM "
            "re-segmentation fallback is not built yet — export the script as "
            "text-based PDF, Fountain, or docx."
        )
    return segment_lines(lines, path)


def parse_docx(path: str) -> ParsedWork:
    try:
        from docx import Document
    except ModuleNotFoundError as e:
        raise RuntimeError(
            "python-docx is not installed — run `pip install -r requirements.txt` "
            "to ingest .docx files."
        ) from e
    doc = Document(path)
    return segment_lines([p.text for p in doc.paragraphs], path)

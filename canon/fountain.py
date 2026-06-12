"""Fountain segmentation — Canon AI ingestion, Stage 1 (docs/extraction.md).

Pure, dependency-free parsing: screenplay text in -> scenes out. No I/O, no DB,
no LLM. This is the `script file --> ingest --> scenes` arrow in
docs/architecture.md, producing exactly the fields db/schema.sql `scenes` needs:
`slug`, `is_flashback`, `raw_text` (plus per-work `scene_index` for "E101/sc3"
style citations).

Story *position* is NOT assigned here. It is a single integer axis per *world*
spanning multiple works (docs/decisions.md D6), so it is assigned across the whole
batch in canon.ingest, not per file.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

# A scene heading: standard Fountain prefixes (INT/EXT/EST/I-E ...) or a forced
# heading (a line beginning with a single leading '.'). Per the Fountain spec a
# heading is also preceded by a blank line; the parser enforces that to avoid
# mistaking action lines ("Established in 1999...") for headings.
_SCENE_PREFIX_RE = re.compile(
    r"^(INT|EXT|EST|INT\.?/EXT|EXT\.?/INT|I/E|E/I)\b",
    re.IGNORECASE,
)
# Trailing Fountain scene number, e.g. "... - DAY #12#".
_SCENE_NUMBER_RE = re.compile(r"\s*#[^#]+#\s*$")
# Title-page key line, e.g. "Title: GREYHARBOR" / "Episode: 101 ...".
_TITLE_KEY_RE = re.compile(r"^([A-Za-z][A-Za-z0-9 _-]*):\s?(.*)$")
# Planted test-annotation, e.g. "[PLANTED: dead_speaker — ...]". Injected into
# fixtures and NOT script content; the answer key requires the ingester to strip
# them. Matched anywhere on a line so an *inline* tag (prefixing real dialogue)
# is removed without dropping the dialogue.
_PLANTED_SUB_RE = re.compile(r"\[PLANTED:[^\]]*\]", re.IGNORECASE)

# Flashback block transitions (standalone marker lines, not scene content).
_FLASHBACK_START_RE = re.compile(r"^(BEGIN\s+FLASHBACK|FLASHBACK)\b", re.IGNORECASE)
_FLASHBACK_END_RE = re.compile(
    r"^(END\s+(OF\s+)?FLASHBACK|BACK\s+TO\s+PRESENT)\b", re.IGNORECASE
)


@dataclass
class Scene:
    scene_index: int           # 1-based ordinal within the work (citations: scN)
    slug: str                  # normalized heading, e.g. "INT. CHAPEL ON THE POINT - NIGHT"
    is_flashback: bool
    raw_text: str              # heading + body, planted annotations stripped


@dataclass
class ParsedWork:
    source_file: str
    title: str                 # derived work label, e.g. 'E101 — The Ledger'
    show_title: Optional[str]  # 'GREYHARBOR' (Title: ...)
    episode: Optional[int]     # 101 (Episode: ...)
    sort_order: Optional[int]  # ordering hint within the world (episode # if known)
    title_page: dict = field(default_factory=dict)
    scenes: list = field(default_factory=list)
    stripped_planted: int = 0  # count of planted annotations removed (eval signal)


def _is_scene_heading(stripped: str) -> bool:
    if not stripped:
        return False
    if stripped.startswith(".") and not stripped.startswith(".."):
        return True
    return bool(_SCENE_PREFIX_RE.match(stripped))


def _normalize_slug(stripped: str) -> str:
    s = stripped
    if s.startswith(".") and not s.startswith(".."):
        s = s[1:].strip()
    s = _SCENE_NUMBER_RE.sub("", s)
    return s.strip()


def _parse_title_page(lines: list[str]) -> tuple[dict, int]:
    """Return (title_page, index_of_first_body_line).

    A Fountain title page is a run of `Key: value` lines at the very top,
    terminated by a blank line. It exists only if the first non-blank line is a
    key/value pair (so a script that opens on a scene heading has no title page).
    """
    tp: dict = {}
    n = len(lines)
    j = 0
    while j < n and not lines[j].strip():
        j += 1
    if j >= n or not _TITLE_KEY_RE.match(lines[j].strip()):
        return tp, 0  # no title page; treat the whole file as body

    i = j
    last_key: Optional[str] = None
    while i < n:
        line = lines[i]
        if not line.strip():
            i += 1
            break  # blank line terminates the title page
        m = _TITLE_KEY_RE.match(line.strip())
        if m:
            last_key = m.group(1).strip().lower()
            tp[last_key] = m.group(2).strip()
        elif last_key is not None:
            tp[last_key] = (tp[last_key] + " " + line.strip()).strip()
        i += 1
    return tp, i


def _derive_title(title_page: dict, source_file: str, episode: Optional[int]) -> str:
    name = None
    ep_field = title_page.get("episode")
    if ep_field:
        m = re.search(r'"([^"]+)"', ep_field)  # 'Episode: 101 "The Ledger"' -> name
        if m:
            name = m.group(1)
    if episode is not None and name:
        return f"E{episode} — {name}"
    if episode is not None:
        return f"E{episode}"
    if title_page.get("title"):
        return title_page["title"]
    return Path(source_file).stem or "untitled"


def _clean_block(lines: list[str]) -> str:
    """Trim leading/trailing blanks and collapse internal blank runs to one.

    Keeps script content verbatim (right-stripped) while tidying the gaps left by
    stripped planted lines so each scene's raw_text is a clean chunk for the LLM.
    """
    out: list[str] = []
    blank_run = 0
    for ln in lines:
        if ln.strip() == "":
            blank_run += 1
            continue
        if out and blank_run:
            out.append("")
        blank_run = 0
        out.append(ln.rstrip())
    return "\n".join(out)


def parse_fountain(text: str, source_file: str = "") -> ParsedWork:
    """Segment one Fountain file into scenes."""
    lines = text.splitlines()
    title_page, start = _parse_title_page(lines)

    episode = None
    if title_page.get("episode"):
        m = re.match(r"\s*(\d+)", title_page["episode"])
        if m:
            episode = int(m.group(1))

    work = ParsedWork(
        source_file=source_file,
        title=_derive_title(title_page, source_file, episode),
        show_title=title_page.get("title"),
        episode=episode,
        sort_order=episode,
        title_page=title_page,
    )

    scenes_raw: list[tuple[str, bool, list[str]]] = []  # (slug, is_flashback, body)
    cur_slug: Optional[str] = None
    cur_flashback = False
    cur_lines: list[str] = []
    in_flashback = False   # active flashback block, spanning scenes
    prev_blank = True      # start of body counts as preceded by a blank line

    def flush() -> None:
        if cur_slug is not None:
            scenes_raw.append((cur_slug, cur_flashback, cur_lines))

    for idx in range(start, len(lines)):
        original = lines[idx]

        # --- strip planted test annotations (inline or standalone) ---
        n_tags = len(_PLANTED_SUB_RE.findall(original))
        raw = _PLANTED_SUB_RE.sub("", original)
        if "[PLANTED:" in raw.upper():  # defensive: unterminated bracket fragment
            raw = ""
            n_tags += 1
        if n_tags:
            raw = raw.strip()  # tidy whitespace left by a removed annotation
        work.stripped_planted += n_tags

        stripped = raw.strip()

        # --- flashback block transitions (all-caps marker lines, not content) ---
        # The isupper() guard keeps prose like "Flashbacks of the war..." from
        # being read as a transition; real Fountain transitions are uppercase.
        if stripped.isupper() and _FLASHBACK_END_RE.match(stripped):
            in_flashback = False
            prev_blank = False
            continue
        if (
            stripped.isupper()
            and _FLASHBACK_START_RE.match(stripped)
            and not _is_scene_heading(stripped)
        ):
            in_flashback = True
            prev_blank = False
            continue

        # --- scene heading (must follow a blank line) ---
        if prev_blank and _is_scene_heading(stripped):
            flush()
            cur_slug = _normalize_slug(stripped)
            cur_flashback = in_flashback or ("FLASHBACK" in cur_slug.upper())
            cur_lines = [raw]
            prev_blank = False
            continue

        # --- ordinary body line (dropped if before the first heading) ---
        if cur_slug is not None:
            cur_lines.append(raw)
        prev_blank = stripped == ""

    flush()

    for i, (slug, fb, body_lines) in enumerate(scenes_raw, start=1):
        work.scenes.append(
            Scene(
                scene_index=i,
                slug=slug,
                is_flashback=fb,
                raw_text=_clean_block(body_lines),
            )
        )
    return work

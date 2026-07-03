"""Renderers — bible and Reader's Report to markdown and print-ready HTML.

Both artifacts share one HTML shell: standalone (no external assets), light
theme consistent with the product (palette from canon/holes.py), embedded
print CSS so browser print-to-PDF produces the shareable artifact — page
breaks per section, fixed header/footer carrying the world name and the
citation key.

The Reader's Report markdown comes from canon/report.py (imported, not
duplicated); this module only converts that markdown subset into HTML. The
bible markdown is rendered here from the structured dict built by bible.py.

Optional direct-to-PDF via weasyprint sits behind a guarded import: it is
feature-detected and never a hard dependency (keep dependencies boring).
No LLM calls anywhere in this module.
"""

from __future__ import annotations

import html as _html
import re
from typing import Any

# ---------------------------------------------------------------------------
# Markdown — bible
# ---------------------------------------------------------------------------

def _md_line(line: dict[str, Any]) -> str:
    """One cited fact bullet: text — [label] · "quote" (status)."""

    out = f"- {line['text']} — [{line['label']}]"
    if line.get("quote"):
        out += f' · "{line["quote"]}"'
    status = line.get("status") or "canon"
    if status == "sealed":
        out += " (sealed: writer marked intentional)"
    elif status == "draft":
        out += " (unconfirmed extraction)"
    return out


def render_bible_markdown(bible: dict[str, Any]) -> str:
    world = bible.get("world_name") or "world"
    ov = bible.get("overview") or {}
    lines: list[str] = [
        f"# {world} — Series Bible",
        "",
        "Compiled by Canon AI from the writer's own scripts. Every line cites its",
        "source scene; a fact without a citation is omitted, never invented. Canon",
        "never writes the story — this document holds established facts and verbatim",
        "quotes only.",
        "",
        "## World Overview",
        f"- Works: {ov.get('works', 0)}"
        + (f" ({', '.join(str(t) for t in ov.get('work_titles') or [])})" if ov.get("work_titles") else ""),
        f"- Scenes: {ov.get('scenes', 0)}",
        f"- Entities: {ov.get('entities', 0)}"
        + (
            " (" + ", ".join(f"{k}={v}" for k, v in sorted((ov.get("entity_kinds") or {}).items())) + ")"
            if ov.get("entity_kinds") else ""
        ),
        f"- Assertions: {ov.get('assertions', 0)}"
        + (
            " (" + ", ".join(f"{k}={v}" for k, v in sorted((ov.get("assertion_statuses") or {}).items())) + ")"
            if ov.get("assertion_statuses") else ""
        ),
    ]
    span = ov.get("span")
    if span:
        pos = span.get("positions")
        pos_txt = f" (story positions {pos[0]}–{pos[1]})" if pos else ""
        lines.append(f"- Corpus span: {span['first_label']} – {span['last_label']}{pos_txt}")
    if bible.get("omitted_uncited"):
        lines.append(
            f"- Omitted for missing citations: {bible['omitted_uncited']} fact(s) "
            "(a fact without a citation never ships)"
        )
    lines.append("")

    lines.append("## Characters")
    if not bible.get("characters"):
        lines.append("- No established characters in canon yet.")
    for c in bible.get("characters") or []:
        lines.extend(["", f"### {c['name']}"])
        if c.get("aliases"):
            lines.append(f"- Also referenced as: {', '.join(c['aliases'])}")
        first, last = c.get("first_appearance"), c.get("last_appearance")
        if first and last:
            lines.append(f"- Appears: first [{first['label']}], last [{last['label']}]")
        for section in ("traits", "occupation", "relationships", "goals", "knowledge"):
            for row in c.get(section) or []:
                lines.append(_md_line(row))
    lines.append("")

    lines.append("## Locations")
    if not bible.get("locations"):
        lines.append("- No established locations in canon yet.")
    for loc in bible.get("locations") or []:
        lines.extend(["", f"### {loc['name']}"])
        if loc.get("aliases"):
            lines.append(f"- Also referenced as: {', '.join(loc['aliases'])}")
        if loc.get("destroyed"):
            lines.append(_md_line(loc["destroyed"]))
        else:
            lines.append("- Standing (no destruction recorded in canon).")
        for row in loc.get("facts") or []:
            lines.append(_md_line(row))
    lines.append("")

    lines.append("## Timeline")
    if not bible.get("timeline"):
        lines.append("- No assertions in canon yet.")
    for row in bible.get("timeline") or []:
        pos = row.get("position")
        prefix = f"pos {pos} · " if pos is not None else ""
        lines.append(_md_line({**row, "text": f"{prefix}{row['text']}"}))
    lines.append("")

    lines.append("## World Rules")
    if not bible.get("world_rules"):
        lines.append("- No cannot/trait rules established in canon yet.")
    for row in bible.get("world_rules") or []:
        lines.append(_md_line(row))
    lines.append("")

    lines.append("## Open Questions")
    if not bible.get("open_questions"):
        lines.append("- No open gaps — every reference, thread, and claim is resolved.")
    for group in bible.get("open_questions") or []:
        lines.extend(["", f"### {group['label']}"])
        for item in group.get("items") or []:
            lines.append(f"- {item['question']} — [{item['label']}] {item['detail']}")
    lines.append("")
    return "\n".join(lines).rstrip() + "\n"


# ---------------------------------------------------------------------------
# Markdown subset -> HTML (covers what report.py and this module emit)
# ---------------------------------------------------------------------------

def _inline(text: str) -> str:
    out = _html.escape(text, quote=False)
    return re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", out)


def _slug(heading: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", heading.lower()).strip("-") or "section"


def markdown_to_sections(md: str) -> tuple[str, list[tuple[str, str]]]:
    """(title, [(section_id, section_html)]) from the markdown subset used by
    canon/report.py and render_bible_markdown: #/##/### headings, - bullets,
    --- rules, paragraphs, **bold**."""

    title = ""
    sections: list[tuple[str, str]] = []
    sid = "intro"
    buf: list[str] = []
    in_list = False

    def close_list() -> None:
        nonlocal in_list
        if in_list:
            buf.append("</ul>")
            in_list = False

    def close_section() -> None:
        close_list()
        if buf:
            sections.append((sid, "\n".join(buf)))
            buf.clear()

    for raw in md.splitlines():
        line = raw.rstrip()
        if line.startswith("# ") and not title:
            title = line[2:].strip()
            continue
        if line.startswith("## "):
            close_section()
            heading = line[3:].strip()
            sid = _slug(heading)
            buf.append(f"<h2>{_inline(heading)}</h2>")
            continue
        if line.startswith("### "):
            close_list()
            buf.append(f"<h3>{_inline(line[4:].strip())}</h3>")
            continue
        if line.startswith("- "):
            if not in_list:
                buf.append("<ul>")
                in_list = True
            buf.append(f"<li>{_inline(line[2:].strip())}</li>")
            continue
        if line == "---":
            close_list()
            buf.append("<hr>")
            continue
        if not line.strip():
            close_list()
            continue
        close_list()
        buf.append(f"<p>{_inline(line.strip())}</p>")
    close_section()
    return title, sections


# ---------------------------------------------------------------------------
# HTML shell with embedded print CSS
# ---------------------------------------------------------------------------

CITATION_KEY = (
    "Citation key: [WORK/scN] = work (episode/chapter), scene number in that "
    "work. Quotes are verbatim from the writer's script."
)

# Light theme, palette shared with canon/holes.py (product-consistent).
_CSS = """
:root { --ink:#2b2620; --muted:#6f6657; --line:#e3dccd; --bg:#f4f1ea; --card:#fffdf8;
        --accent:#9c5b34; --chip:#efe7d6; }
* { box-sizing:border-box; }
body { margin:0; background:var(--bg); color:var(--ink); font:15px/1.55 -apple-system,
       BlinkMacSystemFont,"Segoe UI",Helvetica,Arial,sans-serif; }
.wrap { max-width:820px; margin:0 auto; padding:48px 28px 80px; }
h1 { font:600 30px/1.2 Georgia,"Times New Roman",serif; margin:0 0 10px; }
h2 { font:600 20px/1.3 Georgia,serif; color:var(--accent); border-bottom:1px solid var(--line);
     padding-bottom:6px; margin:34px 0 12px; }
h3 { font:600 16px/1.3 Georgia,serif; margin:20px 0 6px; }
p  { margin:8px 0; }
ul { margin:8px 0; padding-left:20px; }
li { margin:4px 0; }
hr { border:0; border-top:1px solid var(--line); margin:24px 0; }
.lede { color:var(--muted); }
.trust { font-size:12px; color:var(--muted); border-top:1px solid var(--line);
         margin-top:48px; padding-top:16px; }
.doc-header, .doc-footer { display:none; }
@media print {
  body { background:#fff; font-size:12px; }
  .wrap { max-width:none; padding:0; }
  section.sheet { break-before:page; page-break-before:always; }
  section.sheet:first-of-type { break-before:auto; page-break-before:auto; }
  h2 { break-after:avoid; page-break-after:avoid; }
  li, h3 { break-inside:avoid; page-break-inside:avoid; }
  .doc-header { display:block; position:fixed; top:0; left:0; right:0;
                font:600 9px/1.4 -apple-system,sans-serif; letter-spacing:.06em;
                text-transform:uppercase; color:var(--muted);
                border-bottom:1px solid var(--line); padding:2mm 0; background:#fff; }
  .doc-footer { display:block; position:fixed; bottom:0; left:0; right:0;
                font:400 9px/1.4 -apple-system,sans-serif; color:var(--muted);
                border-top:1px solid var(--line); padding:2mm 0; background:#fff; }
}
@page { margin: 20mm 16mm; }
"""


def html_document(world: str, kind: str, markdown: str) -> str:
    """Standalone print-ready HTML for a markdown artifact."""

    e = _html.escape
    title, sections = markdown_to_sections(markdown)
    title = title or f"{world} — {kind}"
    parts = [
        "<!doctype html>",
        '<html lang="en"><head><meta charset="utf-8">',
        '<meta name="viewport" content="width=device-width,initial-scale=1">',
        f"<title>{e(title)}</title>",
        f"<style>{_CSS}</style></head><body>",
        f'<div class="doc-header">{e(world)} — {e(kind)} · Canon AI</div>',
        f'<div class="doc-footer">{e(CITATION_KEY)}</div>',
        '<div class="wrap">',
        f"<h1>{e(title)}</h1>",
    ]
    for sid, body in sections:
        parts.append(f'<section class="sheet" id="{e(sid)}">{body}</section>')
    parts.append(
        '<p class="trust">Canon never writes your story — it keeps it true. '
        "Every line above cites the scene that establishes it.</p>"
    )
    parts.append("</div></body></html>")
    return "\n".join(parts)


def render_bible_html(bible: dict[str, Any]) -> str:
    world = bible.get("world_name") or "world"
    return html_document(world, "Series Bible", render_bible_markdown(bible))


def render_report_html(world: str, report_markdown: str) -> str:
    """Print-ready HTML for the existing Reader's Report markdown
    (produced by canon.report.render_markdown_report — reused, not duplicated)."""

    return html_document(world, "Reader's Report", report_markdown)


# ---------------------------------------------------------------------------
# Optional direct-to-PDF (guarded; never a hard dependency)
# ---------------------------------------------------------------------------

def pdf_available() -> bool:
    try:
        import weasyprint  # noqa: F401
    except Exception:
        return False
    return True


def render_pdf(html_text: str, out_path: str) -> None:
    """Write a PDF via weasyprint when installed; otherwise explain the
    browser print-to-PDF path instead of failing mysteriously."""

    try:
        from weasyprint import HTML
    except Exception as e:  # ImportError or weasyprint's own system-lib errors
        raise RuntimeError(
            "weasyprint is not installed (it is optional). Export --format html "
            "and use the browser's print-to-PDF instead, or `pip install weasyprint`."
        ) from e
    HTML(string=html_text).write_pdf(out_path)

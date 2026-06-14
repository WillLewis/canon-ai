"""Hole-finder — "questions your world doc doesn't answer" (Phase 1, SPEC R8).

Deterministic gap queries over the assertion graph (db/holes.sql): unestablished
references, open threads (setup without payoff), unexplained knowledge, and
unconfirmed beliefs. It asks diagnostic questions; it never answers them or
generates story content (docs/decisions.md D1) — the writer is the only author.

Reuses the check.py SQL machinery (`to_psycopg`) and ask.py citation resolution.
Output is a text report and a self-contained HTML report a writer can open.
"""

from __future__ import annotations

import html

from .ask import resolve_citations
from .check import to_psycopg

_COLUMNS = ("category", "question", "detail", "scene_id")
_NAME_RE_PREFIX = "select"

# Display order + human framing for each category (drives both report sections).
CATEGORIES = (
    ("unknown_entity", "Unestablished references",
     "Named in the script, but the world never says who or what they are."),
    ("open_thread", "Open threads",
     "Promises, goals, and secrets that get set up but never pay off."),
    ("unsourced_knowledge", "Unexplained knowledge",
     "A character acts on something they were never shown learning."),
    ("unconfirmed_belief", "Unconfirmed beliefs",
     "Claims a character believes that canon never confirms or denies."),
)
_ORDER = {key: i for i, (key, _, _) in enumerate(CATEGORIES)}
_LABELS = {key: label for key, label, _ in CATEGORIES}
_BLURBS = {key: blurb for key, _, blurb in CATEGORIES}


def split_holes(sql_text: str) -> list:
    """Split holes.sql into individual SELECT statements (comments stripped)."""
    body = "\n".join(ln for ln in sql_text.splitlines() if not ln.lstrip().startswith("--"))
    return [s.strip() for s in body.split(";") if s.strip()]


def run_holes(conn, world_id: int, holes_sql: str) -> tuple:
    """Execute every gap query; return (holes, errors). Each hole is a dict with
    category/question/detail/scene_id plus a resolved citation label."""
    cur = conn.cursor()
    holes: list = []
    errors: list = []
    for stmt in split_holes(holes_sql):
        try:
            cur.execute(to_psycopg(stmt), {"world_id": world_id})
            rows = cur.fetchall()
        except Exception as e:  # one bad query shouldn't sink the rest
            conn.rollback()
            errors.append(str(e).splitlines()[0])
            continue
        for r in rows:
            holes.append(dict(zip(_COLUMNS, r)))

    cites = resolve_citations(conn, [h["scene_id"] for h in holes])
    for h in holes:
        c = cites.get(h["scene_id"])
        h["cite"] = c["label"] if c else None
        h["cite_full"] = c["full"] if c else None
    holes.sort(key=lambda h: (_ORDER.get(h["category"], 99), h["scene_id"] or 0))
    return holes, errors


def group_by_category(holes: list) -> list:
    """[(key, label, blurb, [holes])] in display order, non-empty groups only."""
    out = []
    for key, label, blurb in CATEGORIES:
        items = [h for h in holes if h["category"] == key]
        if items:
            out.append((key, label, blurb, items))
    return out


def render_text(world: str, holes: list) -> str:
    lines = [f"Hole-finder — {world}: {len(holes)} question(s) your canon doesn't answer", ""]
    if not holes:
        lines.append("  (no gaps found — every reference, thread, and claim is resolved)")
        return "\n".join(lines)
    for _key, label, _blurb, items in group_by_category(holes):
        lines.append(f"{label} ({len(items)})")
        for h in items:
            cite = f"  [{h['cite']}]" if h.get("cite") else ""
            lines.append(f"  • {h['question']}{cite}")
            lines.append(f"      {h['detail']}")
        lines.append("")
    return "\n".join(lines).rstrip()


_CSS = """
:root { --ink:#2b2620; --muted:#6f6657; --line:#e3dccd; --bg:#f4f1ea; --card:#fffdf8;
        --accent:#9c5b34; --chip:#efe7d6; }
* { box-sizing:border-box; }
body { margin:0; background:var(--bg); color:var(--ink); font:16px/1.55 -apple-system,
       BlinkMacSystemFont,"Segoe UI",Helvetica,Arial,sans-serif; }
.wrap { max-width:760px; margin:0 auto; padding:56px 24px 80px; }
h1 { font:600 30px/1.2 Georgia,"Times New Roman",serif; margin:0 0 6px; }
.lede { color:var(--muted); margin:0 0 4px; font-size:17px; }
.meta { color:var(--muted); font-size:13px; margin:0 0 40px; }
.trust { font-size:12px; color:var(--muted); border-top:1px solid var(--line);
         margin-top:48px; padding-top:16px; }
section { margin:0 0 36px; }
.sec-h { font:600 13px/1 -apple-system,sans-serif; letter-spacing:.08em; text-transform:uppercase;
         color:var(--accent); margin:0 0 4px; }
.sec-b { color:var(--muted); font-size:13px; margin:0 0 16px; }
.card { background:var(--card); border:1px solid var(--line); border-radius:10px;
        padding:16px 18px; margin:0 0 12px; }
.q { font:600 18px/1.35 Georgia,serif; margin:0 0 6px; }
.d { color:var(--muted); font-size:14px; margin:0; }
.chip { display:inline-block; margin-top:10px; background:var(--chip); color:var(--muted);
        font:600 11px/1 ui-monospace,SFMono-Regular,Menlo,monospace; letter-spacing:.03em;
        padding:5px 8px; border-radius:5px; }
.empty { background:var(--card); border:1px solid var(--line); border-radius:10px;
         padding:24px; color:var(--muted); }
"""


def render_html(world: str, holes: list, generated_at: str = "") -> str:
    e = html.escape
    groups = group_by_category(holes)
    n = len(holes)
    parts = [
        "<!doctype html><html lang=en><head><meta charset=utf-8>",
        "<meta name=viewport content='width=device-width,initial-scale=1'>",
        f"<title>Hole-finder — {e(world)}</title><style>{_CSS}</style></head><body><div class=wrap>",
        f"<h1>{n} question{'' if n == 1 else 's'} your world doesn't answer</h1>",
        f"<p class=lede>{e(world)} — holes the canon graph surfaced for you to fill.</p>",
        f"<p class=meta>Generated by Canon AI{(' · ' + e(generated_at)) if generated_at else ''} · "
        "deterministic — every question cites where the gap is.</p>",
    ]
    if not holes:
        parts.append("<div class=empty>No gaps found — every reference, thread, and claim "
                     "in this canon is resolved.</div>")
    for key, label, blurb, items in groups:
        parts.append("<section>")
        parts.append(f"<div class=sec-h>{e(label)}</div><div class=sec-b>{e(blurb)}</div>")
        for h in items:
            chip = f"<span class=chip>{e(h['cite'])}</span>" if h.get("cite") else ""
            parts.append(
                f"<div class=card><p class=q>{e(h['question'])}</p>"
                f"<p class=d>{e(h['detail'])}</p>{chip}</div>"
            )
        parts.append("</section>")
    parts.append("<p class=trust>Canon never writes your story — it keeps it true. "
                 "These are questions, not answers; the writer is the only author.</p>")
    parts.append("</div></body></html>")
    return "".join(parts)

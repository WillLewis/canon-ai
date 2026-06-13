"""Pure display helpers — no database, no story generation.

These turn raw graph values into reader-friendly HTML/text for the templates:
the story-position range gloss, the citation-quote highlighter, and a small
struct describing an assertion's object side. The highlighter is the heart of the
"citation inspector": it marks the verbatim supporting quote inside the scene's
raw text so a writer can see the claim in context. It only ever *marks existing
text* — it never writes or paraphrases.
"""

from __future__ import annotations

import html
import re

from markupsafe import Markup


def gloss_range(text: str | None) -> str:
    """Turn an int4range literal into a plain-English story-position span.

    '[2,)'   -> 'pos 2 onward'      (unbounded upper)
    '(,)'    -> 'all positions'     (always valid)
    '[2,5)'  -> 'pos 2–4'           (half-open, exclusive upper per int4range)
    """
    if not text:
        return ""
    if text in ("(,)", "empty"):
        return "all positions" if text == "(,)" else "empty"
    m = re.match(r"^([\[(])\s*(-?\d+)?\s*,\s*(-?\d+)?\s*([\])])$", text)
    if not m:
        return text
    lo_b, lo, hi, hi_b = m.groups()
    lo_i = int(lo) if lo is not None else None
    hi_i = int(hi) if hi is not None else None
    # Normalize to inclusive endpoints for display.
    if lo_i is not None and lo_b == "(":
        lo_i += 1
    if hi_i is not None and hi_b == ")":
        hi_i -= 1
    if lo_i is not None and hi_i is None:
        return f"pos {lo_i} onward"
    if lo_i is None and hi_i is not None:
        return f"through pos {hi_i}"
    if lo_i is not None and hi_i is not None:
        return f"pos {lo_i}" if lo_i == hi_i else f"pos {lo_i}–{hi_i}"
    return "all positions"


def highlight(raw_text: str | None, quote: str | None) -> Markup:
    """HTML-escape `raw_text` and wrap every occurrence of `quote` in <mark>.

    Both sides are escaped with the same function before matching, so the
    substring relationship is preserved (Canon guarantees the quote is verbatim
    in the scene). If the quote isn't found, the scene renders plain — the caller
    surfaces a 'quote not located' note rather than this function inventing one.
    """
    raw_text = raw_text or ""
    escaped = html.escape(raw_text)
    if not quote:
        return Markup(escaped)
    eq = html.escape(quote.strip())
    if not eq or eq not in escaped:
        return Markup(escaped)
    marked = escaped.replace(eq, f"<mark>{eq}</mark>")
    return Markup(marked)


def quote_present(raw_text: str | None, quote: str | None) -> bool:
    if not raw_text or not quote:
        return False
    return html.escape(quote.strip()) in html.escape(raw_text)


def object_side(a: dict) -> dict:
    """Describe an assertion's object for rendering.

    Exactly one of three forms (schema CHECK): an entity, a literal value, or a
    nested assertion (epistemic 'knows/believes THIS assertion'). Returns
    {'kind': 'entity'|'value'|'assertion'|'none', ...} so the template stays dumb.
    """
    if a.get("object_id"):
        return {"kind": "entity", "id": a["object_id"],
                "name": a.get("object_name"), "entity_kind": a.get("object_kind")}
    if a.get("object_assertion_id"):
        return {"kind": "assertion", "id": a["object_assertion_id"]}
    if a.get("object_value") not in (None, ""):
        return {"kind": "value", "value": a["object_value"]}
    return {"kind": "none"}


SEVERITY_RANK = {"critical": 0, "warning": 1, "note": 2}

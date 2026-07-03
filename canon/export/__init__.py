"""Export artifacts — series bible, print-ready report rendering, share links.

The paid tentpole of the build phase (P3-ARTIFACTS). Everything in this package
is pure deterministic assembly over the assertion graph: SQL in, structured
facts + verbatim quotes out. No LLM calls, no prose generation, no loglines,
no synopsis — Canon never writes the user's story. Every line in every export
carries its scene citation; a fact without a citation is omitted, never
invented.

Modules:
  bible.py  — assertion graph -> structured series bible (dict of sections)
  render.py — bible + Reader's Report -> markdown / standalone print-CSS HTML
              (optional weasyprint direct-to-PDF behind a guarded import)
  share.py  — share_links plumbing: create / resolve / revoke over a cursor
  cli.py    — `canon export` subcommand registration
"""

from .bible import assemble_bible, build_bible, load_bible_source
from .render import (
    pdf_available,
    render_bible_html,
    render_bible_markdown,
    render_pdf,
    render_report_html,
)
from .share import create_share_link, resolve_share_link, revoke_share_link

__all__ = [
    "assemble_bible",
    "build_bible",
    "load_bible_source",
    "render_bible_markdown",
    "render_bible_html",
    "render_report_html",
    "pdf_available",
    "render_pdf",
    "create_share_link",
    "resolve_share_link",
    "revoke_share_link",
]

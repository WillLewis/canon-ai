"""Renderer smoke tests — standalone HTML, print CSS, anchors, PDF guard.

Offline. The HTML contract: valid standalone document (doctype, charset,
title), embedded print CSS (@media print, @page, per-section page breaks),
header/footer carrying world name + citation key, section anchors, escaped
content. The report path reuses canon/report.py's markdown renderer.
"""

import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from canon.export import render as render_mod  # noqa: E402
from canon.export.bible import assemble_bible  # noqa: E402
from test_export_bible import _fixture  # noqa: E402


def _bible_html():
    return render_mod.render_bible_html(assemble_bible(_fixture()))


def test_bible_html_is_standalone_with_print_css():
    html = _bible_html()
    assert html.lstrip().lower().startswith("<!doctype html>")
    assert '<meta charset="utf-8">' in html
    assert "<title>" in html and "</html>" in html
    assert "@media print" in html
    assert "@page" in html
    assert "break-before:page" in html
    # no external assets — a strict offline artifact
    assert "http://" not in html and "https://" not in html


def test_bible_html_has_section_anchors_and_header_footer():
    html = _bible_html()
    for anchor in ("world-overview", "characters", "locations", "timeline",
                   "world-rules", "open-questions"):
        assert f'id="{anchor}"' in html, f"missing section anchor {anchor}"
    assert 'class="doc-header"' in html and "greyharbor" in html
    assert 'class="doc-footer"' in html and "Citation key" in html


def test_report_html_wraps_report_markdown_with_anchors():
    md = (
        "# Reader's Report - greyharbor\n\n"
        "## Continuity Findings\n- [warning] dead_speaker: Tobias speaks after death.\n\n"
        "## Open Questions\n- **Does the bell resolve?** No adjacent assertion; cites: E101/sc2\n"
    )
    html = render_mod.render_report_html("greyharbor", md)
    assert html.lstrip().lower().startswith("<!doctype html>")
    assert 'id="continuity-findings"' in html
    assert 'id="open-questions"' in html
    assert "<strong>Does the bell resolve?</strong>" in html
    assert "Reader&#x27;s Report" in html or "Reader's Report" in html


def test_html_escapes_script_content():
    source = _fixture()
    source["assertions"][0]["supporting_quote"] = '<script>alert("x")</script>'
    html = render_mod.render_bible_html(assemble_bible(source))
    assert "<script>" not in html
    assert "&lt;script&gt;" in html


def test_markdown_to_sections_parses_subset():
    title, sections = render_mod.markdown_to_sections(
        "# Title\n\nlede line\n\n## Sec One\n- a\n- b\n\n### Sub\ntext\n\n---\n\n## Sec Two\n- c\n"
    )
    ids = [sid for sid, _ in sections]
    assert title == "Title"
    assert ids == ["intro", "sec-one", "sec-two"]
    sec_one = dict(sections)["sec-one"]
    assert "<ul>" in sec_one and "<li>a</li>" in sec_one and "<h3>Sub</h3>" in sec_one


def test_pdf_is_feature_detected_never_a_hard_dependency():
    if render_mod.pdf_available():
        # weasyprint happens to be installed here; the guard must not raise.
        assert callable(render_mod.render_pdf)
    else:
        try:
            render_mod.render_pdf("<!doctype html><html></html>", "/tmp/never-written.pdf")
        except RuntimeError as e:
            assert "weasyprint" in str(e)
        else:
            raise AssertionError("render_pdf must raise RuntimeError without weasyprint")

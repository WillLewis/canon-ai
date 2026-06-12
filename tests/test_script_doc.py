"""PDF / docx ingestion tests — round-trip against the Fountain parse.

Fixtures are generated at test time in temp dirs from our own .fountain files
(rights guard forbids committing screenplay containers). The bar: the PDF/docx
parse of the same text must segment identically to the Fountain parse.

    python -m pytest -q
    python tests/test_script_doc.py
"""

import pathlib
import shutil
import sys
import tempfile

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

from canon import ingest, script_doc  # noqa: E402
from canon.fountain import parse_fountain  # noqa: E402
from pdf_util import text_to_pdf  # noqa: E402

FIXTURES = ROOT / "fixtures" / "greyharbor"


def _fountain_text(name):
    return (FIXTURES / name).read_text(encoding="utf-8")


def _expected(name):
    return parse_fountain(_fountain_text(name), source_file=name)


# --- segmentation core -------------------------------------------------------

def test_doc_heading_requires_caps():
    assert script_doc._is_doc_heading("INT. CHAPEL ON THE POINT - NIGHT")
    assert script_doc._is_doc_heading("EXT. COAST ROAD - DUSK")
    assert not script_doc._is_doc_heading("interior monologue continues")
    assert not script_doc._is_doc_heading("Int. the chapel was cold")  # mixed case prose
    assert not script_doc._is_doc_heading("")


def test_noise_lines_dropped_and_metadata_from_filename():
    lines = [
        "GREYHARBOR — shooting draft",      # pre-heading junk: skipped
        "INT. OFFICE - DAY",
        "Action line one.",
        "12.",                               # page number
        "(CONTINUED)",
        "CONTINUED: (2)",
        "Action line two.",
        "EXT. DOCKS - NIGHT",
        "More action.",
    ]
    w = script_doc.segment_lines(lines, "ep207_draft.pdf")
    assert w.title == "ep207_draft" and w.episode == 207 and w.sort_order == 207
    assert [s.slug for s in w.scenes] == ["INT. OFFICE - DAY", "EXT. DOCKS - NIGHT"]
    body = w.scenes[0].raw_text
    assert "Action line one." in body and "Action line two." in body
    assert "12." not in body and "CONTINUED" not in body
    assert "shooting draft" not in body


def test_flashback_transitions_in_doc_mode():
    lines = [
        "INT. NOW - DAY", "Action.",
        "FLASHBACK:",
        "INT. THEN - NIGHT", "Memory.",
        "END FLASHBACK",
        "INT. NOW AGAIN - DAY", "Back.",
    ]
    w = script_doc.segment_lines(lines, "x.pdf")
    assert [s.is_flashback for s in w.scenes] == [False, True, False]


def test_unsupported_extension_errors():
    try:
        ingest.parse_one("script.rtf")
    except RuntimeError as e:
        assert ".rtf" in str(e) and ".fountain" in str(e)
    else:
        raise AssertionError("expected RuntimeError for unsupported extension")


# --- PDF round-trip -----------------------------------------------------------

def test_pdf_roundtrip_matches_fountain_parse():
    for name in ("ep101.fountain", "ep102.fountain"):
        expected = _expected(name)
        with tempfile.TemporaryDirectory() as td:
            pdf = pathlib.Path(td) / name.replace(".fountain", ".pdf")
            text_to_pdf(_fountain_text(name), pdf)
            got = script_doc.parse_pdf(str(pdf))
        assert [s.slug for s in got.scenes] == [s.slug for s in expected.scenes], name
        assert all(not s.is_flashback for s in got.scenes)


def test_pdf_strips_planted_and_keeps_dialogue():
    with tempfile.TemporaryDirectory() as td:
        pdf = pathlib.Path(td) / "ep102.pdf"
        text_to_pdf(_fountain_text("ep102.fountain"), pdf)
        w = script_doc.parse_pdf(str(pdf))
    assert w.stripped_planted == 4
    docks = w.scenes[0].raw_text
    assert "PLANTED" not in docks.upper()
    assert "Whoever it was, they didn't find the real ledger" in docks
    office = w.scenes[2].raw_text
    assert "watching the boats" in office


def test_pdf_with_no_text_raises_clear_error():
    with tempfile.TemporaryDirectory() as td:
        pdf = pathlib.Path(td) / "scan.pdf"
        text_to_pdf("", pdf)  # zero text objects = "scanned" stand-in
        try:
            script_doc.parse_pdf(str(pdf))
        except RuntimeError as e:
            assert "no extractable text" in str(e)
        else:
            raise AssertionError("expected RuntimeError for textless PDF")


# --- docx round-trip ------------------------------------------------------------

def test_docx_roundtrip_matches_fountain_parse():
    try:
        from docx import Document
    except ModuleNotFoundError:
        print("  (skipped: python-docx not installed)")
        return
    for name in ("ep101.fountain", "ep102.fountain"):
        expected = _expected(name)
        with tempfile.TemporaryDirectory() as td:
            path = pathlib.Path(td) / name.replace(".fountain", ".docx")
            doc = Document()
            for line in _fountain_text(name).splitlines():
                doc.add_paragraph(line)
            doc.save(str(path))
            got = script_doc.parse_docx(str(path))
        assert [s.slug for s in got.scenes] == [s.slug for s in expected.scenes], name
    assert got.stripped_planted == 4  # ep102 was last


# --- mixed-format world ---------------------------------------------------------

def test_mixed_fountain_plus_pdf_world_orders_and_positions():
    with tempfile.TemporaryDirectory() as td:
        td = pathlib.Path(td)
        shutil.copy(FIXTURES / "ep101.fountain", td / "ep101.fountain")
        text_to_pdf(_fountain_text("ep102.fountain"), td / "ep102.pdf")

        files = ingest.expand_inputs([str(td)])
        assert [pathlib.Path(f).name for f in files] == ["ep101.fountain", "ep102.pdf"]

        works = ingest.parse_works(files)
        assert [w.episode for w in works] == [101, 102]
        plan = ingest.assign_story_positions(works, start=1)
        positions = [pos for _, rows in plan for _, pos in rows]
        assert positions == list(range(1, 12))      # 6 + 5 across formats


if __name__ == "__main__":
    import traceback

    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    failed = 0
    for fn in fns:
        try:
            fn()
            print(f"PASS {fn.__name__}")
        except Exception:
            failed += 1
            print(f"FAIL {fn.__name__}")
            traceback.print_exc()
    raise SystemExit(1 if failed else 0)

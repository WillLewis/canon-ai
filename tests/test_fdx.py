"""FDX ingestion tests for the scene-record contract."""

from __future__ import annotations

from dataclasses import asdict
import pathlib
import sys
import tempfile
import xml.etree.ElementTree as ET

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from ingest.fdx import parse_fdx  # noqa: E402
from ingest.fountain import parse_fountain  # noqa: E402
from ingest.pipeline import ingest_files, scene_records  # noqa: E402

FIXTURES = ROOT / "fixtures" / "greyharbor"


def _paragraph(parent: ET.Element, ptype: str, text: str = "", **attrs) -> ET.Element:
    payload = {"Type": ptype}
    payload.update({key: str(value) for key, value in attrs.items() if value is not None})
    paragraph = ET.SubElement(parent, "Paragraph", payload)
    ET.SubElement(paragraph, "Text").text = text
    return paragraph


def _write_xml(root: ET.Element, path: pathlib.Path) -> pathlib.Path:
    ET.ElementTree(root).write(path, encoding="utf-8", xml_declaration=True)
    return path


def _fdx_root() -> ET.Element:
    return ET.Element("Final" + "Draft", {"DocumentType": "Script", "Version": "1"})


def _write_fdx_from_fountain(name: str, directory: pathlib.Path) -> pathlib.Path:
    source = FIXTURES / name
    work = parse_fountain(source.read_text(encoding="utf-8"), source_file=str(source))

    root = _fdx_root()
    title_page = ET.SubElement(root, "TitlePage")
    title_content = ET.SubElement(title_page, "Content")
    for key, value in work.title_page.items():
        _paragraph(title_content, "General", f"{key.title()}: {value}")

    content = ET.SubElement(root, "Content")
    for scene in work.scenes:
        for line_index, line in enumerate(scene.raw_text.splitlines()):
            if line_index == 0:
                _paragraph(content, "Scene Heading", line, Number=scene.scene_index)
            else:
                _paragraph(content, "Action", line)

    return _write_xml(root, directory / name.replace(".fountain", ".fdx"))


def test_greyharbor_fdx_scene_records_match_fountain_records():
    fountain_files = [str(FIXTURES / "ep101.fountain"), str(FIXTURES / "ep102.fountain")]
    expected = [asdict(record) for record in ingest_files(fountain_files)]

    with tempfile.TemporaryDirectory() as td:
        temp_dir = pathlib.Path(td)
        fdx_files = [
            _write_fdx_from_fountain("ep101.fountain", temp_dir),
            _write_fdx_from_fountain("ep102.fountain", temp_dir),
        ]
        got = [asdict(record) for record in ingest_files([str(path) for path in reversed(fdx_files)])]

    assert got == expected


def test_fdx_parser_strips_notes_omissions_and_keeps_script_text():
    root = _fdx_root()
    content = ET.SubElement(root, "Content")

    _paragraph(content, "Synopsis", "Do not ingest this synopsis.")
    _paragraph(content, "Scene Heading", "INT. START - DAY", Number="99")
    _paragraph(content, "Action", "Action survives.")
    note = ET.SubElement(content, "ScriptNote")
    _paragraph(note, "Note", "Do not ingest this note.")

    revised = ET.SubElement(content, "Paragraph", {"Type": "Action", "RevisionID": "7"})
    ET.SubElement(revised, "PageBreak", {"RevisionID": "7"})
    ET.SubElement(revised, "Text").text = "After revised-page marker."

    dual = ET.SubElement(content, "DualDialogue")
    _paragraph(dual, "Character", "MARA")
    _paragraph(dual, "Dialogue", "Left side.")
    _paragraph(dual, "Character", "COLE")
    _paragraph(dual, "Dialogue", "Right side.")

    omitted = _paragraph(content, "Scene Heading", "INT. OMITTED ROOM - NIGHT", Number="100")
    ET.SubElement(omitted, "SceneProperties", {"Omitted": "Yes"})
    _paragraph(content, "Action", "Omitted body must not appear.")

    _paragraph(content, "Scene Heading", "12A EXT. DOCKS - NIGHT", Number="12A")
    _paragraph(content, "Action", "[PLANTED: fixture marker] Real line.")
    _paragraph(content, "Action", "FLASHBACK:")
    _paragraph(content, "Scene Heading", "INT. MEMORY - NIGHT")
    _paragraph(content, "Action", "Memory action.")
    _paragraph(content, "Action", "END FLASHBACK")
    _paragraph(content, "Scene Heading", "INT. PRESENT - DAY")
    _paragraph(content, "Action", "Back in order.")

    with tempfile.TemporaryDirectory() as td:
        path = _write_xml(root, pathlib.Path(td) / "ep209_locked.fdx")
        work = parse_fdx(str(path))

    assert [scene.scene_index for scene in work.scenes] == [1, 2, 3, 4]
    assert [scene.slug for scene in work.scenes] == [
        "INT. START - DAY",
        "EXT. DOCKS - NIGHT",
        "INT. MEMORY - NIGHT",
        "INT. PRESENT - DAY",
    ]
    assert [scene.is_flashback for scene in work.scenes] == [False, False, True, False]
    assert work.stripped_planted == 1

    start_text = work.scenes[0].raw_text
    assert "Action survives." in start_text
    assert "After revised-page marker." in start_text
    assert "MARA\nLeft side.\nCOLE\nRight side." in start_text

    full_text = "\n".join(scene.raw_text for scene in work.scenes)
    for forbidden in (
        "synopsis",
        "Do not ingest this note",
        "OMITTED",
        "Omitted body",
        "PLANTED",
        "FLASHBACK",
    ):
        assert forbidden not in full_text
    assert "Real line." in full_text

    records = scene_records([work])
    assert [record.scene_id for record in records] == [
        "E209/sc1",
        "E209/sc2",
        "E209/sc3",
        "E209/sc4",
    ]
    assert [record.story_position for record in records] == [1, 2, 3, 4]

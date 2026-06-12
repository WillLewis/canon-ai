"""Parser tests — graded against the greyharbor fixtures (the Phase 0 harness).

Runnable two ways:
    python -m pytest -q
    python tests/test_fountain.py     # no pytest needed
"""

import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from canon.fountain import parse_fountain  # noqa: E402

FIXTURES = pathlib.Path(__file__).resolve().parents[1] / "fixtures" / "greyharbor"

EP101_SLUGS = [
    "INT. HARBORMASTER'S OFFICE - DAY",
    "EXT. GREYHARBOR DOCKS - NIGHT",
    "INT. CHAPEL ON THE POINT - NIGHT",
    "EXT. GREYHARBOR DOCKS - NIGHT",
    "INT. HARBORMASTER'S OFFICE - NIGHT",
    "EXT. CHAPEL ON THE POINT - NIGHT",
]
EP102_SLUGS = [
    "EXT. GREYHARBOR DOCKS - DAY",
    "INT. CHAPEL ON THE POINT - DAY",
    "INT. HARBORMASTER'S OFFICE - DAY",
    "EXT. COAST ROAD - DUSK",
    "EXT. EDDA'S SHACK - NIGHT",
]


def _parse(name):
    return parse_fountain((FIXTURES / name).read_text(encoding="utf-8"), source_file=name)


def test_ep101_scene_count_and_slugs():
    w = _parse("ep101.fountain")
    assert [s.slug for s in w.scenes] == EP101_SLUGS
    assert [s.scene_index for s in w.scenes] == [1, 2, 3, 4, 5, 6]


def test_ep102_scene_count_and_slugs():
    w = _parse("ep102.fountain")
    assert [s.slug for s in w.scenes] == EP102_SLUGS
    assert len(w.scenes) == 5


def test_title_page_parsed_not_treated_as_scene():
    w = _parse("ep101.fountain")
    assert w.show_title == "GREYHARBOR"
    assert w.episode == 101
    assert w.sort_order == 101
    assert w.title == 'E101 — The Ledger'
    # Title-page text must not leak into scene bodies.
    for s in w.scenes:
        assert "Credit:" not in s.raw_text
        assert "Original fixture material" not in s.raw_text


def test_no_flashbacks_in_greyharbor():
    # The planted errors depend on these scenes being NON-flashbacks.
    for name in ("ep101.fountain", "ep102.fountain"):
        for s in _parse(name).scenes:
            assert s.is_flashback is False


def test_planted_annotations_stripped():
    w101 = _parse("ep101.fountain")
    w102 = _parse("ep102.fountain")
    assert w101.stripped_planted == 0          # ep101 has none
    assert w102.stripped_planted == 4          # premature, destroyed, dead_speaker, capability
    for w in (w101, w102):
        for s in w.scenes:
            assert "PLANTED" not in s.raw_text.upper()
            assert "[PLANTED" not in s.raw_text


def test_inline_planted_keeps_real_dialogue():
    # The premature_knowledge tag is an INLINE prefix on Cole's line; stripping
    # it must NOT delete the dialogue the P2 check depends on.
    w = _parse("ep102.fountain")
    docks = w.scenes[0]
    assert "PLANTED" not in docks.raw_text.upper()
    assert "Whoever it was, they didn't find the real ledger" in docks.raw_text
    assert "under the chapel floor stone where your uncle hid it" in docks.raw_text


def test_raw_text_preserves_script_content_losslessly():
    # Key action/dialogue beats survive ingestion in the right scenes.
    w101 = _parse("ep101.fountain")
    assert "License is gone for good" in w101.scenes[0].raw_text          # cannot(Mara, drive)
    assert "slides a LEATHER LEDGER beneath it" in w101.scenes[2].raw_text  # ledger location
    assert "A gunshot." in w101.scenes[4].raw_text                         # dies(Tobias)
    assert "The chapel burns to its stones" in w101.scenes[5].raw_text     # destroyed(Chapel)
    w102 = _parse("ep102.fountain")
    assert "watching the boats" in w102.scenes[2].raw_text                 # dead Tobias speaks
    assert "behind the wheel of Tobias's pickup" in w102.scenes[3].raw_text  # Mara drives


def test_raw_text_starts_with_its_heading():
    for name in ("ep101.fountain", "ep102.fountain"):
        for s in _parse(name).scenes:
            assert s.slug in s.raw_text.splitlines()[0]


def test_synthetic_flashback_detection():
    text = (
        "Title: TEST\n\n"
        "INT. NOW - DAY\n\nAction here.\n\n"
        "FLASHBACK:\n\n"
        "INT. THEN - NIGHT\n\nYoung hero runs.\n\n"
        "END FLASHBACK\n\n"
        "INT. NOW AGAIN - DAY\n\nMore action.\n"
    )
    w = parse_fountain(text, source_file="synthetic")
    assert [s.slug for s in w.scenes] == ["INT. NOW - DAY", "INT. THEN - NIGHT", "INT. NOW AGAIN - DAY"]
    assert [s.is_flashback for s in w.scenes] == [False, True, False]
    # Transition markers are not content.
    for s in w.scenes:
        assert "FLASHBACK" not in s.raw_text.upper()


def test_slug_in_heading_flashback_detection():
    text = "INT. KITCHEN - DAY (FLASHBACK)\n\nA memory.\n"
    w = parse_fountain(text, source_file="synthetic2")
    assert len(w.scenes) == 1
    assert w.scenes[0].is_flashback is True


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

"""Bible assembly tests — all offline, seeded fake rows (no live Postgres).

The contract under test (canon/export/bible.py):
  - every fact line carries its scene citation; uncited facts are omitted,
    never invented;
  - status policy: retconned/rejected never appear, sealed appears (annotated),
    draft appears annotated as unconfirmed;
  - no entity is invented — only seeded entity rows appear, and provisional
    entities never get a character/location page;
  - the epistemic layer surfaces what-they-know with when-they-learned-it;
  - open questions reuse the hole-finder rows and obey the citation law.
"""

import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from canon.export import bible as bible_mod  # noqa: E402
from canon.export.render import render_bible_markdown  # noqa: E402


# --- seeded fake rows --------------------------------------------------------

def _scene(scene_id, pos, label):
    work, idx = label.split("/sc")
    return {
        "id": scene_id,
        "work_title": work,
        "slug": "INT. ROOM - DAY",
        "story_position": pos,
        "is_flashback": False,
        "scene_index": int(idx),
        "label": label,
    }


def _entity(eid, name, kind="character", provisional=False, aliases=()):
    return {"id": eid, "kind": kind, "name": name, "provisional": provisional,
            "aliases": list(aliases)}


def _assertion(aid, scene, subject, predicate, *, object_value=None, object_entity=None,
               quote="A supporting line.", status="canon", polarity=True, start_pos=None):
    return {
        "id": aid,
        "subject_id": subject["id"],
        "subject_name": subject["name"],
        "subject_kind": subject["kind"],
        "predicate": predicate,
        "object_id": object_entity["id"] if object_entity else None,
        "object_name": object_entity["name"] if object_entity else None,
        "object_kind": object_entity["kind"] if object_entity else None,
        "object_value": object_value,
        "object_assertion_id": None,
        "polarity": polarity,
        "start_pos": start_pos if start_pos is not None else scene["story_position"],
        "end_pos": None,
        "scene_id": scene["id"],
        "supporting_quote": quote,
        "confidence": 0.95,
        "status": status,
        "story_position": scene["story_position"],
    }


def _source(assertions, scenes, entities, presence=()):
    return {
        "world_id": 1,
        "world_name": "greyharbor",
        "works": [{"id": 1, "title": "E101", "sort_order": 1, "source_file": None}],
        "scenes": scenes,
        "entities": entities,
        "assertions": assertions,
        "presence": list(presence),
    }


def _fixture():
    mara = _entity(1, "Mara Voss", aliases=["Mara"])
    tobias = _entity(2, "Tobias Hale")
    ghost = _entity(3, "The Collector", provisional=True)  # referenced, never established
    chapel = _entity(4, "Chapel on the Point", kind="location")
    s1, s2, s3 = _scene(11, 1, "E101/sc1"), _scene(12, 2, "E101/sc2"), _scene(13, 3, "E101/sc3")
    assertions = [
        _assertion(1, s1, mara, "trait", object_value="stubborn", quote="She never backs down."),
        _assertion(2, s1, mara, "occupation", object_value="harbor pilot"),
        _assertion(3, s2, mara, "sibling_of", object_entity=tobias, quote="My brother Tobias."),
        _assertion(4, s2, mara, "goal", object_value="find the bell", quote="I will find the bell."),
        _assertion(5, s2, mara, "knows", object_value="the ledger is forged",
                   quote="She reads the forged ledger.", start_pos=2),
        _assertion(6, s3, chapel, "destroyed", quote="The chapel burns to the waterline."),
        _assertion(7, s1, mara, "cannot", object_value="swim", quote="Mara cannot swim."),
        _assertion(8, s3, tobias, "trait", object_value="retconned fact", status="retconned"),
        _assertion(9, s3, tobias, "trait", object_value="rejected fact", status="rejected"),
        _assertion(10, s3, tobias, "trait", object_value="sealed intentional fact",
                   quote="Tobias smiles anyway.", status="sealed"),
        _assertion(11, s3, tobias, "goal", object_value="draft ambition",
                   quote="I want the office.", status="draft"),
    ]
    presence = [{"scene_id": 11, "entity_id": 1}, {"scene_id": 13, "entity_id": 1},
                {"scene_id": 12, "entity_id": 2}]
    return _source(assertions, [s1, s2, s3], [mara, tobias, ghost, chapel], presence)


def _fact_lines(bible):
    lines = list(bible["timeline"]) + list(bible["world_rules"])
    for c in bible["characters"]:
        for section in ("traits", "occupation", "relationships", "goals", "knowledge"):
            lines.extend(c[section])
    for loc in bible["locations"]:
        lines.extend(loc["facts"])
        if loc["destroyed"]:
            lines.append(loc["destroyed"])
    return lines


# --- citation law ------------------------------------------------------------

def test_every_fact_line_carries_a_scene_citation():
    bible = bible_mod.assemble_bible(_fixture())
    lines = _fact_lines(bible)
    assert lines, "fixture should produce fact lines"
    for line in lines:
        assert line["label"], f"uncited line shipped: {line}"
        assert line["scene_id"], f"uncited line shipped: {line}"


def test_fact_with_unresolvable_scene_is_omitted_not_invented():
    source = _fixture()
    orphan_scene = _scene(999, 9, "E999/sc9")
    orphan = _assertion(50, orphan_scene, source["entities"][0], "trait",
                        object_value="orphaned fact")
    source["assertions"].append(orphan)  # scene 999 NOT in source["scenes"]
    bible = bible_mod.assemble_bible(source)

    md = render_bible_markdown(bible)
    assert "orphaned fact" not in md
    assert bible["omitted_uncited"] >= 1


def test_markdown_fact_bullets_all_cite():
    # metadata bullets (aliases, empty-section placeholders) are not story
    # facts; every other bullet in a fact section must carry a [work/scN] cite.
    metadata_prefixes = ("- Also referenced as:", "- No ", "- Standing (")
    md = render_bible_markdown(bible_mod.assemble_bible(_fixture()))
    in_fact_section = False
    for line in md.splitlines():
        if line.startswith("## "):
            in_fact_section = line[3:].strip() in {
                "Characters", "Locations", "Timeline", "World Rules", "Open Questions"}
        if in_fact_section and line.startswith("- ") and not line.startswith(metadata_prefixes):
            assert "[" in line and "]" in line, f"uncited bullet: {line}"


# --- status policy -----------------------------------------------------------

def test_retconned_and_rejected_never_appear():
    md = render_bible_markdown(bible_mod.assemble_bible(_fixture()))
    assert "retconned fact" not in md
    assert "rejected fact" not in md


def test_sealed_fact_appears_annotated():
    bible = bible_mod.assemble_bible(_fixture())
    md = render_bible_markdown(bible)
    assert "sealed intentional fact" in md
    assert "(sealed: writer marked intentional)" in md


def test_draft_fact_appears_annotated_unconfirmed():
    md = render_bible_markdown(bible_mod.assemble_bible(_fixture()))
    assert "draft ambition" in md
    assert "(unconfirmed extraction)" in md


# --- no entity invented ------------------------------------------------------

def test_only_seeded_entities_appear_and_provisional_get_no_page():
    source = _fixture()
    bible = bible_mod.assemble_bible(source)
    seeded = {e["name"] for e in source["entities"]}
    for c in bible["characters"]:
        assert c["name"] in seeded
    for loc in bible["locations"]:
        assert loc["name"] in seeded
    assert "The Collector" not in [c["name"] for c in bible["characters"]]


# --- section content ---------------------------------------------------------

def test_character_page_sections():
    bible = bible_mod.assemble_bible(_fixture())
    mara = next(c for c in bible["characters"] if c["name"] == "Mara Voss")
    assert any("stubborn" in r["text"] for r in mara["traits"])
    assert any("harbor pilot" in r["text"] for r in mara["occupation"])
    assert any("Tobias Hale" in r["text"] for r in mara["relationships"])
    assert any("find the bell" in r["text"] for r in mara["goals"])
    assert mara["first_appearance"]["label"] == "E101/sc1"
    assert mara["last_appearance"]["label"] == "E101/sc3"


def test_knowledge_carries_when_learned():
    bible = bible_mod.assemble_bible(_fixture())
    mara = next(c for c in bible["characters"] if c["name"] == "Mara Voss")
    knows = [r for r in mara["knowledge"] if "ledger is forged" in r["text"]]
    assert knows and "learned at pos 2" in knows[0]["text"]


def test_location_destroyed_status_cited():
    bible = bible_mod.assemble_bible(_fixture())
    chapel = next(l for l in bible["locations"] if l["name"] == "Chapel on the Point")
    assert chapel["destroyed"] is not None
    assert chapel["destroyed"]["label"] == "E101/sc3"


def test_timeline_is_story_position_ordered():
    bible = bible_mod.assemble_bible(_fixture())
    positions = [r["position"] for r in bible["timeline"]]
    assert positions == sorted(positions)


def test_world_rules_hold_cannot_facts():
    bible = bible_mod.assemble_bible(_fixture())
    assert any("cannot" in r["text"] and "swim" in r["text"] for r in bible["world_rules"])


# --- open questions reuse the holes machinery --------------------------------

def test_open_questions_group_cited_holes_and_drop_uncited():
    holes = [
        {"category": "open_thread", "question": "Does Mara ever follow through?",
         "detail": "Set up at pos 2.", "scene_id": 12, "cite": "E101/sc2"},
        {"category": "unknown_entity", "question": "Who is The Collector?",
         "detail": "Referenced, never established.", "scene_id": 13, "cite": None},
    ]
    bible = bible_mod.assemble_bible(_fixture(), holes)
    groups = {g["label"]: g for g in bible["open_questions"]}
    assert "Open threads" in groups
    assert any("follow through" in i["question"] for i in groups["Open threads"]["items"])
    # the uncited hole is dropped, not rendered citation-less
    assert "Unestablished references" not in groups
    assert bible["omitted_uncited"] >= 1

"""Extraction tests — all offline (no API key needed).

Pure logic (schema, prompts, synopsis, quality gates) plus the full
scene-by-scene orchestration driven by a fake Anthropic client.

    python -m pytest -q
    python tests/test_extract.py
"""

import json
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from canon import extract  # noqa: E402
from canon import ingest  # noqa: E402

FIXTURES = pathlib.Path(__file__).resolve().parents[1] / "fixtures" / "greyharbor"
FILES = [str(FIXTURES / "ep101.fountain"), str(FIXTURES / "ep102.fountain")]


# --- fake Anthropic client -------------------------------------------------

class FakeBlock:
    def __init__(self, type_, text=""):
        self.type = type_
        self.text = text


class FakeResponse:
    def __init__(self, payload, *, thinking=False, stop_reason="end_turn"):
        self.content = ([FakeBlock("thinking", "...")] if thinking else []) + [
            FakeBlock("text", payload)
        ]
        self.stop_reason = stop_reason


class FakeMessages:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def create(self, **kwargs):
        self.calls.append(kwargs)
        return self.responses.pop(0)


class FakeClient:
    def __init__(self, responses):
        self.messages = FakeMessages(responses)


def _assertion(subject, predicate, quote, *, object_value=None, object_entity=None,
               polarity=True, confidence=0.95, starts_here=True, ends_here=False, notes=""):
    return {
        "subject": subject, "predicate": predicate,
        "object_entity": object_entity, "object_value": object_value, "object_fact_ref": None,
        "polarity": polarity, "starts_here": starts_here, "ends_here": ends_here,
        "supporting_quote": quote, "confidence": confidence, "notes": notes,
    }


def _payload(assertions=None, presence=None, deaths=None, destructions=None, open_questions=None):
    return json.dumps({
        "assertions": assertions or [],
        "scene_presence": presence or [],
        "deaths": deaths or [],
        "destructions": destructions or [],
        "open_questions": open_questions or [],
    })


# --- schema & vocabulary ---------------------------------------------------

def test_predicate_vocabulary_is_closed_and_has_fact():
    assert "fact" in extract.PREDICATES
    for p in ("dies", "destroyed", "knows", "believes", "cannot", "located_at", "promised"):
        assert p in extract.PREDICATES


def test_schema_shape_is_structured_output_safe():
    schema = extract.candidate_schema()
    assert schema["additionalProperties"] is False
    assert set(schema["required"]) == {
        "assertions", "scene_presence", "deaths", "destructions", "open_questions"
    }
    a = schema["properties"]["assertions"]["items"]
    assert a["additionalProperties"] is False
    assert a["properties"]["predicate"]["enum"] == list(extract.PREDICATES)
    # every property is required, and the object is fully specified
    assert set(a["required"]) == set(a["properties"].keys())


def test_build_request_omits_temperature_and_uses_structured_output():
    req = extract.build_request("claude-opus-4-8", "sys", "user", effort="high")
    assert "temperature" not in req and "top_p" not in req and "top_k" not in req
    assert req["model"] == "claude-opus-4-8"
    assert req["output_config"]["effort"] == "high"
    assert req["output_config"]["format"]["type"] == "json_schema"
    assert req["thinking"] == {"type": "adaptive"}
    no_think = extract.build_request("m", "s", "u", thinking=False)
    assert "thinking" not in no_think


def test_system_prompt_encodes_core_rules():
    sp = extract.SYSTEM_PROMPT
    assert "never write, invent" in sp           # D1 guardrail
    assert "VERBATIM" in sp                       # quote requirement
    assert "believes" in sp and "knows" in sp     # epistemic distinction
    assert "BACKDATING" in sp


# --- prompts & synopsis ----------------------------------------------------

def test_user_prompt_includes_scene_text_position_and_synopsis():
    ctx = extract.SceneContext("E101", 3, "INT. CHAPEL - NIGHT", 3, False, "Tobias hides the ledger.")
    prompt = extract.build_user_prompt(ctx, "PRIOR FACT LINE")
    assert "INT. CHAPEL - NIGHT" in prompt
    assert "Tobias hides the ledger." in prompt
    assert "story_position: 3" in prompt
    assert "PRIOR FACT LINE" in prompt
    fb = extract.build_user_prompt(
        extract.SceneContext("E1", 1, "INT. X", 1, True, "x"), ""
    )
    assert "[FLASHBACK]" in fb


def test_synopsis_renders_prior_facts_and_presence():
    prior = [extract.SceneExtraction(
        work_title="E101", scene_index=1, slug="INT. OFFICE - DAY", story_position=1,
        is_flashback=False,
        assertions=[_assertion("Mara", "cannot", "License is gone for good.", object_value="drive")],
        scene_presence=["Mara", "Tobias"],
    )]
    syn = extract.build_synopsis(prior)
    assert "Mara cannot drive" in syn
    assert "present: Mara, Tobias" in syn


# --- quality gates ---------------------------------------------------------

def test_post_process_drops_unquoted_objectless_and_unverified():
    ctx = extract.SceneContext("E1", 1, "INT. X", 1, False, "Mara counts the boats by the dock.")
    obj = {"assertions": [
        _assertion("Mara", "knows", "", object_value="something"),                 # no quote
        _assertion("Mara", "knows", "counts the boats", object_value=None),          # no object (relational)
        _assertion("Mara", "fact", "this line is not in the scene", object_value="x"),  # unverified quote
        _assertion("Mara", "located_at", "by the dock", object_entity="Dock"),       # keeper
    ]}
    kept, dropped = extract.post_process(ctx, obj, verify_quotes=True)
    assert len(kept) == 1 and kept[0]["predicate"] == "located_at"
    assert dropped == {"no_quote": 1, "bad_predicate": 0, "no_object": 1, "quote_unverified": 1}


def test_post_process_keeps_intransitive_predicates_without_object():
    ctx = extract.SceneContext("E1", 5, "INT. OFFICE - NIGHT", 5, False, "A gunshot. Tobias slumps. Dead.")
    obj = {"assertions": [_assertion("Tobias", "dies", "A gunshot.", object_value=None)]}
    kept, dropped = extract.post_process(ctx, obj)
    assert len(kept) == 1
    assert sum(dropped.values()) == 0


def test_post_process_clamps_confidence():
    ctx = extract.SceneContext("E1", 1, "INT. X", 1, False, "the chapel burns to its stones")
    obj = {"assertions": [_assertion("Chapel", "destroyed", "burns to its stones", confidence=5.0)]}
    kept, _ = extract.post_process(ctx, obj)
    assert kept[0]["confidence"] == 1.0


def test_first_text_skips_thinking_block():
    resp = FakeResponse(_payload(), thinking=True)
    assert json.loads(extract.first_text(resp)) == json.loads(_payload())


# --- orchestration end-to-end (fake client) --------------------------------

def _responses_for_11_scenes():
    blank = _payload()
    responses = [blank] * 11
    # pos1 (ep101 sc1): cannot(Mara, drive)
    responses[0] = _payload(
        assertions=[_assertion("Mara", "cannot", "License is gone for good.", object_value="drive")],
        presence=["Mara", "Tobias"],
    )
    # pos5 (ep101 sc5): dies(Tobias) — intransitive, no object
    responses[4] = _payload(
        assertions=[_assertion("Tobias", "dies", "A gunshot.", object_value=None)],
        presence=["Tobias"], deaths=["Tobias"],
    )
    # pos6 (ep101 sc6): destroyed(Chapel) — intransitive, no object
    responses[5] = _payload(
        assertions=[_assertion("Chapel on the Point", "destroyed", "The chapel burns to its stones")],
        destructions=["Chapel on the Point"],
    )
    return [FakeResponse(r) for r in responses]


def test_run_extraction_threads_synopsis_and_shapes_output():
    works = ingest.parse_works(FILES)
    client = FakeClient(_responses_for_11_scenes())
    results = extract.run_extraction(client, works, verify_quotes=True)

    assert len(results) == 11
    assert [r.story_position for r in results] == list(range(1, 12))

    # Planted facts survive the quality gates (quotes are real substrings).
    assert results[0].assertions[0]["predicate"] == "cannot"
    assert results[4].assertions[0]["predicate"] == "dies"
    assert results[5].assertions[0]["predicate"] == "destroyed"

    # Rolling synopsis: scene 2's prompt carries scene 1's extracted fact.
    second_user_prompt = client.messages.calls[1]["messages"][0]["content"]
    assert "Mara cannot drive" in second_user_prompt

    # Every call used structured output and never sent temperature.
    for call in client.messages.calls:
        assert "temperature" not in call
        assert call["output_config"]["format"]["type"] == "json_schema"


def test_run_extraction_respects_limit():
    works = ingest.parse_works(FILES)
    client = FakeClient(_responses_for_11_scenes()[:3])
    results = extract.run_extraction(client, works, limit=3)
    assert len(results) == 3
    assert len(client.messages.calls) == 3


def test_to_json_carries_scene_citations():
    works = ingest.parse_works(FILES)
    client = FakeClient(_responses_for_11_scenes())
    results = extract.run_extraction(client, works)
    out = extract.to_json("greyharbor", "claude-opus-4-8", results)
    assert out["world"] == "greyharbor"
    assert out["model"] == "claude-opus-4-8"
    s5 = out["scenes"][4]
    assert s5["story_position"] == 5
    assert s5["slug"] == "INT. HARBORMASTER'S OFFICE - NIGHT"
    assert s5["work_title"].startswith("E101")
    assert "dropped" in s5  # provenance of what the gates removed


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

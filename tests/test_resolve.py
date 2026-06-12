"""Entity-resolution tests — all offline.

Pure helpers + deterministic matching + the LLM pass (fake client) + confirm/merge,
plus an integration test that scores resolved output through the REAL
eval/run_eval.py grader (PLAN.md step 6).

    python -m pytest -q
    python tests/test_resolve.py
"""

import importlib.util
import json
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from canon import resolve  # noqa: E402

# Load the real eval grader by path (eval/ has no __init__.py).
_spec = importlib.util.spec_from_file_location("run_eval", ROOT / "eval" / "run_eval.py")
run_eval = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(run_eval)


# --- fake disambiguation client -------------------------------------------

class _Block:
    def __init__(self, text):
        self.type = "text"
        self.text = text


class _Resp:
    def __init__(self, payload):
        self.content = [_Block(payload)]
        self.stop_reason = "end_turn"


class FakeResolveClient:
    """Maps any reference containing 'deputy' to the existing 'Cole' entity."""

    def __init__(self):
        self.messages = self
        self.calls = []

    def create(self, **kwargs):
        self.calls.append(kwargs)
        prompt = kwargs["messages"][0]["content"]
        if "deputy" in prompt.lower():
            dec = {"decision": "existing", "entity_name": "Cole", "canonical_name": None,
                   "kind": None, "alias_kind": "role_reference", "dossier": None,
                   "confidence": 0.95, "reason": "the deputy = Cole"}
        else:
            dec = {"decision": "unsure", "entity_name": None, "canonical_name": None,
                   "kind": None, "alias_kind": None, "dossier": None,
                   "confidence": 0.2, "reason": "unclear"}
        return _Resp(json.dumps(dec))


# --- greyharbor candidate fixture (messy surfaces, covers A1–A15) ----------

def _a(subject, predicate, *, object_value=None, object_entity=None):
    return {"subject": subject, "predicate": predicate, "object_entity": object_entity,
            "object_value": object_value, "supporting_quote": "q", "confidence": 0.95,
            "polarity": True, "starts_here": True, "ends_here": False}


def _scene(work, idx, pos, slug, assertions, presence=(), deaths=(), destructions=()):
    return {"work_title": work, "scene_index": idx, "story_position": pos, "slug": slug,
            "assertions": assertions, "scene_presence": list(presence),
            "deaths": list(deaths), "destructions": list(destructions)}


def _candidates():
    E1, E2 = "E101 — The Ledger", "E102 — Thursday Numbers"
    return {"world": "greyharbor", "scenes": [
        _scene(E1, 1, 1, "INT. HARBORMASTER'S OFFICE - DAY", [
            _a("Mara", "cannot", object_value="drive a truck"),
            _a("Tobias", "occupation", object_value="harbormaster"),
        ], presence=["Mara", "Tobias"]),
        _scene(E1, 2, 2, "EXT. GREYHARBOR DOCKS - NIGHT", [
            _a("Edda", "knows", object_value="keeps two sets of books"),
            _a("Edda", "believes", object_value="the real one's not in that office"),
            _a("Edda", "occupation", object_value="cook"),
        ], presence=["Mara", "Edda"]),
        _scene(E1, 3, 3, "INT. CHAPEL ON THE POINT - NIGHT", [
            _a("ledger", "located_at", object_entity="chapel"),
            _a("Tobias", "knows", object_value="where Tobias hid the ledger"),
        ], presence=["Tobias"]),
        _scene(E1, 4, 4, "EXT. GREYHARBOR DOCKS - NIGHT", [
            _a("Mara", "promised", object_value="find Danny"),
        ], presence=["Mara"]),
        _scene(E1, 5, 5, "INT. HARBORMASTER'S OFFICE - NIGHT", [
            _a("Tobias", "dies"),
        ], presence=["Tobias"], deaths=["Tobias"]),
        _scene(E1, 6, 6, "EXT. CHAPEL ON THE POINT - NIGHT", [
            _a("chapel", "destroyed"),
        ], destructions=["chapel"]),
        _scene(E2, 1, 7, "EXT. GREYHARBOR DOCKS - DAY", [
            _a("Cole", "occupation", object_value="deputy"),
            _a("the deputy", "knows", object_value="hiding place of the ledger"),
            _a("Mara", "knows", object_value="ledger under the chapel floor stone"),
        ], presence=["Mara", "Cole"]),
        _scene(E2, 2, 8, "INT. CHAPEL ON THE POINT - DAY", [
            _a("Mara", "possesses", object_entity="leather ledger"),
        ], presence=["Mara"]),
        _scene(E2, 5, 11, "EXT. EDDA'S SHACK - NIGHT", [
            _a("Mara", "knows", object_value="c.b. four hundred"),
        ], presence=["Mara", "Edda"]),
    ]}


# --- pure helpers ----------------------------------------------------------

def test_norm_and_display_and_initials():
    assert resolve._norm("  The Old Chapel! ") == "the old chapel"
    assert resolve.display_name("MARA VOSS") == "Mara Voss"
    assert resolve.display_name("chapel") == "chapel"
    assert resolve.is_initials("C.B.") and not resolve.is_initials("Cole")


def test_role_reference_detection():
    assert resolve.is_role_reference("the deputy")
    assert resolve.is_role_reference("uncle")
    assert resolve.is_role_reference("C.B.")
    assert not resolve.is_role_reference("Mara")
    assert not resolve.is_role_reference("Chapel on the Point")


def test_kind_inference():
    assert resolve.occ_kind("presence", None) == "character"
    assert resolve.occ_kind("subject", "dies") == "character"
    assert resolve.occ_kind("object", "located_at") == "location"
    assert resolve.occ_kind("subject", "located_at") == "object"


# --- deterministic matching ------------------------------------------------

def test_registry_exact_fuzzy_and_ambiguous():
    reg = resolve.Registry()
    mara = reg.add("Mara Voss", "character", None, False)
    tobias = reg.add("Tobias Voss", "character", None, False)
    assert reg.match("mara voss") == ("exact", mara)
    # "Mara" is a token-subset of "Mara Voss"
    assert reg.match("Mara") == ("fuzzy", mara)
    # bare surname is shared -> ambiguous
    status, cands = reg.match("Voss")
    assert status == "ambiguous" and set(cands) == {mara, tobias}
    assert reg.match("Edda") == ("none", None)


def test_add_alias_upgrades_canonical_to_richer_form():
    reg = resolve.Registry()
    e = reg.add("ledger", "object", None, False)
    reg.add_alias(e, "leather ledger")
    assert e.name == "leather ledger"
    assert "ledger" in {a.lower() for a, _ in e.aliases}


# --- mentions --------------------------------------------------------------

def test_collect_and_group_mentions():
    surfaces = {s.norm: s for s in resolve.group_surfaces(resolve.collect_mentions(_candidates()))}
    assert "mara" in surfaces and surfaces["mara"].kind_hint == "character"
    assert "chapel" in surfaces and surfaces["chapel"].kind_hint == "location"
    assert surfaces["the deputy"].is_role is True


# --- LLM pass --------------------------------------------------------------

def test_resolve_with_llm_maps_role_reference_to_existing_entity():
    client = FakeResolveClient()
    state = resolve.resolve_candidates(_candidates(), client=client)
    names = {resolve._norm(e.name) for e in state.registry.entities}
    assert "cole" in names
    assert "the deputy" not in names                  # folded into Cole, not its own entity
    a10 = next(a for a in state.assertions if a["predicate"] == "knows"
               and a["object_value"] == "hiding place of the ledger")
    assert resolve._norm(a10["subject"]) == "cole"
    assert len(client.calls) == 1                     # only the deputy needed the LLM


def test_resolved_output_passes_eval_recall_gate():
    state = resolve.resolve_candidates(_candidates(), client=FakeResolveClient())
    acts = resolve.to_eval_assertions(state)["assertions"]
    recall, matched, missed = run_eval.score_extraction(acts)
    assert recall == 1.0, f"missed {missed}"


def test_no_llm_queues_role_reference_but_still_clears_gate():
    state = resolve.resolve_candidates(_candidates(), client=None)
    queued = {q.surface.lower() for q in state.queue}
    assert "the deputy" in queued
    recall, _, _ = run_eval.score_extraction(resolve.to_eval_assertions(state)["assertions"])
    assert recall >= 0.80                              # graceful degradation stays above the gate


# --- confirm + merge -------------------------------------------------------

def test_confirm_assign_to_existing_entity_repoints_assertions():
    state = resolve.resolve_candidates(_candidates(), client=None)
    item = next(q for q in state.queue if q.surface.lower() == "the deputy")
    handled = resolve.apply_confirm_decision(state, item, "= Cole")
    assert handled
    a10 = next(a for a in state.assertions if a["object_value"] == "hiding place of the ledger")
    assert resolve._norm(a10["subject"]) == "cole"
    assert state.registry.by_name("the deputy").name.lower() == "cole"  # alias now


def test_confirm_promote_provisional_to_new_entity():
    state = resolve.resolve_candidates(_candidates(), client=None)
    item = next(q for q in state.queue if q.surface.lower() == "the deputy")
    handled = resolve.apply_confirm_decision(state, item, "new Cole Brannigan | character")
    assert handled
    e = state.registry.by_name("Cole Brannigan")
    assert e is not None and e.provisional is False and e.kind == "character"


def test_merge_is_reflected_in_assertions_and_logged():
    state = resolve.resolve_candidates(_candidates(), client=None)
    # "the deputy" resolved to its own provisional entity under --no-llm
    assert resolve.merge_entities(state, "Cole", "the deputy", "manual")
    a10 = next(a for a in state.assertions if a["object_value"] == "hiding place of the ledger")
    assert resolve._norm(a10["subject"]) == "cole"
    assert state.merges and state.merges[-1]["drop"].lower() == "the deputy"
    assert not any(q.surface.lower() == "the deputy" for q in state.queue)  # stale item pruned


def test_state_round_trips_through_json():
    state = resolve.resolve_candidates(_candidates(), client=FakeResolveClient())
    d = resolve.state_to_dict(state)
    restored = resolve.state_from_dict(json.loads(json.dumps(d)))
    assert len(restored.registry.entities) == len(state.registry.entities)
    assert len(restored.assertions) == len(state.assertions)
    assert restored.world == "greyharbor"


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

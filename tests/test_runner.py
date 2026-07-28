"""Pipeline runner tests (P3-FRONTDOOR) — all offline.

canon/runner.py with a FakeClient (tests/test_extract.py pattern) and the
store/check/report stages faked at the runner's module seams: phase ordering,
per-scene events with running counts, the mid-extraction check cycle emitting
the first finding, failure -> resumable candidates, resume skipping done
scenes, and the CostGuard abort firing the ops alert.

    python -m pytest -q tests/test_runner.py
"""

import contextlib
import json
import pathlib
import sys
from types import SimpleNamespace

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from canon import runner  # noqa: E402


# --- fakes (tests/test_extract.py pattern) -----------------------------------------

class FakeBlock:
    def __init__(self, type_, text):
        self.type = type_
        self.text = text


class FakeUsage:
    def __init__(self, tokens_in, tokens_out):
        self.input_tokens = tokens_in
        self.output_tokens = tokens_out


class FakeResponse:
    def __init__(self, payload, tokens_in=100, tokens_out=50, model="claude-sonnet-5"):
        self.content = [FakeBlock("text", payload)]
        self.stop_reason = "end_turn"
        self.usage = FakeUsage(tokens_in, tokens_out)
        self.model = model


class FakeMessages:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def create(self, **kwargs):
        self.calls.append(kwargs)
        item = self.responses.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


class FakeClient:
    def __init__(self, responses):
        self.messages = FakeMessages(responses)


def scene_payload(subject, quote, facts=1):
    assertions = [{
        "subject": subject, "predicate": "fact",
        "object_entity": None, "object_value": f"thing {i}", "object_fact_ref": None,
        "polarity": True, "starts_here": True, "ends_here": False,
        "supporting_quote": quote, "confidence": 0.95, "notes": None,
    } for i in range(facts)]
    return json.dumps({"assertions": assertions, "scene_presence": [subject],
                       "deaths": [], "destructions": [], "open_questions": []})


def make_works(n_scenes):
    scenes = [SimpleNamespace(scene_index=i, slug=f"INT. PLACE {i} - DAY",
                              is_flashback=False,
                              raw_text=f"MARA\nline of scene {i}.")
              for i in range(1, n_scenes + 1)]
    return [SimpleNamespace(title="E101 — Test", source_file="e101.fountain",
                            episode=101, sort_order=1, scenes=scenes)]


def responses_for(n_scenes, facts=1):
    return [FakeResponse(scene_payload("Mara", f"line of scene {i}.", facts))
            for i in range(1, n_scenes + 1)]


class Emitted(list):
    def __call__(self, kind, data):
        self.append((kind, data))

    def kinds(self):
        return [k for k, _ in self]

    def of(self, kind):
        return [d for k, d in self if k == kind]


@contextlib.contextmanager
def stage_fakes(findings_per_check=None):
    """Fake the store/check/report seams on the runner module; restore after.
    findings_per_check: list popped per run_checks call (last value sticks)."""
    queue = list(findings_per_check or [])
    calls = {"store": [], "checks": 0, "persist": [], "report": []}
    saved = {n: getattr(runner, n) for n in
             ("store_state", "run_checks", "persist_findings", "run_report_notes")}

    def fake_store(conn, world, state_dict, *, reset=False, **kw):
        calls["store"].append({"world": world, "reset": reset,
                               "n_assertions": len(state_dict.get("assertions") or [])})
        return {"world_id": 1, "entities": len(state_dict.get("entities") or []),
                "assertions": len(state_dict.get("assertions") or []),
                "aliases": 0, "scene_presence": 0, "character_locations": 0,
                "skipped": 0, "deduped": 0}

    def fake_checks(conn, world_id, checks_sql):
        calls["checks"] += 1
        current = queue.pop(0) if len(queue) > 1 else (queue[0] if queue else [])
        return list(current), []

    runner.store_state = fake_store
    runner.run_checks = fake_checks
    runner.persist_findings = lambda conn, wid, findings, clear=True: (
        calls["persist"].append(len(findings)))
    runner.run_report_notes = lambda conn, wid, **kw: (
        calls["report"].append(wid) or ([], [], {}, {}))
    try:
        yield calls
    finally:
        for name, fn in saved.items():
            setattr(runner, name, fn)


def fake_conn_factory():
    return SimpleNamespace(close=lambda: None)


def run(works, responses, *, emit=None, findings=None, **kw):
    emit = emit if emit is not None else Emitted()
    with stage_fakes(findings) as calls:
        result = runner.run_pipeline(
            fake_conn_factory, "testworld", works,
            client_factory=lambda: FakeClient(responses), emit=emit,
            checks_sql="-- faked", **kw)
    return result, emit, calls


FINDING = {"check": "dead_speaker", "severity": "critical",
           "explanation": "Tobias speaks after dying at pos 2.",
           "scene_id": 5, "assertion_a": 21, "assertion_b": 22, "sealed": False}


# --- phase ordering + scene events ----------------------------------------------

def test_happy_path_phase_ordering_and_done():
    result, emit, calls = run(make_works(4), responses_for(4), findings=[[]])
    assert result["status"] == "done"
    assert result["report_url"] == "/worlds/1/report"
    phases = [d["phase"] for d in emit.of("phase")]
    assert phases == ["extracting", "resolving", "checking", "reporting", "done"]
    # scene events precede resolving; done is the last event
    kinds = emit.kinds()
    assert kinds[-1] == "done"
    assert kinds.index("scene") < kinds.index("done")
    assert calls["persist"] == [0]          # final findings persisted once
    assert calls["report"] == [1]           # report notes ran for the world


def test_scene_events_carry_running_counts():
    _, emit, _ = run(make_works(3), responses_for(3, facts=2), findings=[[]])
    scenes = emit.of("scene")
    assert [s["index"] for s in scenes] == [1, 2, 3]
    assert all(s["total"] == 3 for s in scenes)
    assert [s["facts_total"] for s in scenes] == [2, 4, 6]
    assert scenes[0]["facts_scene"] == 2
    assert scenes[0]["slug"] == "INT. PLACE 1 - DAY"
    assert "open_questions" in scenes[0]


def test_stat_line_precedes_done():
    _, emit, _ = run(make_works(3), responses_for(3), findings=[[]])
    kinds = emit.kinds()
    assert kinds.index("stat") < kinds.index("done")
    stat = emit.of("stat")[0]
    assert set(stat) == {"facts_total", "entities", "scenes"}
    assert stat["scenes"] == 3


# --- check cycles ---------------------------------------------------------------

def test_check_cycle_emits_first_finding_before_extraction_completes():
    # 5 scenes; the cycle at scene 3 returns the finding -> it must be emitted
    # before the scene-4 event, flagged first:true, and never re-emitted when
    # the final full check returns it again.
    result, emit, calls = run(make_works(5), responses_for(5),
                              findings=[[FINDING]])
    assert result["status"] == "done"
    findings = emit.of("finding")
    assert len(findings) == 1
    assert findings[0]["first"] is True
    assert findings[0]["check"] == "dead_speaker"
    order = emit.kinds()
    scene_indexes = [i for i, k in enumerate(order) if k == "scene"]
    assert order.index("finding") < scene_indexes[3]   # before scene 4
    assert calls["checks"] >= 2                        # mid-cycle + final


def test_only_first_finding_is_flagged_first():
    other = dict(FINDING, check="presence_conflict",
                 explanation="Mara in two places at pos 3.")
    _, emit, _ = run(make_works(5), responses_for(5),
                     findings=[[FINDING, other]])
    firsts = [f["first"] for f in emit.of("finding")]
    assert firsts == [True, False]


def test_sealed_findings_never_surface_in_the_theater():
    sealed = dict(FINDING, sealed=True)
    _, emit, _ = run(make_works(5), responses_for(5), findings=[[sealed]])
    assert emit.of("finding") == []


def test_check_cadence_and_should_check():
    assert [i for i in range(1, 22) if runner._should_check(i, 22, 6)] == [3, 9, 15, 21]
    assert runner._should_check(3, 3, 6) is False      # never on the last scene
    with_env = runner.check_cadence(2)
    assert with_env == 2


# --- failure + resume -------------------------------------------------------------

def test_failure_midway_is_resumable_with_candidates():
    responses = responses_for(2) + [RuntimeError("api down")]
    result, emit, _ = run(make_works(4), responses, findings=[[]])
    assert result["status"] == "failed"
    assert result["resumable"] is True
    assert result["scenes_done"] == 2
    assert len(result["candidates"]) == 2
    assert result["candidates"][0]["slug"] == "INT. PLACE 1 - DAY"
    err = emit.of("error")[0]
    assert err["resumable"] is True and err["scenes_done"] == 2
    assert "api down" in err["message"]


def test_resume_skips_done_scenes_and_rebuilds_synopsis():
    # Fail at scene 3 of 4, then resume from the returned candidates: the
    # resumed run must only call the API for scenes 3 and 4, with scene 1's
    # facts present in the rolling synopsis.
    first, _, _ = run(make_works(4), responses_for(2) + [RuntimeError("boom")],
                      findings=[[]])
    remaining = [FakeResponse(scene_payload("Mara", f"line of scene {i}."))
                 for i in (3, 4)]
    client = FakeClient(remaining)
    emit = Emitted()
    with stage_fakes([[]]):
        result = runner.run_pipeline(
            fake_conn_factory, "testworld", make_works(4),
            client_factory=lambda: client, emit=emit,
            checks_sql="-- faked", resume_from=first["candidates"])
    assert result["status"] == "done"
    assert len(client.messages.calls) == 2
    prompt = client.messages.calls[0]["messages"][0]["content"]
    assert "PRIOR SCENES" in prompt and "sc1" in prompt
    assert [s["index"] for s in emit.of("scene")] == [3, 4]
    assert emit.of("scene")[0]["facts_total"] == 3     # 2 resumed + 1 new


# --- cost guard --------------------------------------------------------------------

def test_cost_guard_abort_fires_alert_and_is_not_resumable(monkeypatch):
    alerts_fired = []
    monkeypatch.setattr(runner.alerts, "alert",
                        lambda event, payload=None: alerts_fired.append((event, payload)))
    # Each response costs ~$0.001 at sonnet pricing; a $0.0005 cap aborts on
    # the first scene.
    result, emit, _ = run(make_works(4), responses_for(4),
                          findings=[[]], cost_cap_usd=0.0005)
    assert result["status"] == "aborted"
    assert result["resumable"] is False
    aborts = emit.of("cost_abort")
    assert len(aborts) == 1
    assert aborts[0]["cap_usd"] == 0.0005
    assert aborts[0]["cost_usd"] > aborts[0]["cap_usd"]
    assert [e for e, _ in alerts_fired] == ["run_cogs_exceeded"]
    assert emit.of("error") == []                      # abort is not an error event


def test_cost_guard_accumulates_across_calls():
    guard = runner.CostGuard(FakeClient(responses_for(3)), cap_usd=1.0)
    for _ in range(3):
        guard.messages.create(model="claude-sonnet-5", messages=[])
    # 3 * (100 in + 50 out) at $3/$15 per Mtok
    assert float(guard.cost) == (3 * (100 * 3 + 50 * 15)) / 1_000_000


# --- pure diff helpers ---------------------------------------------------------------

def test_diff_findings_is_pure_and_keyed_on_check_and_explanation():
    emitted: set = set()
    new, updated = runner.diff_findings(emitted, [FINDING])
    assert [f["check"] for f in new] == ["dead_speaker"]
    assert emitted == set()                            # input untouched
    again, final = runner.diff_findings(updated, [FINDING])
    assert again == []
    # same check, new explanation -> a new finding
    variant = dict(FINDING, explanation="Tobias speaks again at pos 9.")
    more, _ = runner.diff_findings(final, [variant])
    assert len(more) == 1


def test_diff_aliases_reports_only_new_links():
    ent = SimpleNamespace(name="Cole", kind="character",
                          aliases=[["Cole", "name_variant"], ["the deputy", "role_reference"]])
    state = SimpleNamespace(registry=SimpleNamespace(entities=[ent]))
    events, seen = runner.diff_aliases(set(), state)
    assert events == [{"name": "Cole", "alias": "the deputy", "kind": "character"}]
    events2, _ = runner.diff_aliases(seen, state)
    assert events2 == []


def test_web_model_and_effort_env_overrides(monkeypatch):
    assert runner.web_model() == "claude-sonnet-5"
    assert runner.web_effort() == "medium"
    monkeypatch.setenv("CANON_WEB_EXTRACT_MODEL", "claude-haiku-4-5")
    monkeypatch.setenv("CANON_WEB_EXTRACT_EFFORT", "low")
    assert runner.web_model() == "claude-haiku-4-5"
    assert runner.web_effort() == "low"


if __name__ == "__main__":
    import pytest

    raise SystemExit(pytest.main(["-q", __file__]))

"""Background pipeline runner (P3-FRONTDOOR) — the whole ingest as one call.

`run_pipeline` drives the existing stages end to end for one world —
extraction (canon/extract.py) -> periodic partial check cycles -> final
resolution (canon/resolve.py) -> store (canon/store.py) -> checks
(canon/check.py) -> Reader's Report notes (canon/report.py) — emitting a
stream of plain-dict events through an injected `emit` callback. That event
stream IS the "ingest theater" (docs/readers-report.md): scene-by-scene
narration, running fact counts, and the first finding surfacing while
extraction is still going.

Boundaries, deliberate:
  - NO HTTP imports. This module knows nothing about FastAPI, runs, or run
    rows; ui/jobs.py owns persistence (its emit closure appends ledger events
    and mirrors counters onto the pipeline_runs row).
  - Consumes the pipeline stages, never edits them.
  - Web-path model defaults (claude-sonnet-5, medium effort) live HERE via
    CANON_WEB_EXTRACT_MODEL / CANON_WEB_EXTRACT_EFFORT — the CLI keeps its own
    defaults in canon/extract.py untouched.

Event kinds emitted (data is always a plain JSON-safe dict):
  phase      {phase, ...}                      phase transitions
  scene      {index,total,slug,facts_scene,facts_total,open_questions}
  entity     {name,alias,kind}                 alias attached between cycles
  finding    {check,severity,explanation,scene_id,assertion_a,assertion_b,first}
  stat       {facts_total,entities,scenes}     the closing stat line
  done       {report_url}
  error      {message,scenes_done,resumable:true}     failed — resumable
  cost_abort {cost_usd,cap_usd,scenes_done}           cap breached — NOT resumable

Failure contract: run_pipeline never raises — it returns a summary dict
{"status": "done"|"failed"|"aborted", ...}. On failure the cumulative
candidate extractions ride back in "candidates"; they are the resume state
(pass them as resume_from and build_synopsis rebuilds the rolling context).
A cost abort is final: the money is spent, resuming would spend more.
"""

from __future__ import annotations

import dataclasses
import os
from dataclasses import asdict
from decimal import Decimal
from pathlib import Path

from ops import alerts, metering

from .check import persist_findings, run_checks
from .extract import (SceneExtraction, build_synopsis, extract_scene,
                      run_extraction, scene_contexts, to_json)
from .report import run_report_notes
from .resolve import _norm, resolve_candidates, state_to_dict
from .store import store_state

DEFAULT_WEB_MODEL = "claude-sonnet-5"
DEFAULT_WEB_EFFORT = "medium"
DEFAULT_COST_CAP_USD = 3.00
CHECK_FIRST_AT = 3          # first partial check cycle after this many scenes
DEFAULT_CHECK_EVERY = 6     # then every N scenes (CANON_CHECK_EVERY_SCENES)

_CHECKS_SQL_PATH = Path(__file__).resolve().parent.parent / "db" / "checks.sql"


def web_model() -> str:
    return os.environ.get("CANON_WEB_EXTRACT_MODEL") or DEFAULT_WEB_MODEL


def web_effort() -> str:
    return os.environ.get("CANON_WEB_EXTRACT_EFFORT") or DEFAULT_WEB_EFFORT


def cost_cap_usd_default() -> float:
    try:
        return float(os.environ.get("CANON_RUN_COGS_CAP_USD", DEFAULT_COST_CAP_USD))
    except ValueError:
        return DEFAULT_COST_CAP_USD


def check_cadence(check_every: int | None = None) -> int:
    if check_every:
        return max(1, int(check_every))
    raw = os.environ.get("CANON_CHECK_EVERY_SCENES")
    try:
        return max(1, int(raw)) if raw else DEFAULT_CHECK_EVERY
    except ValueError:
        return DEFAULT_CHECK_EVERY


def _default_checks_sql() -> str:
    return _CHECKS_SQL_PATH.read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# Cost guard — wraps the client at the same seam extract/llm.py meters.
# ---------------------------------------------------------------------------

class CostCapExceeded(RuntimeError):
    def __init__(self, cost: Decimal, cap: Decimal):
        super().__init__(
            f"run cost ${cost:.4f} exceeded the ${cap:.2f} cap; run aborted")
        self.cost = cost
        self.cap = cap


class _GuardedMessages:
    def __init__(self, guard: "CostGuard", messages):
        self._guard = guard
        self._messages = messages

    def create(self, **kwargs):
        response = self._messages.create(**kwargs)
        # The response is already paid for — account for it first, THEN abort
        # if the running total crossed the cap. Aborting mid-run beats letting
        # a pathological script spend without bound.
        self._guard.record(response, kwargs.get("model"))
        return response


class CostGuard:
    """Client wrapper that accumulates ops.metering cost per response and
    raises CostCapExceeded once the running total passes the cap. Duck-types
    the one client surface the pipeline uses: `client.messages.create`."""

    def __init__(self, client, cap_usd: float):
        self.cap = Decimal(str(cap_usd))
        self.cost = Decimal("0")
        self.messages = _GuardedMessages(self, client.messages)

    def record(self, response, request_model: str | None = None) -> None:
        usage = getattr(response, "usage", None)
        tokens_in = getattr(usage, "input_tokens", 0) or 0
        tokens_out = getattr(usage, "output_tokens", 0) or 0
        model = getattr(response, "model", None) or request_model
        cost, _priced = metering.cost_usd(model, tokens_in, tokens_out)
        self.cost += cost
        if self.cost > self.cap:
            raise CostCapExceeded(self.cost, self.cap)


# ---------------------------------------------------------------------------
# Pure diff helpers (exported for tests)
# ---------------------------------------------------------------------------

def finding_key(f: dict) -> tuple:
    return (f.get("check"), f.get("explanation"))


def diff_findings(emitted: set, findings: list[dict]) -> tuple[list[dict], set]:
    """Findings not yet emitted, keyed (check, explanation); sealed ones never
    surface in the theater. Pure: returns (new_findings, updated_key_set)."""
    seen = set(emitted)
    new: list[dict] = []
    for f in findings:
        if f.get("sealed"):
            continue
        key = finding_key(f)
        if key not in seen:
            seen.add(key)
            new.append(f)
    return new, seen


def alias_triples(state) -> set:
    """{(canonical_name, alias, kind)} for every alias that is not just the
    entity's own name — the 'Canon connected X to Y' narration material."""
    triples: set = set()
    for e in state.registry.entities:
        for alias, _alias_kind in e.aliases:
            if _norm(alias) != _norm(e.name):
                triples.add((e.name, alias, e.kind))
    return triples


def diff_aliases(seen: set, state) -> tuple[list[dict], set]:
    """New alias links since the previous cycle. Pure: (events, updated_set)."""
    current = alias_triples(state)
    events = [{"name": n, "alias": a, "kind": k}
              for (n, a, k) in sorted(current - set(seen))]
    return events, set(seen) | current


# ---------------------------------------------------------------------------
# The run
# ---------------------------------------------------------------------------

def _as_extraction(d: dict) -> SceneExtraction:
    known = {f.name for f in dataclasses.fields(SceneExtraction)}
    return SceneExtraction(**{k: v for k, v in d.items() if k in known})


def _should_check(index: int, total: int, every: int) -> bool:
    """Partial check cycles land at scene 3, then every `every` scenes — but
    never on the last scene (the final full pass follows immediately)."""
    if index >= total or index < CHECK_FIRST_AT:
        return False
    return index == CHECK_FIRST_AT or (index - CHECK_FIRST_AT) % every == 0


def run_pipeline(conn_factory, world_name: str, works, *, client_factory, emit,
                 model: str | None = None, effort: str | None = None,
                 check_every: int | None = None, cost_cap_usd: float | None = None,
                 resume_from: list[dict] | None = None,
                 checks_sql: str | None = None) -> dict:
    """Run the whole pipeline for one already-ingested world. See module doc."""
    model = model or web_model()
    effort = effort or web_effort()
    every = check_cadence(check_every)
    cap = cost_cap_usd if cost_cap_usd is not None else cost_cap_usd_default()
    checks = checks_sql if checks_sql is not None else _default_checks_sql()

    results: list[SceneExtraction] = [_as_extraction(d) for d in (resume_from or [])]
    facts_total = sum(len(r.assertions) for r in results)
    emitted_findings: set = set()
    seen_aliases: set = set()
    any_finding = False
    world_id: int | None = None
    guard: CostGuard | None = None
    conn = None

    def _candidates() -> list[dict]:
        return [asdict(r) for r in results]

    try:
        guard = CostGuard(client_factory(), cap)
        conn = conn_factory()
        ctxs = scene_contexts(works)
        total = len(ctxs)
        emit("phase", {"phase": "extracting", "scenes_total": total,
                       "scenes_done": len(results)})

        def _emit_new_findings(findings: list[dict]) -> None:
            nonlocal emitted_findings, any_finding
            new, emitted_findings = diff_findings(emitted_findings, findings)
            for f in new:
                emit("finding", {
                    "check": f.get("check"), "severity": f.get("severity"),
                    "explanation": f.get("explanation"),
                    "scene_id": f.get("scene_id"),
                    "assertion_a": f.get("assertion_a"),
                    "assertion_b": f.get("assertion_b"),
                    "first": not any_finding,
                })
                any_finding = True

        def _cycle(state) -> None:
            """Store the (partial or final) graph, diff aliases + findings."""
            nonlocal world_id, seen_aliases
            summary = store_state(conn, world_name, state_to_dict(state), reset=True)
            world_id = summary["world_id"]
            alias_events, seen_aliases = diff_aliases(seen_aliases, state)
            for ev in alias_events:
                emit("entity", ev)
            findings, _errors = run_checks(conn, world_id, checks)
            _emit_new_findings(findings)

        def _after_scene(ex: SceneExtraction) -> None:
            nonlocal facts_total
            results.append(ex)
            facts_total += len(ex.assertions)
            index = len(results)
            emit("scene", {
                "index": index, "total": total, "slug": ex.slug,
                "facts_scene": len(ex.assertions), "facts_total": facts_total,
                "open_questions": list(ex.open_questions or []),
            })
            if _should_check(index, total, every):
                # Deterministic partial pass: no LLM in resolution here — the
                # theater's mid-run findings must not add extraction cost.
                _cycle(resolve_candidates(to_json(world_name, model, results),
                                          client=None))

        if results:
            # Resume: skip the scenes already extracted; the prior candidates
            # rebuild the rolling synopsis exactly as a fresh run would have it.
            for ctx in ctxs[len(results):]:
                _after_scene(extract_scene(guard, ctx, build_synopsis(results),
                                           model, effort))
        else:
            run_extraction(guard, works, model=model, effort=effort,
                           on_result=_after_scene)

        # Final pass: full resolution WITH the LLM disambiguation tier.
        emit("phase", {"phase": "resolving"})
        state = resolve_candidates(to_json(world_name, model, results),
                                   client=guard, model=model, effort=effort)
        emit("phase", {"phase": "checking"})
        summary = store_state(conn, world_name, state_to_dict(state), reset=True)
        world_id = summary["world_id"]
        alias_events, seen_aliases = diff_aliases(seen_aliases, state)
        for ev in alias_events:
            emit("entity", ev)
        findings, _errors = run_checks(conn, world_id, checks)
        persist_findings(conn, world_id, findings)
        _emit_new_findings(findings)

        emit("phase", {"phase": "reporting"})
        run_report_notes(conn, world_id, client=guard, model=model, effort=effort)

        report_url = f"/worlds/{world_id}/report"
        emit("stat", {"facts_total": summary["assertions"],
                      "entities": summary["entities"], "scenes": total})
        emit("phase", {"phase": "done"})
        emit("done", {"report_url": report_url})
        return {"status": "done", "world_id": world_id, "report_url": report_url,
                "scenes_done": len(results), "facts_total": facts_total,
                "cost_usd": float(guard.cost)}

    except CostCapExceeded as exc:
        # Final, not resumable: the cap is a spend decision, not a hiccup.
        emit("cost_abort", {"cost_usd": float(exc.cost), "cap_usd": float(exc.cap),
                            "scenes_done": len(results)})
        alerts.alert("run_cogs_exceeded", {
            "world": world_name, "cost_usd": float(exc.cost),
            "cap_usd": float(exc.cap), "scenes_done": len(results)})
        return {"status": "aborted", "error": str(exc), "resumable": False,
                "scenes_done": len(results), "facts_total": facts_total,
                "cost_usd": float(exc.cost), "candidates": _candidates()}

    except Exception as exc:  # any phase — the run row records it, resumable
        emit("error", {"message": str(exc), "scenes_done": len(results),
                       "resumable": True})
        return {"status": "failed", "error": str(exc), "resumable": True,
                "scenes_done": len(results), "facts_total": facts_total,
                "cost_usd": float(guard.cost) if guard else 0.0,
                "candidates": _candidates()}

    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass

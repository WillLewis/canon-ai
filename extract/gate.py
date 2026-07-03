"""Confidence gate and assertion confirmation queue."""

from __future__ import annotations

from typing import Any

import json

from .schema import AUTO_ACCEPT_CONFIDENCE, PREDICATES
from .resolution import ResolutionState


def _confidence(value: Any) -> float:
    try:
        return max(0.0, min(1.0, float(value)))
    except (TypeError, ValueError):
        return 0.0


def assertion_summary(assertion: dict[str, Any]) -> str:
    obj = assertion.get("object_entity") or assertion.get("object_value") or ""
    neg = "" if assertion.get("polarity", True) else "NOT "
    return f"{neg}{assertion.get('subject')} {assertion.get('predicate')} {obj}".strip()


def gate_state(state: ResolutionState, threshold: float = AUTO_ACCEPT_CONFIDENCE) -> ResolutionState:
    """Apply Stage 4 confidence gate in-place and return state.

    `confidence >= threshold` becomes `draft`. Lower confidence assertions are
    queued for human accept/edit/reject. Human-confirmed assertions later become
    `canon`.
    """

    queue: list[dict[str, Any]] = []
    for idx, assertion in enumerate(state.assertions):
        if assertion.get("status") in {"canon", "rejected"}:
            continue
        conf = _confidence(assertion.get("confidence"))
        assertion["confidence"] = conf
        if conf >= threshold:
            assertion["status"] = "draft"
            assertion["confirmed_by_human"] = bool(assertion.get("confirmed_by_human", False))
            continue
        assertion["status"] = "needs_confirmation"
        queue.append(
            {
                "assertion_index": idx,
                "reason": "low_confidence",
                "confidence": conf,
                "scene": assertion.get("scene"),
                "scene_id": assertion.get("scene_id"),
                "subject": assertion.get("subject"),
                "predicate": assertion.get("predicate"),
                "object_entity": assertion.get("object_entity"),
                "object_value": assertion.get("object_value"),
                "supporting_quote": assertion.get("supporting_quote"),
                "summary": assertion_summary(assertion),
            }
        )
    state.assertion_queue = queue
    return state


def apply_assertion_decision(state: ResolutionState, item: dict[str, Any], decision: str) -> bool:
    """Apply one human assertion decision.

    Grammar: `accept`, `reject`, `edit {"field": "value"}`, or `skip`.
    Accepted or edited assertions become `canon` with `confirmed_by_human`.
    """

    choice = (decision or "").strip()
    if not choice or choice.lower() in {"skip", "s"}:
        return False

    idx = int(item["assertion_index"])
    if not (0 <= idx < len(state.assertions)):
        return True
    assertion = state.assertions[idx]
    low = choice.lower()

    if low in {"accept", "a"}:
        assertion["status"] = "canon"
        assertion["confirmed_by_human"] = True
        return True

    if low in {"reject", "r"}:
        assertion["status"] = "rejected"
        assertion["confirmed_by_human"] = True
        assertion["rejection_reason"] = "human rejected from confidence queue"
        return True

    if low.startswith("edit ") or low.startswith("e "):
        payload = choice.split(" ", 1)[1].strip()
        try:
            patch = json.loads(payload)
        except json.JSONDecodeError:
            return False
        if patch.get("predicate") and patch["predicate"] not in PREDICATES:
            return False
        for key, value in patch.items():
            if key in {
                "subject",
                "predicate",
                "object_entity",
                "object_value",
                "polarity",
                "starts_here",
                "ends_here",
                "supporting_quote",
                "confidence",
                "notes",
            }:
                assertion[key] = value
        assertion["status"] = "canon"
        assertion["confirmed_by_human"] = True
        assertion["human_edit"] = True
        return True

    return False


def walk_assertion_queue(state: ResolutionState, prompt_fn) -> int:
    remaining: list[dict[str, Any]] = []
    resolved = 0
    for item in state.assertion_queue:
        if apply_assertion_decision(state, item, prompt_fn(item, state)):
            resolved += 1
        else:
            remaining.append(item)
    state.assertion_queue = remaining
    return resolved


def to_eval_assertions(state: ResolutionState) -> dict[str, list[dict[str, Any]]]:
    """Flat eval contract, excluding rejected assertions."""

    return {
        "assertions": [
            {
                "subject": assertion.get("subject"),
                "predicate": assertion.get("predicate"),
                "object_value": assertion.get("object_value"),
                "object_entity": assertion.get("object_entity"),
                "scene": assertion.get("scene"),
                "confidence": assertion.get("confidence"),
                "status": assertion.get("status"),
            }
            for assertion in state.assertions
            if assertion.get("status") != "rejected"
        ]
    }

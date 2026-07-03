"""Per-scene LLM extraction and quote gates."""

from __future__ import annotations

from dataclasses import asdict
from typing import Any, Callable

import json
import re

from .llm import DEFAULT_EFFORT, DEFAULT_MODEL, MAX_EXTRACTION_TOKENS, first_text, structured_request
from .models import SceneExtraction, SceneRecord
from .prompts import build_synopsis, build_user_prompt, extraction_system_prompt
from .schema import INTRANSITIVE, PREDICATES, candidate_schema


def _normalize(text: str) -> str:
    text = (
        (text or "")
        .replace("'", "'")
        .replace("\u2018", "'")
        .replace("\u2019", "'")
        .replace("\u201c", '"')
        .replace("\u201d", '"')
        .replace("\u2013", "-")
        .replace("\u2014", "-")
    )
    return " ".join(text.split()).casefold()


def quote_in_text(quote: str, text: str) -> bool:
    return bool(quote and _normalize(quote) in _normalize(text))


def clamp_confidence(value: Any) -> float:
    try:
        conf = float(value)
    except (TypeError, ValueError):
        conf = 0.0
    return max(0.0, min(1.0, conf))


def post_process(scene: SceneRecord, payload: dict[str, Any], verify_quotes: bool = True) -> tuple[list[dict], dict]:
    """Apply the non-negotiable extraction quality gates."""

    dropped = {"no_quote": 0, "bad_predicate": 0, "no_object": 0, "quote_unverified": 0}
    kept: list[dict[str, Any]] = []
    for assertion in payload.get("assertions") or []:
        quote = (assertion.get("supporting_quote") or "").strip()
        predicate = assertion.get("predicate")
        has_object = bool(
            assertion.get("object_entity")
            or assertion.get("object_value")
            or assertion.get("object_fact_ref")
        )
        if not quote:
            dropped["no_quote"] += 1
            continue
        if predicate not in PREDICATES:
            dropped["bad_predicate"] += 1
            continue
        if not has_object and predicate not in INTRANSITIVE:
            dropped["no_object"] += 1
            continue
        if verify_quotes and not quote_in_text(quote, scene.raw_text):
            dropped["quote_unverified"] += 1
            continue

        row = dict(assertion)
        row["supporting_quote"] = quote
        row["confidence"] = clamp_confidence(row.get("confidence"))
        row["polarity"] = bool(row.get("polarity", True))
        row["starts_here"] = bool(row.get("starts_here", True))
        row["ends_here"] = bool(row.get("ends_here", False))
        row["scene_id"] = scene.scene_id
        row["scene"] = scene.label
        row["story_position"] = scene.story_position
        kept.append(row)
    return kept, dropped


def extract_scene(
    client,
    scene: SceneRecord,
    prior: list[SceneExtraction],
    *,
    model: str = DEFAULT_MODEL,
    effort: str = DEFAULT_EFFORT,
    thinking: bool = True,
    verify_quotes: bool = True,
) -> SceneExtraction:
    request = structured_request(
        model=model,
        system=extraction_system_prompt(),
        user=build_user_prompt(scene, build_synopsis(prior)),
        schema=candidate_schema(),
        max_tokens=MAX_EXTRACTION_TOKENS,
        effort=effort,
        thinking=thinking,
    )
    response = client.messages.create(**request)
    if getattr(response, "stop_reason", None) == "refusal":
        raise RuntimeError(f"model refused scene {scene.scene_id} ({scene.slug})")
    text = first_text(response)
    if text is None:
        raise RuntimeError(f"no text block in response for scene {scene.scene_id}")
    payload = json.loads(text)
    assertions, dropped = post_process(scene, payload, verify_quotes=verify_quotes)
    return SceneExtraction(
        scene_id=scene.scene_id,
        work_title=scene.work_title,
        scene_index=scene.scene_index,
        slug=scene.slug,
        story_position=scene.story_position,
        is_flashback=scene.is_flashback,
        assertions=assertions,
        scene_presence=payload.get("scene_presence") or [],
        deaths=payload.get("deaths") or [],
        destructions=payload.get("destructions") or [],
        open_questions=payload.get("open_questions") or [],
        dropped=dropped,
    )


def run_extraction(
    client,
    scenes: list[SceneRecord],
    *,
    model: str = DEFAULT_MODEL,
    effort: str = DEFAULT_EFFORT,
    thinking: bool = True,
    verify_quotes: bool = True,
    limit: int | None = None,
    on_result: Callable[[SceneExtraction], None] | None = None,
) -> list[SceneExtraction]:
    if limit is not None:
        scenes = scenes[:limit]
    results: list[SceneExtraction] = []
    for scene in scenes:
        result = extract_scene(
            client,
            scene,
            results,
            model=model,
            effort=effort,
            thinking=thinking,
            verify_quotes=verify_quotes,
        )
        results.append(result)
        if on_result:
            on_result(result)
    return results


def to_candidate_json(world: str, model: str, scenes: list[SceneExtraction]) -> dict[str, Any]:
    return {"world": world, "model": model, "scenes": [asdict(scene) for scene in scenes]}


def render_dry_run(
    scenes: list[SceneRecord],
    *,
    model: str = DEFAULT_MODEL,
    effort: str = DEFAULT_EFFORT,
    limit: int | None = None,
) -> str:
    shown = scenes[: (limit or 1)]
    lines = [
        f"model: {model}   effort: {effort}   thinking: adaptive",
        f"scenes to extract: {len(scenes)}",
        "",
        "=== SYSTEM PROMPT ===",
        extraction_system_prompt(),
        "",
    ]
    prior: list[SceneExtraction] = []
    for scene in shown:
        lines.append(f"=== USER PROMPT - {scene.scene_id} | {scene.slug} ===")
        lines.append(build_user_prompt(scene, build_synopsis(prior)))
        lines.append("")
    if len(scenes) > len(shown):
        lines.append(
            f"... {len(scenes) - len(shown)} more scene(s); each receives a rolling synopsis "
            "built from prior scene extractions."
        )
    return "\n".join(lines)

"""Prompt assembly from versioned prompt files."""

from __future__ import annotations

from pathlib import Path

from .models import SceneExtraction, SceneRecord
from .schema import PREDICATES

PROMPT_DIR = Path(__file__).resolve().parent / "prompts"


def read_prompt(name: str) -> str:
    return (PROMPT_DIR / name).read_text(encoding="utf-8")


def extraction_system_prompt() -> str:
    return read_prompt("extraction_system.md").format(predicate_list=", ".join(PREDICATES))


def resolution_system_prompt() -> str:
    return read_prompt("entity_resolution_system.md")


def build_synopsis(prior: list[SceneExtraction], facts_per_scene: int = 12) -> str:
    """Compact rolling context from prior extracted facts.

    This is deterministic indexing text, not story generation.
    """

    lines: list[str] = []
    for ex in prior:
        facts: list[str] = []
        for a in ex.assertions:
            obj = a.get("object_entity") or a.get("object_value") or a.get("object_fact_ref") or ""
            neg = "" if a.get("polarity", True) else "NOT "
            facts.append(f"{neg}{a.get('subject', '?')} {a.get('predicate', '?')} {obj}".strip())
        line = f"[{ex.scene_id} | {ex.slug}] present: {', '.join(ex.scene_presence)}"
        if facts:
            line += " | " + "; ".join(facts[:facts_per_scene])
        lines.append(line)
    return "\n".join(lines)


def build_user_prompt(scene: SceneRecord, synopsis: str) -> str:
    prior = (
        "PRIOR SCENES (context only - do NOT re-extract these):\n" + synopsis
        if synopsis
        else "PRIOR SCENES: none - this is the first scene."
    )
    flashback = " [FLASHBACK]" if scene.is_flashback else ""
    return (
        f"{prior}\n\n"
        "CURRENT SCENE - extract assertions for THIS scene only.\n"
        f"scene_id: {scene.scene_id}\n"
        f"work: {scene.work_title}\n"
        f"scene: {scene.slug}{flashback}\n"
        f"story_position: {scene.story_position}\n"
        "---\n"
        f"{scene.raw_text}\n"
        "---\n\n"
        "Return the structured candidate assertions for this scene."
    )

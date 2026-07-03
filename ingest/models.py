from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional


@dataclass
class Scene:
    scene_index: int
    slug: str
    is_flashback: bool
    raw_text: str


@dataclass
class ParsedWork:
    source_file: str
    title: str
    show_title: Optional[str]
    episode: Optional[int]
    sort_order: Optional[int]
    title_page: dict[str, str] = field(default_factory=dict)
    scenes: list[Scene] = field(default_factory=list)
    stripped_planted: int = 0


@dataclass(frozen=True)
class SceneRecord:
    scene_id: str
    slug: str
    story_position: int
    is_flashback: bool
    raw_text: str

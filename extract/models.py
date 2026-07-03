"""JSON contracts and dataclasses for P0 extraction."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

import json


@dataclass
class SceneRecord:
    """Scene JSON consumed by this workstream.

    Required JSON fields are `slug`, `story_position`, `is_flashback`, and
    `raw_text`. `scene_id`, `work_title`, and `scene_index` are strongly
    recommended because they make citations stable before the DB exists.
    """

    scene_id: str
    work_title: str
    scene_index: int
    slug: str
    story_position: int
    is_flashback: bool
    raw_text: str
    source_file: str | None = None

    @property
    def label(self) -> str:
        if self.scene_id:
            return self.scene_id
        head = (self.work_title or "?").split()[0]
        return f"{head}/sc{self.scene_index}"


@dataclass
class SceneExtraction:
    scene_id: str
    work_title: str
    scene_index: int
    slug: str
    story_position: int
    is_flashback: bool
    assertions: list[dict[str, Any]] = field(default_factory=list)
    scene_presence: list[str] = field(default_factory=list)
    deaths: list[str] = field(default_factory=list)
    destructions: list[str] = field(default_factory=list)
    open_questions: list[str] = field(default_factory=list)
    dropped: dict[str, int] = field(default_factory=dict)


def _coerce_int(value: Any, fallback: int, field_name: str) -> int:
    if value is None or value == "":
        return fallback
    try:
        return int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{field_name} must be an integer, got {value!r}") from exc


def scene_from_dict(row: dict[str, Any], index: int) -> SceneRecord:
    slug = str(row.get("slug") or row.get("scene_slug") or "").strip()
    raw_text = str(row.get("raw_text") or row.get("text") or "").strip()
    if not slug:
        raise ValueError(f"scene {index}: missing slug")
    if not raw_text:
        raise ValueError(f"scene {index}: missing raw_text")

    story_position = _coerce_int(row.get("story_position"), index, "story_position")
    scene_index = _coerce_int(
        row.get("scene_index") or row.get("scene_number"),
        index,
        "scene_index",
    )
    work_title = str(row.get("work_title") or row.get("work") or row.get("episode") or "").strip()
    scene_id = str(row.get("scene_id") or row.get("id") or "").strip()
    if not scene_id:
        head = (work_title or "?").split()[0]
        scene_id = f"{head}/sc{scene_index}"
    return SceneRecord(
        scene_id=scene_id,
        work_title=work_title,
        scene_index=scene_index,
        slug=slug,
        story_position=story_position,
        is_flashback=bool(row.get("is_flashback", False)),
        raw_text=raw_text,
        source_file=row.get("source_file"),
    )


def load_scenes(path: str | Path) -> tuple[str, list[SceneRecord]]:
    """Load scene records from either `{world, scenes}` or a bare scene list."""

    data = json.loads(Path(path).read_text(encoding="utf-8"))
    if isinstance(data, dict):
        world = str(data.get("world") or "")
        rows = data.get("scenes") or []
    elif isinstance(data, list):
        world = ""
        rows = data
    else:
        raise ValueError("scene input must be a JSON object with scenes or a list")
    if not isinstance(rows, list):
        raise ValueError("scenes must be a list")
    scenes = [scene_from_dict(row, i) for i, row in enumerate(rows, start=1)]
    scenes.sort(key=lambda s: (s.story_position, s.scene_id))
    return world, scenes


def extraction_to_dict(ex: SceneExtraction) -> dict[str, Any]:
    return asdict(ex)

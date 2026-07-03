"""Entity resolution for candidate assertions.

Stage contract: exact alias match -> fuzzy match -> LLM disambiguation with
dossiers. Unresolved role references become provisional entities and queue
items. Two existing entities are never merged automatically.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import json
import re

from .llm import DEFAULT_EFFORT, DEFAULT_MODEL, MAX_RESOLUTION_TOKENS, first_text, structured_request
from .prompts import resolution_system_prompt
from .schema import ALIAS_KINDS, AUTO_ACCEPT_CONFIDENCE, ENTITY_KINDS, resolution_schema

ROLE_WORDS = frozenset(
    {
        "uncle",
        "aunt",
        "brother",
        "sister",
        "father",
        "mother",
        "mom",
        "dad",
        "son",
        "daughter",
        "cousin",
        "nephew",
        "niece",
        "husband",
        "wife",
        "widow",
        "deputy",
        "sheriff",
        "captain",
        "cook",
        "boss",
        "stranger",
        "man",
        "woman",
        "boy",
        "girl",
        "kid",
        "the man",
        "the woman",
    }
)
PRONOUNS = frozenset({"he", "she", "they", "him", "her", "them", "it", "his", "hers", "its", "their"})
INITIALS_RE = re.compile(r"^([A-Za-z]\.){2,}$")

CHARACTER_SUBJECT_PREDS = frozenset(
    {
        "knows",
        "believes",
        "cannot",
        "dies",
        "occupation",
        "married_to",
        "parent_of",
        "sibling_of",
        "romantic_with",
        "allied_with",
        "enemy_of",
        "member_of",
        "possesses",
        "promised",
        "goal",
        "secret_of",
        "trait",
        "alive",
        "present_in_scene",
    }
)
OBJECT_KIND_BY_PRED = {
    "located_at": "location",
    "possesses": "object",
    "member_of": "faction",
    "married_to": "character",
    "parent_of": "character",
    "sibling_of": "character",
    "romantic_with": "character",
    "allied_with": "character",
    "enemy_of": "character",
    "created": "object",
    "secret_of": "character",
}
KIND_PRIORITY = {k: i for i, k in enumerate(("character", "location", "object", "faction", "event", "rule", "other"))}


def norm(text: str | None) -> str:
    return re.sub(r"[^a-z0-9 ]", "", (text or "").lower()).strip()


def tokens(text: str | None) -> set[str]:
    return set(norm(text).split())


def display_name(text: str) -> str:
    text = (text or "").strip()
    if text and text.isupper() and any(c.isalpha() for c in text):
        return text.title()
    return text


def is_initials(text: str) -> bool:
    return bool(INITIALS_RE.match((text or "").strip()))


def strip_articles(text: str) -> str:
    parts = (text or "").strip().split()
    while parts and parts[0].lower() in ("the", "a", "an"):
        parts = parts[1:]
    return " ".join(parts)


def is_role_reference(surface: str) -> bool:
    if is_initials(surface):
        return True
    n = norm(strip_articles(surface))
    return (not n) or n in ROLE_WORDS or n in PRONOUNS


def occ_kind(role: str, predicate: str | None) -> str:
    if role in ("presence", "death"):
        return "character"
    if role == "destruction":
        return "location"
    if role == "subject":
        if predicate in ("located_at", "created"):
            return "object"
        if predicate == "destroyed":
            return "location"
        if predicate in CHARACTER_SUBJECT_PREDS:
            return "character"
        return "other"
    if role == "object":
        return OBJECT_KIND_BY_PRED.get(predicate, "other")
    return "other"


def scene_label(scene: dict[str, Any]) -> str:
    if scene.get("scene_id"):
        return scene["scene_id"]
    head = (scene.get("work_title") or "?").split()[0]
    return f"{head}/sc{scene.get('scene_index', 0)}"


@dataclass
class Mention:
    surface: str
    role: str
    predicate: str | None
    story_position: int
    scene: str
    slug: str
    quote: str | None


@dataclass
class SurfaceInfo:
    display: str
    norm: str
    kind_hint: str
    first_position: int
    is_role: bool
    occurrences: list[Mention] = field(default_factory=list)


@dataclass(eq=False)
class Entity:
    local_id: int
    name: str
    kind: str
    dossier: str | None
    provisional: bool
    seeded: bool = False
    aliases: list[list[str]] = field(default_factory=list)

    def alias_norms(self) -> set[str]:
        return {norm(self.name)} | {norm(alias) for alias, _kind in self.aliases}

    def token_sets(self) -> list[set[str]]:
        return [tokens(self.name)] + [tokens(alias) for alias, _kind in self.aliases]


class Registry:
    def __init__(self) -> None:
        self.entities: list[Entity] = []
        self._next = 1

    def add(
        self,
        name: str,
        kind: str,
        dossier: str | None,
        provisional: bool,
        *,
        alias_kind: str = "name_variant",
        seeded: bool = False,
    ) -> Entity:
        entity = Entity(
            local_id=self._next,
            name=display_name(name),
            kind=kind if kind in ENTITY_KINDS else "other",
            dossier=dossier,
            provisional=provisional,
            seeded=seeded,
            aliases=[[name, alias_kind if alias_kind in ALIAS_KINDS else "name_variant"]],
        )
        self._next += 1
        self.entities.append(entity)
        return entity

    def add_alias(self, entity: Entity, surface: str, alias_kind: str = "name_variant", upgrade: bool = True) -> None:
        alias_kind = alias_kind if alias_kind in ALIAS_KINDS else "name_variant"
        if norm(surface) not in entity.alias_norms():
            entity.aliases.append([surface, alias_kind])
        if (
            upgrade
            and alias_kind == "name_variant"
            and (len(tokens(surface)), len(surface)) > (len(tokens(entity.name)), len(entity.name))
        ):
            entity.name = display_name(surface)

    def by_name(self, name: str | None) -> Entity | None:
        n = norm(name)
        if not n:
            return None
        return next((entity for entity in self.entities if n in entity.alias_norms()), None)

    def match(self, surface: str) -> tuple[str, Entity | list[Entity] | None]:
        n = norm(surface)
        ts = tokens(surface)
        if not n:
            return "none", None
        exact = [entity for entity in self.entities if n in entity.alias_norms()]
        if len(exact) == 1:
            return "exact", exact[0]
        if len(exact) > 1:
            return "ambiguous", exact

        candidates: list[Entity] = []
        for entity in self.entities:
            for known_tokens in entity.token_sets():
                if not ts or not known_tokens or ts == known_tokens:
                    continue
                if ts <= known_tokens or known_tokens <= ts:
                    candidates.append(entity)
                    break
                if len(ts & known_tokens) / len(ts | known_tokens) >= 0.5:
                    candidates.append(entity)
                    break
        candidates = list(dict.fromkeys(candidates))
        if len(candidates) == 1:
            return "fuzzy", candidates[0]
        if len(candidates) > 1:
            return "ambiguous", candidates
        return "none", None

    def merge(self, keep: Entity, drop: Entity, reason: str) -> None:
        """Human-only operation. Call only from confirm/merge CLI paths."""

        if keep is drop:
            return
        for alias, alias_kind in ([[drop.name, "name_variant"]] + drop.aliases):
            self.add_alias(keep, alias, alias_kind, upgrade=False)
        keep.provisional = keep.provisional and drop.provisional
        keep.seeded = keep.seeded or drop.seeded
        if not keep.dossier and drop.dossier:
            keep.dossier = drop.dossier
        self.entities = [entity for entity in self.entities if entity is not drop]


def registry_from_alias_table(alias_table: dict[str, Any] | None) -> Registry:
    reg = Registry()
    if not alias_table:
        return reg

    if isinstance(alias_table.get("entities"), list):
        for row in alias_table["entities"]:
            name = row.get("canonical_name") or row.get("name")
            if not name:
                continue
            entity = reg.add(
                name,
                row.get("kind") or "other",
                row.get("dossier"),
                bool(row.get("provisional", False)),
                seeded=True,
            )
            for alias_row in row.get("aliases") or []:
                if isinstance(alias_row, str):
                    reg.add_alias(entity, alias_row, "name_variant", upgrade=False)
                elif alias_row.get("alias"):
                    reg.add_alias(entity, alias_row["alias"], alias_row.get("kind") or "name_variant", upgrade=False)

    if isinstance(alias_table.get("aliases"), list):
        for row in alias_table["aliases"]:
            canonical = row.get("canonical_name") or row.get("entity") or row.get("name")
            alias = row.get("alias")
            if not canonical or not alias:
                continue
            entity = reg.by_name(canonical)
            if entity is None:
                entity = reg.add(canonical, row.get("entity_kind") or "other", None, False, seeded=True)
            reg.add_alias(entity, alias, row.get("kind") or "name_variant", upgrade=False)
    return reg


def scenes_from_candidates(candidates: dict[str, Any] | list[dict[str, Any]]) -> list[dict[str, Any]]:
    if isinstance(candidates, dict):
        return candidates.get("scenes") or []
    return list(candidates or [])


def collect_mentions(candidates: dict[str, Any] | list[dict[str, Any]]) -> list[Mention]:
    mentions: list[Mention] = []
    for scene in scenes_from_candidates(candidates):
        label = scene_label(scene)
        story_position = int(scene.get("story_position") or 0)
        slug = scene.get("slug") or ""

        def add(surface: Any, role: str, predicate: str | None = None, quote: str | None = None) -> None:
            if surface and str(surface).strip():
                mentions.append(Mention(str(surface).strip(), role, predicate, story_position, label, slug, quote))

        for assertion in scene.get("assertions") or []:
            add(assertion.get("subject"), "subject", assertion.get("predicate"), assertion.get("supporting_quote"))
            if assertion.get("object_entity"):
                add(assertion.get("object_entity"), "object", assertion.get("predicate"), assertion.get("supporting_quote"))
        for name in scene.get("scene_presence") or []:
            add(name, "presence")
        for name in scene.get("deaths") or []:
            add(name, "death")
        for name in scene.get("destructions") or []:
            add(name, "destruction")
    return mentions


def vote_kind(occurrences: list[Mention]) -> str:
    votes: dict[str, int] = {}
    for mention in occurrences:
        kind = occ_kind(mention.role, mention.predicate)
        votes[kind] = votes.get(kind, 0) + 1
    return min(votes, key=lambda k: (-votes[k], KIND_PRIORITY.get(k, 99))) if votes else "other"


def group_surfaces(mentions: list[Mention]) -> list[SurfaceInfo]:
    groups: dict[str, SurfaceInfo] = {}
    for mention in mentions:
        n = norm(mention.surface)
        if not n:
            continue
        info = groups.get(n)
        if info is None:
            info = SurfaceInfo(
                display=mention.surface,
                norm=n,
                kind_hint="other",
                first_position=mention.story_position,
                is_role=is_role_reference(mention.surface),
            )
            groups[n] = info
        info.occurrences.append(mention)
        info.first_position = min(info.first_position, mention.story_position)
        if (len(tokens(mention.surface)), len(mention.surface)) > (len(tokens(info.display)), len(info.display)):
            info.display = mention.surface

    for info in groups.values():
        info.display = display_name(info.display)
        info.kind_hint = vote_kind(info.occurrences)
    return sorted(groups.values(), key=lambda s: (s.first_position, s.is_role, s.display))


def build_disambiguation_prompt(surface: SurfaceInfo, registry: Registry) -> str:
    known = (
        "\n".join(
            f"- {entity.name} [{entity.kind}]"
            f"{' (provisional)' if entity.provisional else ''} - {entity.dossier or '(no dossier)'}"
            f" aliases: {', '.join(alias for alias, _kind in entity.aliases)}"
            for entity in registry.entities
        )
        if registry.entities
        else "(none yet)"
    )
    occurrences = "\n".join(
        f"- {mention.scene} ({mention.role}"
        + (f", predicate {mention.predicate}" if mention.predicate else "")
        + ")"
        + (f' quote: "{mention.quote}"' if mention.quote else "")
        for mention in surface.occurrences[:8]
    )
    return (
        f"KNOWN ENTITIES:\n{known}\n\n"
        f'REFERENCE TO RESOLVE: "{surface.display}"\n'
        f"kind hint: {surface.kind_hint}\n"
        f"occurrences:\n{occurrences}\n\n"
        "Decide whether this reference is one of the KNOWN ENTITIES, a NEW entity, or UNSURE.\n"
        "- existing: entity_name must be the exact canonical name from the known list.\n"
        "- new: provide canonical_name, kind, and a one-line dossier.\n"
        "- unsure: use when the evidence is not enough.\n"
        "Return confidence 0..1 and a brief reason."
    )


def disambiguate(
    client,
    surface: SurfaceInfo,
    registry: Registry,
    *,
    model: str = DEFAULT_MODEL,
    effort: str = DEFAULT_EFFORT,
    thinking: bool = True,
) -> dict[str, Any]:
    request = structured_request(
        model=model,
        system=resolution_system_prompt(),
        user=build_disambiguation_prompt(surface, registry),
        schema=resolution_schema(),
        max_tokens=MAX_RESOLUTION_TOKENS,
        effort=effort,
        thinking=thinking,
    )
    response = client.messages.create(**request)
    text = first_text(response)
    if text is None:
        raise RuntimeError(f"no text block resolving {surface.display!r}")
    return json.loads(text)


@dataclass
class EntityQueueItem:
    surface: str
    kind_hint: str
    reason: str
    candidates: list[str]
    entity_name: str
    occurrences: list[dict[str, Any]]


@dataclass
class ResolutionState:
    world: str
    registry: Registry
    assertions: list[dict[str, Any]]
    scene_presence: list[dict[str, Any]]
    entity_queue: list[EntityQueueItem]
    assertion_queue: list[dict[str, Any]] = field(default_factory=list)
    merges: list[dict[str, str]] = field(default_factory=list)


def _resolve_surface(
    registry: Registry,
    surface: SurfaceInfo,
    client,
    *,
    model: str,
    effort: str,
    thinking: bool,
    conf_auto: float,
) -> tuple[Entity, str | None, list[str]]:
    status, found = registry.match(surface.display)
    if status in ("exact", "fuzzy"):
        entity = found
        assert isinstance(entity, Entity)
        registry.add_alias(entity, surface.display, "role_reference" if surface.is_role else "name_variant")
        return entity, None, []

    candidates = [entity.name for entity in found] if status == "ambiguous" and isinstance(found, list) else []
    needs_llm = status == "ambiguous" or (status == "none" and surface.is_role)
    if needs_llm and client is not None:
        decision = disambiguate(client, surface, registry, model=model, effort=effort, thinking=thinking)
        confidence = float(decision.get("confidence") or 0.0)
        if decision.get("decision") == "existing" and confidence >= conf_auto:
            entity = registry.by_name(decision.get("entity_name"))
            if entity is not None:
                registry.add_alias(entity, surface.display, decision.get("alias_kind") or "role_reference")
                return entity, None, []
        if decision.get("decision") == "new" and confidence >= conf_auto:
            entity = registry.add(
                decision.get("canonical_name") or surface.display,
                decision.get("kind") or surface.kind_hint,
                decision.get("dossier"),
                provisional=False,
            )
            return entity, None, []

    if status == "ambiguous":
        entity = registry.add(surface.display, surface.kind_hint, None, provisional=True)
        return entity, "ambiguous", candidates

    if not surface.is_role:
        entity = registry.add(surface.display, surface.kind_hint, None, provisional=False)
        return entity, None, []

    entity = registry.add(surface.display, surface.kind_hint, None, provisional=True, alias_kind="role_reference")
    return entity, "role_reference", []


def resolve_candidates(
    candidates: dict[str, Any],
    *,
    world: str = "",
    alias_table: dict[str, Any] | None = None,
    client=None,
    model: str = DEFAULT_MODEL,
    effort: str = DEFAULT_EFFORT,
    thinking: bool = True,
    conf_auto: float = AUTO_ACCEPT_CONFIDENCE,
) -> ResolutionState:
    if isinstance(candidates, dict) and not world:
        world = candidates.get("world") or ""
    registry = registry_from_alias_table(alias_table)
    mentions = collect_mentions(candidates)
    surfaces = group_surfaces(mentions)

    surface_to_entity: dict[str, Entity] = {}
    entity_queue: list[EntityQueueItem] = []
    for surface in surfaces:
        entity, reason, cand_names = _resolve_surface(
            registry,
            surface,
            client,
            model=model,
            effort=effort,
            thinking=thinking,
            conf_auto=conf_auto,
        )
        surface_to_entity[surface.norm] = entity
        if reason:
            entity_queue.append(
                EntityQueueItem(
                    surface=surface.display,
                    kind_hint=surface.kind_hint,
                    reason=reason,
                    candidates=cand_names,
                    entity_name=entity.name,
                    occurrences=[
                        {
                            "scene": mention.scene,
                            "role": mention.role,
                            "predicate": mention.predicate,
                            "quote": mention.quote,
                        }
                        for mention in surface.occurrences[:8]
                    ],
                )
            )

    presence = _build_scene_presence(candidates, surface_to_entity)
    present_norms = {norm(name) for row in presence for name in row["entities"]}
    for entity in registry.entities:
        if entity.seeded:
            continue
        if entity.kind in ("character", "other") and norm(entity.name) not in present_norms:
            entity.provisional = True

    assertions = _build_assertions(candidates, surface_to_entity)
    return ResolutionState(world, registry, assertions, presence, entity_queue)


def _entity_for(surface_to_entity: dict[str, Entity], surface: Any) -> Entity | None:
    if not surface or not str(surface).strip():
        return None
    return surface_to_entity.get(norm(str(surface)))


def _build_assertions(candidates: dict[str, Any], surface_to_entity: dict[str, Entity]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    ordinal = 1
    for scene in scenes_from_candidates(candidates):
        label = scene_label(scene)
        for assertion in scene.get("assertions") or []:
            subject = _entity_for(surface_to_entity, assertion.get("subject"))
            obj = _entity_for(surface_to_entity, assertion.get("object_entity")) if assertion.get("object_entity") else None
            out.append(
                {
                    "assertion_id": assertion.get("assertion_id") or f"A{ordinal}",
                    "subject": subject.name if subject else assertion.get("subject"),
                    "predicate": assertion.get("predicate"),
                    "object_entity": obj.name if obj else None,
                    "object_value": assertion.get("object_value") or assertion.get("object_fact_ref"),
                    "polarity": assertion.get("polarity", True),
                    "starts_here": assertion.get("starts_here", True),
                    "ends_here": assertion.get("ends_here", False),
                    "supporting_quote": assertion.get("supporting_quote"),
                    "confidence": assertion.get("confidence"),
                    "notes": assertion.get("notes"),
                    "scene": label,
                    "scene_id": scene.get("scene_id") or label,
                    "story_position": scene.get("story_position"),
                    "subject_provisional": bool(subject.provisional) if subject else True,
                    "object_provisional": bool(obj.provisional) if obj else False,
                    "status": assertion.get("status") or "candidate",
                    "confirmed_by_human": bool(assertion.get("confirmed_by_human", False)),
                }
            )
            ordinal += 1
    return out


def _build_scene_presence(candidates: dict[str, Any], surface_to_entity: dict[str, Entity]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for scene in scenes_from_candidates(candidates):
        names: list[str] = []
        for name in scene.get("scene_presence") or []:
            entity = _entity_for(surface_to_entity, name)
            if entity and entity.name not in names:
                names.append(entity.name)
        out.append(
            {
                "scene": scene_label(scene),
                "scene_id": scene.get("scene_id") or scene_label(scene),
                "story_position": scene.get("story_position"),
                "entities": names,
            }
        )
    return out


def repoint_assertions(state: ResolutionState, old_name: str, new_name: str) -> None:
    old = norm(old_name)
    for assertion in state.assertions:
        if norm(assertion.get("subject")) == old:
            assertion["subject"] = new_name
        if assertion.get("object_entity") and norm(assertion["object_entity"]) == old:
            assertion["object_entity"] = new_name
    for row in state.scene_presence:
        row["entities"] = [new_name if norm(name) == old else name for name in row["entities"]]


def merge_entities(state: ResolutionState, keep_name: str, drop_name: str, reason: str) -> bool:
    keep = state.registry.by_name(keep_name)
    drop = state.registry.by_name(drop_name)
    if keep is None or drop is None or keep is drop:
        return False
    old = drop.name
    state.registry.merge(keep, drop, reason)
    repoint_assertions(state, old, keep.name)
    for assertion in state.assertions:
        if norm(assertion.get("subject")) == norm(keep.name):
            assertion["subject_provisional"] = keep.provisional
        if assertion.get("object_entity") and norm(assertion["object_entity"]) == norm(keep.name):
            assertion["object_provisional"] = keep.provisional
    state.entity_queue = [item for item in state.entity_queue if norm(item.entity_name) != norm(old)]
    state.merges.append({"keep": keep.name, "drop": old, "reason": reason})
    return True


def apply_entity_decision(state: ResolutionState, item: EntityQueueItem, decision: str) -> bool:
    """Apply one human entity decision.

    Grammar: `<n>`, `= Existing Name`, `new Name | kind`, `keep`, or `skip`.
    """

    choice = (decision or "").strip()
    if not choice or choice.lower() == "skip":
        return False
    if choice.lower() == "keep":
        return True
    if choice.isdigit():
        idx = int(choice) - 1
        if 0 <= idx < len(item.candidates):
            return merge_entities(state, item.candidates[idx], item.entity_name, "confirm: picked candidate")
        return False
    if choice.startswith("="):
        return merge_entities(state, choice[1:].strip(), item.entity_name, "confirm: assigned to existing")
    if choice.lower().startswith("new "):
        rest = choice[4:].strip()
        name, _sep, kind = rest.partition("|")
        name = name.strip()
        kind = kind.strip() or "other"
        entity = state.registry.by_name(item.entity_name)
        if entity is None or not name:
            return False
        old = entity.name
        entity.name = display_name(name)
        entity.kind = kind if kind in ENTITY_KINDS else entity.kind
        entity.provisional = False
        state.registry.add_alias(entity, old, "name_variant", upgrade=False)
        repoint_assertions(state, old, entity.name)
        return True
    return False


def walk_entity_queue(state: ResolutionState, prompt_fn) -> int:
    remaining: list[EntityQueueItem] = []
    resolved = 0
    for item in state.entity_queue:
        if apply_entity_decision(state, item, prompt_fn(item, state)):
            resolved += 1
        else:
            remaining.append(item)
    state.entity_queue = remaining
    return resolved


def state_to_dict(state: ResolutionState) -> dict[str, Any]:
    return {
        "world": state.world,
        "entities": [
            {
                "local_id": entity.local_id,
                "name": entity.name,
                "kind": entity.kind,
                "dossier": entity.dossier,
                "provisional": entity.provisional,
                "seeded": entity.seeded,
                "aliases": [{"alias": alias, "kind": kind} for alias, kind in entity.aliases],
            }
            for entity in state.registry.entities
        ],
        "assertions": state.assertions,
        "scene_presence": state.scene_presence,
        "entity_queue": [item.__dict__ for item in state.entity_queue],
        "assertion_queue": state.assertion_queue,
        "merges": state.merges,
    }


def state_from_dict(data: dict[str, Any]) -> ResolutionState:
    registry = Registry()
    for row in data.get("entities") or []:
        entity = Entity(
            local_id=int(row["local_id"]),
            name=row["name"],
            kind=row.get("kind") or "other",
            dossier=row.get("dossier"),
            provisional=bool(row.get("provisional", False)),
            seeded=bool(row.get("seeded", False)),
            aliases=[[alias["alias"], alias["kind"]] for alias in row.get("aliases") or []],
        )
        registry.entities.append(entity)
        registry._next = max(registry._next, entity.local_id + 1)
    return ResolutionState(
        world=data.get("world") or "",
        registry=registry,
        assertions=data.get("assertions") or [],
        scene_presence=data.get("scene_presence") or [],
        entity_queue=[EntityQueueItem(**item) for item in data.get("entity_queue") or data.get("queue") or []],
        assertion_queue=data.get("assertion_queue") or [],
        merges=data.get("merges") or [],
    )


def to_resolved_assertions(state: ResolutionState, *, include_rejected: bool = False) -> dict[str, Any]:
    assertions = [
        assertion
        for assertion in state.assertions
        if include_rejected or assertion.get("status") != "rejected"
    ]
    return {"world": state.world, "assertions": assertions, "scene_presence": state.scene_presence}


def render_summary(state: ResolutionState) -> str:
    provisional_count = sum(1 for entity in state.registry.entities if entity.provisional)
    lines = [
        f"world: {state.world}",
        f"entities: {len(state.registry.entities)} ({provisional_count} provisional)",
        f"resolved assertions: {len(state.assertions)}",
        f"entity queue: {len(state.entity_queue)}",
        f"assertion queue: {len(state.assertion_queue)}",
        "",
    ]
    for entity in state.registry.entities:
        tag = " [provisional]" if entity.provisional else ""
        aliases = ", ".join(alias for alias, _kind in entity.aliases if norm(alias) != norm(entity.name))
        lines.append(f"  {entity.name} [{entity.kind}]{tag}" + (f" (aka {aliases})" if aliases else ""))
    if state.entity_queue:
        lines.append("")
        lines.append("entity queue:")
        for item in state.entity_queue:
            candidates = f" candidates: {', '.join(item.candidates)}" if item.candidates else ""
            lines.append(f"  - {item.surface!r} [{item.kind_hint}] - {item.reason}{candidates}")
    if state.assertion_queue:
        lines.append("")
        lines.append("assertion queue:")
        for item in state.assertion_queue[:10]:
            lines.append(
                f"  - #{item['assertion_index']} {item['subject']} {item['predicate']} "
                f"{item.get('object_value') or item.get('object_entity') or ''} "
                f"conf={item.get('confidence')}"
            )
    return "\n".join(lines)

"""Closed extraction schemas from docs/extraction.md."""

from __future__ import annotations

AUTO_ACCEPT_CONFIDENCE = 0.85

# Closed predicate vocabulary. Do not add predicates here without updating the
# product decision log and extraction spec.
PREDICATES = (
    "alive",
    "dies",
    "located_at",
    "present_in_scene",
    "member_of",
    "possesses",
    "married_to",
    "parent_of",
    "sibling_of",
    "romantic_with",
    "allied_with",
    "enemy_of",
    "knows",
    "believes",
    "secret_of",
    "destroyed",
    "created",
    "occupation",
    "trait",
    "cannot",
    "goal",
    "promised",
    "fact",
)

INTRANSITIVE = frozenset({"alive", "dies", "destroyed"})

ENTITY_KINDS = ("character", "location", "object", "faction", "event", "rule", "other")
ALIAS_KINDS = ("name_variant", "nickname", "role_reference")


def _nullable_str() -> dict:
    return {"anyOf": [{"type": "string"}, {"type": "null"}]}


def candidate_schema() -> dict:
    """Anthropic structured-output JSON schema for one scene."""

    assertion = {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "subject": {"type": "string"},
            "predicate": {"type": "string", "enum": list(PREDICATES)},
            "object_entity": _nullable_str(),
            "object_value": _nullable_str(),
            "object_fact_ref": _nullable_str(),
            "polarity": {"type": "boolean"},
            "starts_here": {"type": "boolean"},
            "ends_here": {"type": "boolean"},
            "supporting_quote": {"type": "string"},
            "confidence": {"type": "number"},
            "notes": _nullable_str(),
        },
        "required": [
            "subject",
            "predicate",
            "object_entity",
            "object_value",
            "object_fact_ref",
            "polarity",
            "starts_here",
            "ends_here",
            "supporting_quote",
            "confidence",
            "notes",
        ],
    }
    str_array = {"type": "array", "items": {"type": "string"}}
    return {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "assertions": {"type": "array", "items": assertion},
            "scene_presence": str_array,
            "deaths": str_array,
            "destructions": str_array,
            "open_questions": str_array,
        },
        "required": ["assertions", "scene_presence", "deaths", "destructions", "open_questions"],
    }


def resolution_schema() -> dict:
    """Structured-output schema for LLM entity disambiguation."""

    def nullable(t: dict) -> dict:
        return {"anyOf": [t, {"type": "null"}]}

    return {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "decision": {"type": "string", "enum": ["existing", "new", "unsure"]},
            "entity_name": nullable({"type": "string"}),
            "canonical_name": nullable({"type": "string"}),
            "kind": nullable({"type": "string", "enum": list(ENTITY_KINDS)}),
            "alias_kind": nullable({"type": "string", "enum": list(ALIAS_KINDS)}),
            "dossier": nullable({"type": "string"}),
            "confidence": {"type": "number"},
            "reason": {"type": "string"},
        },
        "required": [
            "decision",
            "entity_name",
            "canonical_name",
            "kind",
            "alias_kind",
            "dossier",
            "confidence",
            "reason",
        ],
    }

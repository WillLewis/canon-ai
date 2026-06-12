"""Entity resolution — Canon AI ingestion, Stage 3 (docs/extraction.md).

`candidate assertions (names) --> entity resolution --> resolved assertions`.
Match each candidate name against the alias table (exact -> fuzzy -> LLM
disambiguation with entity dossiers). Unresolvable subjects/objects become
`provisional` entities flagged for the confirm queue. Merging two existing
entities is human-only (collision policy) and recorded with provenance.

Output is two serializations of the same resolved assertions:
  - the eval I/O contract `{"assertions": [{subject, predicate, object_value,
    object_entity, ...}]}` (eval/run_eval.py), with canonical entity NAMES; and
  - a resolution state file (entities + aliases + resolved assertions + queue +
    merge log) that maps 1:1 to db/schema.sql for the later Postgres load.

The Postgres load (entities/aliases/assertions/scene_presence) is the next
"store" step — not done here; the eval contract is JSON, so the extraction-recall
gate is checkable now without a database.

Determinism + offline-testability: exact/fuzzy matching and all serialization are
pure (no I/O); the LLM disambiguation pass takes an injected client and is fake-
tested; --no-llm routes every non-deterministic case to the confirm queue.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field

from .extract import DEFAULT_EFFORT, DEFAULT_MODEL, first_text

ENTITY_KINDS = ("character", "location", "object", "faction", "event", "rule", "other")
ALIAS_KINDS = ("name_variant", "nickname", "role_reference")
CONF_AUTO = 0.85  # LLM-decision auto-accept threshold (mirrors extraction.md gate)

# References that are aliases of some other entity rather than entities in their
# own right — never auto-created as standalone entities; routed to LLM/confirm.
ROLE_WORDS = frozenset({
    "uncle", "aunt", "brother", "sister", "father", "mother", "mom", "dad",
    "son", "daughter", "cousin", "nephew", "niece", "husband", "wife", "widow",
    "deputy", "sheriff", "captain", "cook", "boss", "stranger", "man", "woman",
    "boy", "girl", "kid", "the man", "the woman",
})
PRONOUNS = frozenset({"he", "she", "they", "him", "her", "them", "it",
                      "his", "hers", "its", "their", "we", "us"})
_INITIALS_RE = re.compile(r"^([A-Za-z]\.){2,}$")  # e.g. "C.B."

# Kind inference (a hint; the LLM / human can correct it).
_CHARACTER_SUBJECT_PREDS = frozenset({
    "knows", "believes", "cannot", "dies", "occupation", "married_to", "parent_of",
    "sibling_of", "romantic_with", "allied_with", "enemy_of", "member_of",
    "possesses", "promised", "goal", "secret_of", "trait", "alive", "present_in_scene",
})
_OBJECT_KIND_BY_PRED = {
    "located_at": "location", "possesses": "object", "member_of": "faction",
    "married_to": "character", "parent_of": "character", "sibling_of": "character",
    "romantic_with": "character", "allied_with": "character", "enemy_of": "character",
    "created": "object", "secret_of": "character",
}
_KIND_PRIORITY = {k: i for i, k in enumerate(
    ("character", "location", "object", "faction", "event", "rule", "other")
)}


# ---------------------------------------------------------------------------
# Text helpers (pure)
# ---------------------------------------------------------------------------

def _norm(s: str | None) -> str:
    return re.sub(r"[^a-z0-9 ]", "", (s or "").lower()).strip()


def _tokens(s: str | None) -> set[str]:
    return set(_norm(s).split())


def display_name(s: str) -> str:
    """Nicest display form: title-case ALL-CAPS cues, leave the rest as written."""
    s = (s or "").strip()
    if s and s.isupper() and any(c.isalpha() for c in s):
        return s.title()
    return s


def is_initials(s: str) -> bool:
    return bool(_INITIALS_RE.match((s or "").strip()))


def _strip_articles(s: str) -> str:
    toks = (s or "").strip().split()
    while toks and toks[0].lower() in ("the", "a", "an"):
        toks = toks[1:]
    return " ".join(toks)


def is_role_reference(surface: str) -> bool:
    """A role-ref / pronoun / initials surface — an alias of some entity, not its own."""
    if is_initials(surface):
        return True
    n = _norm(_strip_articles(surface))
    return (not n) or n in ROLE_WORDS or n in PRONOUNS


def occ_kind(role: str, predicate: str | None) -> str:
    if role in ("presence", "death"):
        return "character"
    if role == "destruction":
        return "location"
    if role == "subject":
        if predicate == "located_at" or predicate == "created":
            return "object"
        if predicate == "destroyed":
            return "location"
        if predicate in _CHARACTER_SUBJECT_PREDS:
            return "character"
        return "other"
    if role == "object":
        return _OBJECT_KIND_BY_PRED.get(predicate, "other")
    return "other"


def scene_label(work_title: str, scene_index: int) -> str:
    head = (work_title or "").split()[0] if work_title else "?"
    return f"{head}/sc{scene_index}"


# ---------------------------------------------------------------------------
# Mentions
# ---------------------------------------------------------------------------

@dataclass
class Mention:
    surface: str
    role: str            # subject | object | presence | death | destruction
    predicate: str | None
    story_position: int
    scene: str           # "E101/sc3"
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


def _scenes(candidates) -> list[dict]:
    if isinstance(candidates, dict):
        return candidates.get("scenes") or []
    return list(candidates or [])


def collect_mentions(candidates) -> list[Mention]:
    out: list[Mention] = []
    for sc in _scenes(candidates):
        label = scene_label(sc.get("work_title", ""), sc.get("scene_index", 0))
        pos, slug = sc.get("story_position", 0), sc.get("slug", "")

        def add(surface, role, predicate=None, quote=None):
            if surface and str(surface).strip():
                out.append(Mention(str(surface).strip(), role, predicate, pos, label, slug, quote))

        for a in sc.get("assertions") or []:
            add(a.get("subject"), "subject", a.get("predicate"), a.get("supporting_quote"))
            if a.get("object_entity"):
                add(a.get("object_entity"), "object", a.get("predicate"), a.get("supporting_quote"))
        for name in sc.get("scene_presence") or []:
            add(name, "presence")
        for name in sc.get("deaths") or []:
            add(name, "death")
        for name in sc.get("destructions") or []:
            add(name, "destruction")
    return out


def group_surfaces(mentions: list[Mention]) -> list[SurfaceInfo]:
    groups: dict[str, SurfaceInfo] = {}
    for m in mentions:
        n = _norm(m.surface)
        if not n:
            continue
        si = groups.get(n)
        if si is None:
            si = SurfaceInfo(display=m.surface, norm=n, kind_hint="other",
                             first_position=m.story_position, is_role=is_role_reference(m.surface))
            groups[n] = si
        si.occurrences.append(m)
        si.first_position = min(si.first_position, m.story_position)
        # display = the richest surface form seen (most tokens, then longest)
        if (len(_tokens(m.surface)), len(m.surface)) > (len(_tokens(si.display)), len(si.display)):
            si.display = m.surface

    for si in groups.values():
        si.display = display_name(si.display)
        si.kind_hint = _vote_kind(si.occurrences)
    # proper names before role-refs at the same first position, so the LLM/fuzzy
    # layer can attach a role-ref to an entity that already exists.
    return sorted(groups.values(), key=lambda s: (s.first_position, s.is_role, s.display))


def _vote_kind(occs: list[Mention]) -> str:
    votes: dict[str, int] = {}
    for m in occs:
        k = occ_kind(m.role, m.predicate)
        votes[k] = votes.get(k, 0) + 1
    return min(votes, key=lambda k: (-votes[k], _KIND_PRIORITY.get(k, 99)))


# ---------------------------------------------------------------------------
# Entity registry + deterministic matching
# ---------------------------------------------------------------------------

@dataclass(eq=False)  # identity-based: entities are mutable nodes, compared with `is`
class Entity:
    local_id: int
    name: str
    kind: str
    dossier: str | None
    provisional: bool
    aliases: list = field(default_factory=list)  # list[[alias, alias_kind]]

    def alias_norms(self) -> set[str]:
        return {_norm(self.name)} | {_norm(a) for a, _ in self.aliases}

    def token_sets(self) -> list[set[str]]:
        return [_tokens(self.name)] + [_tokens(a) for a, _ in self.aliases]


class Registry:
    def __init__(self):
        self.entities: list[Entity] = []
        self._id = 0

    def _next_id(self) -> int:
        self._id += 1
        return self._id

    def add(self, name: str, kind: str, dossier: str | None, provisional: bool,
            alias_kind: str = "name_variant") -> Entity:
        e = Entity(self._next_id(), display_name(name), kind, dossier, provisional,
                   [[name, alias_kind]])
        self.entities.append(e)
        return e

    def add_alias(self, e: Entity, surface: str, alias_kind: str = "name_variant",
                  upgrade: bool = True) -> None:
        if _norm(surface) not in e.alias_norms():
            e.aliases.append([surface, alias_kind])
        # Upgrade canonical to a richer *name variant* only — never promote a
        # nickname/role-reference ("the deputy") over a real name ("Cole").
        if (upgrade and alias_kind == "name_variant"
                and (len(_tokens(surface)), len(surface)) > (len(_tokens(e.name)), len(e.name))):
            e.name = display_name(surface)

    def by_name(self, name: str | None) -> Entity | None:
        n = _norm(name)
        if not n:
            return None
        return next((e for e in self.entities if n in e.alias_norms()), None)

    def match(self, surface: str):
        """Return (status, entity_or_candidates): exact | fuzzy | ambiguous | none."""
        n = _norm(surface)
        toks = _tokens(surface)
        if not n:
            return "none", None
        exact = [e for e in self.entities if n in e.alias_norms()]
        if len(exact) == 1:
            return "exact", exact[0]
        if len(exact) > 1:
            return "ambiguous", exact
        cands: list[Entity] = []
        for e in self.entities:
            for ats in e.token_sets():
                if not toks or not ats or toks == ats:
                    continue
                if toks <= ats or ats <= toks:  # one is a token-subset of the other
                    cands.append(e)
                    break
                if len(toks & ats) / len(toks | ats) >= 0.5:
                    cands.append(e)
                    break
        cands = list(dict.fromkeys(cands))
        if len(cands) == 1:
            return "fuzzy", cands[0]
        if len(cands) > 1:
            return "ambiguous", cands
        return "none", None

    def merge(self, keep: Entity, drop: Entity, reason: str) -> None:
        """Fold `drop` into `keep` (human-only collision policy)."""
        if keep is drop:
            return
        # The human chose `keep` as canonical — fold drop's names in as aliases
        # without ever upgrading keep's canonical name.
        for alias, kind in ([[drop.name, "name_variant"]] + drop.aliases):
            self.add_alias(keep, alias, kind, upgrade=False)
        keep.provisional = keep.provisional and drop.provisional
        if not keep.dossier and drop.dossier:
            keep.dossier = drop.dossier
        self.entities = [e for e in self.entities if e is not drop]


# ---------------------------------------------------------------------------
# LLM disambiguation pass (injected client)
# ---------------------------------------------------------------------------

RESOLVE_SYSTEM_PROMPT = (
    "You are the entity-resolution assistant for Canon AI, a continuity index for "
    "fiction. You never invent or generate story content. Given a referring "
    "expression from a script and the entities already known in this world, decide "
    "whether the reference denotes one of the known entities, a brand-new entity, or "
    "is unclear. Use ONLY the provided occurrences and dossiers; do not speculate "
    "beyond the text. Map the reference to AT MOST one known entity — you never merge "
    "two known entities (that is a human action)."
)


def resolution_schema() -> dict:
    def nullable(t):
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
        "required": ["decision", "entity_name", "canonical_name", "kind",
                     "alias_kind", "dossier", "confidence", "reason"],
    }


def build_disambiguation_prompt(si: SurfaceInfo, reg: Registry) -> str:
    if reg.entities:
        known = "\n".join(
            f"- {e.name} [{e.kind}]{' (provisional)' if e.provisional else ''} — "
            f"{e.dossier or '(no dossier)'}"
            for e in reg.entities
        )
    else:
        known = "(none yet)"
    occ = "\n".join(
        f"- {m.scene} ({m.role}"
        + (f", predicate {m.predicate}" if m.predicate else "")
        + ")"
        + (f' quote: "{m.quote}"' if m.quote else "")
        for m in si.occurrences[:6]
    )
    return (
        f"KNOWN ENTITIES:\n{known}\n\n"
        f'REFERENCE TO RESOLVE: "{si.display}"\n'
        f"kind hint: {si.kind_hint}\n"
        f"occurrences:\n{occ}\n\n"
        "Decide whether this reference is one of the KNOWN ENTITIES, a NEW entity, or UNSURE.\n"
        "- existing: set entity_name to the EXACT canonical name from the list above, and "
        "alias_kind (name_variant | nickname | role_reference).\n"
        "- new: set canonical_name (best full name from the text), kind, and a one-line dossier.\n"
        "- unsure: if you cannot tell from the text.\n"
        "Return confidence 0..1 and a brief reason."
    )


def disambiguate(client, si: SurfaceInfo, reg: Registry, model: str = DEFAULT_MODEL,
                 effort: str = DEFAULT_EFFORT, thinking: bool = True) -> dict:
    output_config = {"format": {"type": "json_schema", "schema": resolution_schema()}}
    if effort:
        output_config["effort"] = effort
    req = {
        "model": model,
        "max_tokens": 4000,
        "system": RESOLVE_SYSTEM_PROMPT,
        "messages": [{"role": "user", "content": build_disambiguation_prompt(si, reg)}],
        "output_config": output_config,
    }
    if thinking:
        req["thinking"] = {"type": "adaptive"}
    resp = client.messages.create(**req)
    text = first_text(resp)
    if text is None:
        raise RuntimeError(f"no text block resolving '{si.display}'")
    return json.loads(text)


# ---------------------------------------------------------------------------
# Resolution
# ---------------------------------------------------------------------------

@dataclass
class QueueItem:
    surface: str
    kind_hint: str
    reason: str                 # ambiguous | role_reference | llm_unsure
    candidates: list            # candidate existing entity names (human picks)
    entity_name: str            # the (provisional) entity this surface maps to now
    occurrences: list           # list[{scene, role, predicate, quote}]


@dataclass
class ResolutionState:
    world: str
    registry: Registry
    assertions: list            # resolved assertion dicts
    scene_presence: list        # [{scene, story_position, entities:[names]}]
    queue: list                 # list[QueueItem]
    merges: list = field(default_factory=list)


def _resolve_surface(reg: Registry, si: SurfaceInfo, client, model, effort, thinking, conf_auto):
    """Resolve one surface to an Entity. Returns (entity, queue_reason_or_None, candidates).

    Tiers (docs/extraction.md Stage 3): exact -> fuzzy -> LLM disambiguation. The
    LLM is consulted only where it can actually disambiguate: an ambiguous fuzzy
    match (pick among candidates), or a role-reference/initial that likely aliases
    an existing entity. A brand-new proper name with no fuzzy candidates is created
    deterministically — no LLM call.
    """
    status, found = reg.match(si.display)
    if status in ("exact", "fuzzy"):
        reg.add_alias(found, si.display, "name_variant")
        return found, None, []

    cand_names = [e.name for e in (found or [])] if status == "ambiguous" else []
    needs_llm = status == "ambiguous" or (status == "none" and si.is_role)

    if needs_llm and client is not None:
        dec = disambiguate(client, si, reg, model, effort, thinking)
        conf = float(dec.get("confidence") or 0.0)
        if dec.get("decision") == "existing" and conf >= conf_auto:
            e = reg.by_name(dec.get("entity_name"))
            if e:
                reg.add_alias(e, si.display, dec.get("alias_kind") or "role_reference")
                return e, None, []
        if dec.get("decision") == "new" and conf >= conf_auto:
            e = reg.add(dec.get("canonical_name") or si.display,
                        dec.get("kind") or si.kind_hint, dec.get("dossier"), provisional=False)
            return e, None, []
        # unsure / low confidence -> fall through to provisional + queue

    if status == "ambiguous":
        e = reg.add(si.display, si.kind_hint, None, provisional=True)
        return e, "ambiguous", cand_names
    # status == none
    if not si.is_role:
        e = reg.add(si.display, si.kind_hint, None, provisional=False)  # fresh named entity
        return e, None, []
    # role-reference / initials / pronoun with no match — an alias of someone unknown
    e = reg.add(si.display, si.kind_hint, None, provisional=True)
    return e, "role_reference", []


def resolve_candidates(candidates, *, world: str = "", client=None, model: str = DEFAULT_MODEL,
                       effort: str = DEFAULT_EFFORT, thinking: bool = True,
                       conf_auto: float = CONF_AUTO) -> ResolutionState:
    if isinstance(candidates, dict) and not world:
        world = candidates.get("world", "")
    mentions = collect_mentions(candidates)
    surfaces = group_surfaces(mentions)

    reg = Registry()
    surface_to_entity: dict[str, Entity] = {}
    queue: list[QueueItem] = []
    for si in surfaces:
        e, reason, cand_names = _resolve_surface(reg, si, client, model, effort, thinking, conf_auto)
        surface_to_entity[si.norm] = e
        if reason:
            queue.append(QueueItem(
                surface=si.display, kind_hint=si.kind_hint, reason=reason,
                candidates=cand_names, entity_name=e.name,
                occurrences=[{"scene": m.scene, "role": m.role,
                              "predicate": m.predicate, "quote": m.quote}
                             for m in si.occurrences[:6]],
            ))

    assertions = _build_resolved_assertions(candidates, surface_to_entity)
    presence = _build_scene_presence(candidates, surface_to_entity)
    return ResolutionState(world, reg, assertions, presence, queue)


def _entity_for(surface_to_entity: dict, surface) -> Entity | None:
    if not surface or not str(surface).strip():
        return None
    return surface_to_entity.get(_norm(str(surface)))


def _build_resolved_assertions(candidates, surface_to_entity) -> list[dict]:
    out: list[dict] = []
    for sc in _scenes(candidates):
        label = scene_label(sc.get("work_title", ""), sc.get("scene_index", 0))
        for a in sc.get("assertions") or []:
            subj = _entity_for(surface_to_entity, a.get("subject"))
            obj = _entity_for(surface_to_entity, a.get("object_entity")) if a.get("object_entity") else None
            out.append({
                "subject": subj.name if subj else a.get("subject"),
                "predicate": a.get("predicate"),
                "object_entity": obj.name if obj else None,
                "object_value": a.get("object_value"),
                "polarity": a.get("polarity", True),
                "starts_here": a.get("starts_here", True),
                "ends_here": a.get("ends_here", False),
                "supporting_quote": a.get("supporting_quote"),
                "confidence": a.get("confidence"),
                "scene": label,
                "story_position": sc.get("story_position"),
                "subject_provisional": bool(subj.provisional) if subj else True,
                "object_provisional": bool(obj.provisional) if obj else False,
            })
    return out


def _build_scene_presence(candidates, surface_to_entity) -> list[dict]:
    out: list[dict] = []
    for sc in _scenes(candidates):
        names = []
        for name in sc.get("scene_presence") or []:
            e = _entity_for(surface_to_entity, name)
            if e and e.name not in names:
                names.append(e.name)
        out.append({
            "scene": scene_label(sc.get("work_title", ""), sc.get("scene_index", 0)),
            "story_position": sc.get("story_position"),
            "entities": names,
        })
    return out


# ---------------------------------------------------------------------------
# Confirm queue + merge (human-in-the-loop)
# ---------------------------------------------------------------------------

def repoint_assertions(state: ResolutionState, old_name: str, new_name: str) -> None:
    on, nn = _norm(old_name), new_name
    for a in state.assertions:
        if _norm(a.get("subject")) == on:
            a["subject"] = nn
        if a.get("object_entity") and _norm(a["object_entity"]) == on:
            a["object_entity"] = nn
    for p in state.scene_presence:
        p["entities"] = [nn if _norm(n) == on else n for n in p["entities"]]


def merge_entities(state: ResolutionState, keep_name: str, drop_name: str, reason: str) -> bool:
    keep, drop = state.registry.by_name(keep_name), state.registry.by_name(drop_name)
    if keep is None or drop is None or keep is drop:
        return False
    old = drop.name
    state.registry.merge(keep, drop, reason)
    repoint_assertions(state, old, keep.name)
    # provisional flags on resolved assertions may have changed
    for a in state.assertions:
        if _norm(a.get("subject")) == _norm(keep.name):
            a["subject_provisional"] = keep.provisional
        if a.get("object_entity") and _norm(a["object_entity"]) == _norm(keep.name):
            a["object_provisional"] = keep.provisional
    # drop any now-stale queue items that pointed at the merged-away entity
    state.queue = [q for q in state.queue if _norm(q.entity_name) != _norm(old)]
    state.merges.append({"keep": keep.name, "drop": old, "reason": reason})
    return True


def apply_confirm_decision(state: ResolutionState, item: QueueItem, decision: str) -> bool:
    """Apply one human decision to a queue item. Returns True if it was handled
    (and should leave the queue); False to keep it queued.

    Grammar: "<n>" pick candidate n · "= Name" assign to existing entity ·
    "new Name | kind" promote provisional to a real entity · "keep" · "skip".
    """
    d = (decision or "").strip()
    if not d or d.lower() == "skip":
        return False

    if d.lower() == "keep":
        return True

    if d.isdigit():
        idx = int(d) - 1
        if 0 <= idx < len(item.candidates):
            return merge_entities(state, item.candidates[idx], item.entity_name,
                                  reason="confirm: picked candidate")
        return False

    if d.startswith("="):
        target = d[1:].strip()
        return merge_entities(state, target, item.entity_name, reason="confirm: assigned to existing")

    if d.lower().startswith("new "):
        rest = d[4:].strip()
        name, _, kind = rest.partition("|")
        name, kind = name.strip(), (kind.strip() or None)
        e = state.registry.by_name(item.entity_name)
        if e is None or not name:
            return False
        old = e.name
        e.name = display_name(name)
        e.provisional = False
        if kind in ENTITY_KINDS:
            e.kind = kind
        if old != e.name:
            state.registry.add_alias(e, old, "name_variant")
            repoint_assertions(state, old, e.name)
        for a in state.assertions:
            if _norm(a.get("subject")) == _norm(e.name):
                a["subject_provisional"] = False
            if a.get("object_entity") and _norm(a["object_entity"]) == _norm(e.name):
                a["object_provisional"] = False
        return True

    return False


def walk_queue(state: ResolutionState, prompt_fn) -> int:
    """Walk the confirm queue, calling prompt_fn(item, state) -> decision string.
    Returns the number of items resolved (removed from the queue)."""
    remaining: list[QueueItem] = []
    resolved = 0
    for item in state.queue:
        decision = prompt_fn(item, state)
        if apply_confirm_decision(state, item, decision):
            resolved += 1
        else:
            remaining.append(item)
    state.queue = remaining
    return resolved


# ---------------------------------------------------------------------------
# Serialization
# ---------------------------------------------------------------------------

def state_to_dict(state: ResolutionState) -> dict:
    return {
        "world": state.world,
        "entities": [
            {"local_id": e.local_id, "name": e.name, "kind": e.kind,
             "dossier": e.dossier, "provisional": e.provisional,
             "aliases": [{"alias": a, "kind": k} for a, k in e.aliases]}
            for e in state.registry.entities
        ],
        "assertions": state.assertions,
        "scene_presence": state.scene_presence,
        "queue": [vars(q) for q in state.queue],
        "merges": state.merges,
    }


def state_from_dict(d: dict) -> ResolutionState:
    reg = Registry()
    for ed in d.get("entities") or []:
        e = Entity(
            local_id=ed["local_id"], name=ed["name"], kind=ed["kind"],
            dossier=ed.get("dossier"), provisional=ed.get("provisional", False),
            aliases=[[a["alias"], a["kind"]] for a in ed.get("aliases") or []],
        )
        reg.entities.append(e)
        reg._id = max(reg._id, e.local_id)
    queue = [QueueItem(**q) for q in d.get("queue") or []]
    return ResolutionState(d.get("world", ""), reg, d.get("assertions") or [],
                           d.get("scene_presence") or [], queue, d.get("merges") or [])


def to_eval_assertions(state: ResolutionState) -> dict:
    """The eval/run_eval.py I/O contract — canonical entity names, flat."""
    return {"assertions": [
        {"subject": a["subject"], "predicate": a["predicate"],
         "object_value": a.get("object_value"), "object_entity": a.get("object_entity"),
         "scene": a.get("scene"), "confidence": a.get("confidence")}
        for a in state.assertions
    ]}


def render_summary(state: ResolutionState) -> str:
    n_prov = sum(1 for e in state.registry.entities if e.provisional)
    lines = [
        f"world: {state.world}",
        f"entities: {len(state.registry.entities)} ({n_prov} provisional)",
        f"resolved assertions: {len(state.assertions)}",
        f"confirm queue: {len(state.queue)} item(s)",
        "",
    ]
    for e in state.registry.entities:
        tag = "  [provisional]" if e.provisional else ""
        al = ", ".join(a for a, _ in e.aliases if _norm(a) != _norm(e.name))
        lines.append(f"  {e.name} [{e.kind}]{tag}" + (f"  (aka {al})" if al else ""))
    if state.queue:
        lines.append("")
        lines.append("confirm queue:")
        for q in state.queue:
            cands = f"  candidates: {', '.join(q.candidates)}" if q.candidates else ""
            lines.append(f"  - \"{q.surface}\" [{q.kind_hint}] — {q.reason}{cands}")
    return "\n".join(lines)

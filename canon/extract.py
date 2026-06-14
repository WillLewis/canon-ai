"""LLM extraction — Canon AI ingestion, Stage 2 (docs/extraction.md).

`scenes --> LLM extraction (structured output) --> candidate assertions JSON`
(docs/architecture.md). Per scene, with a rolling synopsis of prior scenes for
context, one structured-output call produces candidate assertions that quote the
supporting line. Entity resolution (Stage 3) and the confidence gate / confirm
queue (Stage 4) are SEPARATE later steps — candidates here still name their
subjects/objects as written; they are not yet resolved to entity IDs or loaded
into the assertions table.

Design rule (docs/decisions.md D1, D7): the LLM *extracts and explains*; it never
writes story content. Every assertion carries a verbatim supporting_quote, and
assertions whose quote cannot be found in the scene are dropped — citations are
the trust mechanism, so an unverifiable citation does not ship.

Model/params (see the claude-api reference): default claude-opus-4-8, adaptive
thinking + effort, structured output via output_config.format. NOTE: `temperature`
is intentionally NOT sent — it was removed on Opus 4.7/4.8 (400 if included);
docs/extraction.md's "temperature 0" intent (determinism) is met via prompting +
conservative extraction + low-ish effort instead.

No-training: there is no per-request flag. The ANTHROPIC_API_KEY must belong to a
zero-data-retention / no-training org (CLAUDE.md). This module assumes that.
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import asdict, dataclass, field

from .ingest import assign_story_positions

DEFAULT_MODEL = "claude-opus-4-8"
DEFAULT_EFFORT = "high"
MAX_TOKENS = 16000

# Closed predicate vocabulary (docs/extraction.md). Extraction must choose from
# this set; `fact` is the free-text escape hatch. Never invent predicates here —
# expand the vocabulary deliberately via docs/decisions.md.
PREDICATES = (
    "alive", "dies", "located_at", "present_in_scene", "member_of", "possesses",
    "married_to", "parent_of", "sibling_of", "romantic_with", "allied_with",
    "enemy_of", "knows", "believes", "secret_of", "destroyed", "created",
    "occupation", "trait", "cannot", "goal", "promised", "fact",
)
# Predicates that describe a state of the subject alone — object is optional.
# (Every assertions-table row still needs an object per db/schema.sql; the loader
# synthesizes one for these. Extraction must not drop them for lacking an object.)
INTRANSITIVE = frozenset({"alive", "dies", "destroyed"})


SYSTEM_PROMPT = f"""You are the extraction engine for Canon AI, a continuity and canon system for serialized fiction.

YOUR ONE HARD RULE: you never write, invent, continue, or paraphrase story content. You do not speculate about what might happen or fill gaps. You record only assertions the scene text explicitly states or directly implies, each tied to a verbatim quote. You are an indexer, not an author.

For the CURRENT scene only, extract candidate continuity assertions as structured output.

PREDICATES — choose exactly one from this closed set (never invent one):
{', '.join(PREDICATES)}
Use `fact` with a free-text object_value for anything true and citable that no other predicate fits. `alive` is implicit at a character's first appearance — do NOT emit `alive`; emit `dies` when a character dies.

EVIDENCE:
- Every assertion MUST include `supporting_quote`: text copied VERBATIM from the current scene (exact words). If you cannot quote it from THIS scene, do not emit the assertion.
- Extract only what the text states or directly implies. No inference chains ("she seems upset, so..."). Inference is a later layer's job, not yours.
- Direct implication INCLUDES: acting on information (going straight to a hidden thing's location implies `knows` it), titles and signage ("DEPUTY COLE", working the desk of the harbormaster's office) implying `occupation`, and an object being placed/found somewhere implying `located_at(object, place)`.

OBJECT VALUES ARE CANONICAL HANDLES (load-bearing for cross-scene checks):
- `object_value` is a SHORT lowercase handle (2–5 words), never a sentence: "ledger location", "two sets of numbers", "drive", "find Danny". Manner, detail, and nuance go in `notes`.
- THE SAME FACT GETS THE SAME HANDLE EVERYWHERE: before inventing a handle, scan PRIOR SCENES for the same fact and COPY its object_value character-for-character — across scenes AND across characters (if Tobias `knows "ledger location"`, Cole learning it is also `knows "ledger location"`).
- HANDLE CONVENTIONS (use these exact shapes so identical facts collide):
  "<thing> location" for where something is hidden/kept ("ledger location");
  bare infinitive verbs for capabilities ("drive", "swim");
  "find <person>" for search goals; "<person> payment" for money entries;
  the SHORTEST noun phrase a fan-wiki index would use, never a clause.
- HANDLE PRECISION: reuse a handle ONLY for the SAME fact. A related fact is a
  DIFFERENT handle — knowing where someone goes on Thursdays is NOT knowing
  "ledger location"; a decoy/fake object is NOT the real one. When unsure
  whether two facts are identical, coin a distinct handle.
- `dies` / `destroyed`: leave ALL object fields null; put the manner in `notes`.

KNOWLEDGE & BELIEF (this distinction is load-bearing):
- `knows`: first-hand — the character did it, saw it, read it, was told it on-screen, or acts on it. Hiding something yourself means you `knows` its location; reading a ledger entry means you `knows` its contents; stating a specific fact from your own observation ("I've seen where he goes") is `knows`.
- `believes`: secondhand, suspicion, inference, or an unverifiable claim. "suspects" / "thinks" / "guesses" != `knows`.
- A character's dialogue claim about the WORLD is `believes(speaker, X)` — NOT a world-fact (characters lie). Record the world-fact too only if action lines or independent sources corroborate it. But note: a flat first-person factual statement of something the speaker has direct access to still earns `knows(speaker, ...)` — the epistemic fact, not the world-fact.

CAPABILITY VIOLATIONS & ACTIVITIES:
- When a character performs a notable physical activity on screen (driving,
  swimming, shooting, riding), ALWAYS emit `fact(subject, <bare verb>)` — e.g.
  Mara behind the wheel -> `fact(Mara, "drive")` — in addition to any location
  change. Continuity checks join on these bare-verb handles.
- If PRIOR SCENES establish `cannot(X, handle)` and X now performs that very action, ALSO emit `cannot(X, same handle)` with `polarity: false` quoting the action line (this records "X is doing the thing canon says X cannot do").

POLARITY & NEGATION:
- `polarity: true` = the assertion holds. Use `polarity: false` for an explicit negation of a relational predicate (e.g. "he is not a member").
- For an inability, use predicate `cannot` with `polarity: true` and the capability in `object_value` (e.g. cannot / "drive").

TIME (starts_here / ends_here):
- Default `starts_here: true` (the fact becomes true at this scene) and `ends_here: false`.
- BACKDATING: if dialogue establishes the fact began earlier ("I've known since the lake house"), set `starts_here: false` and explain in `notes`. Do not guess the earlier position.

OBJECTS (set unused object fields to null):
- `object_entity`: when the object is another named entity (character/location/object/faction). Use the name as written; entity resolution happens later.
- `object_value`: when the object is a literal/free-text value (a role, a trait, a fact).
- `object_fact_ref`: a short description of another fact this assertion is about (for `knows`/`believes` of a fact).

ALSO REPORT for this scene:
- `scene_presence`: every character physically present or speaking in the scene (by name as written).
- `deaths`: characters who die in this scene. `destructions`: locations/objects destroyed in this scene.
- `open_questions`: setups, promises, or unresolved questions this scene raises (for dangling-thread tracking).

CONFIDENCE: 0..1, your calibrated certainty the assertion is correct and citable. Be conservative — a missed borderline assertion is cheaper than a wrong one.
"""


def candidate_schema() -> dict:
    """JSON Schema for one scene's candidate output (docs/extraction.md).

    Structured-output constraints (see claude-api reference): every object sets
    additionalProperties:false and lists all keys in `required`; nullable fields
    use anyOf[..., null]; numeric ranges (confidence 0..1) are validated in
    post-processing, not the schema.
    """
    def nullable_str() -> dict:
        return {"anyOf": [{"type": "string"}, {"type": "null"}]}

    assertion = {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "subject": {"type": "string"},
            "predicate": {"type": "string", "enum": list(PREDICATES)},
            "object_entity": nullable_str(),
            "object_value": nullable_str(),
            "object_fact_ref": nullable_str(),
            "polarity": {"type": "boolean"},
            "starts_here": {"type": "boolean"},
            "ends_here": {"type": "boolean"},
            "supporting_quote": {"type": "string"},
            "confidence": {"type": "number"},
            "notes": nullable_str(),
        },
        "required": [
            "subject", "predicate", "object_entity", "object_value",
            "object_fact_ref", "polarity", "starts_here", "ends_here",
            "supporting_quote", "confidence", "notes",
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


@dataclass
class SceneContext:
    work_title: str
    scene_index: int
    slug: str
    story_position: int
    is_flashback: bool
    raw_text: str


@dataclass
class SceneExtraction:
    work_title: str
    scene_index: int
    slug: str
    story_position: int
    is_flashback: bool
    assertions: list = field(default_factory=list)
    scene_presence: list = field(default_factory=list)
    deaths: list = field(default_factory=list)
    destructions: list = field(default_factory=list)
    open_questions: list = field(default_factory=list)
    dropped: dict = field(default_factory=dict)


def scene_contexts(works) -> list[SceneContext]:
    """Flatten parsed works into per-scene contexts with global story positions."""
    ctxs: list[SceneContext] = []
    for w, rows in assign_story_positions(works, start=1):
        for sc, pos in rows:
            ctxs.append(
                SceneContext(w.title, sc.scene_index, sc.slug, pos, sc.is_flashback, sc.raw_text)
            )
    return ctxs


# ---------------------------------------------------------------------------
# Prompt assembly (pure)
# ---------------------------------------------------------------------------

def build_synopsis(prior: list[SceneExtraction], facts_per_scene: int = 12) -> str:
    """Compact, deterministic rolling synopsis built from prior extractions.

    Not generated prose (D1) — just prior scenes' presence + extracted facts, so
    the model has continuity context without us authoring anything.
    """
    lines: list[str] = []
    for ex in prior:
        facts = []
        for a in ex.assertions:
            obj = a.get("object_entity") or a.get("object_value") or a.get("object_fact_ref") or ""
            neg = "" if a.get("polarity", True) else "NOT "
            facts.append(f"{neg}{a.get('subject', '?')} {a.get('predicate', '?')} {obj}".strip())
        line = f"[{ex.work_title} sc{ex.scene_index} | {ex.slug}] present: {', '.join(ex.scene_presence)}"
        if facts:
            line += " | " + "; ".join(facts[:facts_per_scene])
        lines.append(line)
    return "\n".join(lines)


def build_user_prompt(ctx: SceneContext, synopsis: str) -> str:
    if synopsis:
        head = "PRIOR SCENES (context only — do NOT re-extract these):\n" + synopsis
    else:
        head = "PRIOR SCENES: none — this is the first scene."
    fb = " [FLASHBACK]" if ctx.is_flashback else ""
    return (
        f"{head}\n\n"
        f"CURRENT SCENE — extract assertions for THIS scene only.\n"
        f"work: {ctx.work_title}\n"
        f"scene: {ctx.slug}{fb}\n"
        f"story_position: {ctx.story_position}\n"
        f"---\n{ctx.raw_text}\n---\n\n"
        f"Return the structured candidate assertions for this scene."
    )


def build_request(model: str, system: str, user: str, effort: str = DEFAULT_EFFORT,
                  thinking: bool = True) -> dict:
    """Assemble messages.create kwargs. No `temperature` — removed on Opus 4.7/4.8."""
    output_config: dict = {"format": {"type": "json_schema", "schema": candidate_schema()}}
    if effort:
        output_config["effort"] = effort
    req: dict = {
        "model": model,
        "max_tokens": MAX_TOKENS,
        "system": system,
        "messages": [{"role": "user", "content": user}],
        "output_config": output_config,
    }
    if thinking:
        req["thinking"] = {"type": "adaptive"}
    return req


# ---------------------------------------------------------------------------
# Response handling / quality gates (pure)
# ---------------------------------------------------------------------------

def _normalize(s: str) -> str:
    s = (s.replace("’", "'").replace("‘", "'")
          .replace("“", '"').replace("”", '"')
          .replace("—", "-").replace("–", "-"))
    return " ".join(s.split()).casefold()


def quote_in_text(quote: str, text: str) -> bool:
    """Tolerant verbatim check (whitespace/case/smart-punct normalized)."""
    return _normalize(quote) in _normalize(text)


def first_text(resp) -> str | None:
    """Return the first text block's text, skipping thinking/other blocks."""
    for b in getattr(resp, "content", None) or []:
        if getattr(b, "type", None) == "text":
            return b.text
    return None


def post_process(ctx: SceneContext, obj: dict, verify_quotes: bool = True):
    """Apply the evidence gates from docs/extraction.md and return (kept, dropped).

    Drops: no quote; predicate outside the closed set; relational predicate with
    no object; (if verify_quotes) quote not found verbatim in the scene.
    """
    dropped = {"no_quote": 0, "bad_predicate": 0, "no_object": 0, "quote_unverified": 0}
    kept = []
    for a in obj.get("assertions") or []:
        quote = (a.get("supporting_quote") or "").strip()
        if not quote:
            dropped["no_quote"] += 1
            continue
        if a.get("predicate") not in PREDICATES:
            dropped["bad_predicate"] += 1
            continue
        has_object = bool(a.get("object_entity") or a.get("object_value") or a.get("object_fact_ref"))
        if not has_object and a.get("predicate") not in INTRANSITIVE:
            dropped["no_object"] += 1
            continue
        if verify_quotes and not quote_in_text(quote, ctx.raw_text):
            dropped["quote_unverified"] += 1
            continue
        try:
            conf = float(a.get("confidence"))
        except (TypeError, ValueError):
            conf = 0.0
        a["confidence"] = max(0.0, min(1.0, conf))
        a["polarity"] = bool(a.get("polarity", True))
        a["starts_here"] = bool(a.get("starts_here", True))
        a["ends_here"] = bool(a.get("ends_here", False))
        kept.append(a)
    return kept, dropped


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------

def extract_scene(client, ctx: SceneContext, synopsis: str, model: str = DEFAULT_MODEL,
                  effort: str = DEFAULT_EFFORT, thinking: bool = True,
                  verify_quotes: bool = True) -> SceneExtraction:
    req = build_request(model, SYSTEM_PROMPT, build_user_prompt(ctx, synopsis), effort, thinking)
    resp = client.messages.create(**req)
    if getattr(resp, "stop_reason", None) == "refusal":
        raise RuntimeError(f"model refused on scene pos {ctx.story_position} ({ctx.slug})")
    text = first_text(resp)
    if text is None:
        raise RuntimeError(
            f"no text block in response for pos {ctx.story_position} "
            f"(stop_reason={getattr(resp, 'stop_reason', None)})"
        )
    obj = json.loads(text)
    assertions, dropped = post_process(ctx, obj, verify_quotes)
    return SceneExtraction(
        work_title=ctx.work_title, scene_index=ctx.scene_index, slug=ctx.slug,
        story_position=ctx.story_position, is_flashback=ctx.is_flashback,
        assertions=assertions,
        scene_presence=obj.get("scene_presence") or [],
        deaths=obj.get("deaths") or [],
        destructions=obj.get("destructions") or [],
        open_questions=obj.get("open_questions") or [],
        dropped=dropped,
    )


def run_extraction(client, works, model: str = DEFAULT_MODEL, effort: str = DEFAULT_EFFORT,
                   thinking: bool = True, verify_quotes: bool = True, limit: int | None = None,
                   on_result=None) -> list[SceneExtraction]:
    """Extract every scene in story order, threading a rolling synopsis forward."""
    ctxs = scene_contexts(works)
    if limit is not None:
        ctxs = ctxs[:limit]
    results: list[SceneExtraction] = []
    for ctx in ctxs:
        ex = extract_scene(client, ctx, build_synopsis(results), model, effort, thinking, verify_quotes)
        results.append(ex)
        if on_result:
            on_result(ex)
    return results


def to_json(world: str, model: str, results: list[SceneExtraction]) -> dict:
    return {"world": world, "model": model, "scenes": [asdict(r) for r in results]}


# ---------------------------------------------------------------------------
# Client / CLI helpers
# ---------------------------------------------------------------------------

def has_credentials() -> bool:
    return bool(os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_AUTH_TOKEN"))


def make_client():
    try:
        import anthropic
    except ModuleNotFoundError as e:
        raise RuntimeError(
            "anthropic is not installed. Run `pip install -r requirements.txt`, "
            "or pass --dry-run to assemble prompts without calling the API."
        ) from e
    # Resolves ANTHROPIC_API_KEY / ANTHROPIC_AUTH_TOKEN from the environment.
    # That key must belong to a no-training / zero-data-retention org (CLAUDE.md).
    return anthropic.Anthropic()


def render_dry_run(ctxs: list[SceneContext], model: str = DEFAULT_MODEL,
                   effort: str = DEFAULT_EFFORT, limit: int | None = None) -> str:
    shown = ctxs[: (limit or 1)]
    lines = [
        f"model: {model}   effort: {effort}   thinking: adaptive   "
        f"(no temperature — removed on Opus 4.7/4.8)",
        f"scenes to extract: {len(ctxs)}",
        "",
        "=== SYSTEM PROMPT ===",
        SYSTEM_PROMPT,
        "",
    ]
    for ctx in shown:
        lines.append(f"=== USER PROMPT — pos {ctx.story_position} | {ctx.slug} ===")
        lines.append(build_user_prompt(ctx, ""))  # dry run has no prior extractions
        lines.append("")
    if len(ctxs) > len(shown):
        lines.append(
            f"... {len(ctxs) - len(shown)} more scene(s); each receives a rolling "
            f"synopsis built from prior scenes' extractions."
        )
    return "\n".join(lines)

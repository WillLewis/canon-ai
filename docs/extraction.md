# Canon AI — Extraction Spec

## Stages

1. **Segment.** Parse script into scenes. Fountain: native headings. PDF/docx: layout parse, fall back to LLM re-segmentation (time-boxed; see SPEC open questions). Output: `scene_id`, `slug` (INT. BOATHOUSE — NIGHT), `story_position`, `is_flashback`, raw text.
2. **Extract.** Per scene (with a rolling synopsis of prior scenes for context), one structured-output call → candidate assertions. Temperature 0. Model must quote the supporting line.
3. **Resolve entities.** Match candidate names against the alias table (exact → fuzzy → LLM disambiguation with entity dossiers). Unresolvable subjects/objects become `provisional` entities flagged for the confirm queue.
4. **Gate.** `confidence ≥ 0.85` → auto-accept as `draft`; below → human-confirm queue (CLI: show quote + assertion, accept/edit/reject). Human-confirmed and batch-promoted assertions become `canon`.

## Candidate assertion JSON schema

```json
{
  "assertions": [
    {
      "subject": "Dani",
      "predicate": "knows",
      "object_entity": null,
      "object_value": "Marcus is having an affair",
      "object_fact_ref": null,
      "polarity": true,
      "starts_here": true,
      "ends_here": false,
      "supporting_quote": "I've known since the lake house.",
      "confidence": 0.93,
      "notes": "Dani states knowledge predating this scene; see backdating rule."
    }
  ],
  "scene_presence": ["Dani", "Marcus"],
  "deaths": [],
  "destructions": [],
  "open_questions": ["Why did Dani stay quiet since the lake house?"]
}
```

### Predicate vocabulary (v0 — closed set; extraction must choose from these)

`alive`(implicit true at first appearance), `dies`, `located_at`, `present_in_scene`, `member_of`, `possesses`, `married_to`, `parent_of`, `sibling_of`, `romantic_with`, `allied_with`, `enemy_of`, `knows`, `believes`(false-belief support), `secret_of`, `destroyed`, `created`, `occupation`, `trait`, `cannot`(e.g., "Maya cannot drive"), `goal`, `promised`.

Anything that doesn't fit: predicate `fact` with free-text `object_value`. Do not invent predicates; expand the vocabulary deliberately via decisions.md.

## Prompting rules

- Extract only what the text **states or directly implies**; no inference chains ("she seems angry, so..."). Inference is the rules layer's job, not extraction's.
- Every assertion must carry a verbatim `supporting_quote` from the scene. No quote → drop it.
- **Backdating:** if dialogue establishes a fact began earlier ("I've known since the lake house"), emit the assertion with `starts_here: false` and a note; the placement step anchors the start at the referenced event if it resolves, else at an open lower bound. These are prime confirm-queue candidates.
- **Negation/uncertainty:** "suspects" ≠ `knows` — emit `believes` with the suspected content. This distinction powers the knowledge-state checks; be strict.
- Dialogue claims are not facts: a character asserting something emits `believes(speaker, X)`, and only also a world-fact if the script corroborates (action lines, multiple independent sources) — otherwise route to confirm queue. Characters lie.

## Entity resolution v0

- `entities(id, canonical_name, kind, dossier)` + `aliases(entity_id, alias, kind)` where alias kinds: name-variant, nickname, role-reference ("her husband"), pronoun-window (scene-scoped, resolved at extraction time, never stored as alias).
- Seed aliases from cast lists / character pages when present.
- Collision policy: never auto-merge two existing entities; merging is human-only (CLI command), recorded with provenance.

## Quality harness

- `fixtures/greyharbor/answer-key.md` lists expected assertions and planted errors. The eval script scores extraction recall, check precision/recall, and prints the diff. Run after every prompt or schema change. **Recall ≥80%, all planted errors found, ≤2 false positives/episode** is the Phase 0 bar.

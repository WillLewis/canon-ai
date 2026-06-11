# Canon AI — Architecture

## Core idea

The atomic unit is not the entity, it's the **time-stamped, sourced assertion**:

```
(subject) —predicate→ (object)  valid over [story positions]  · cited to scene · status · confidence · author
```

Example: `(Aria) member_of (Iron Order)` valid `[pos 120, pos 480)`, cited `S1E04/sc12`, status `canon`.

Everything else — graph views, checks, bible documents, answers — is a projection or query over the assertions table.

## The two clocks (v0 simplification)

The full model is bitemporal: **story time** (when it happens in the fiction) vs **canon time** (when/where it was established — prequels, retcons, reveals). **v0 implements story time only**, as a single global integer axis:

- Every scene gets a `story_position` (integer). Default = reading order across the ingested corpus.
- Flashbacks: scene carries `is_flashback` + an optional overridden diegetic position. v0 may simply exclude flashback scenes from interval checks rather than solve partial-order reasoning. Do not build a constraint solver.
- Canon time exists in the schema only as provenance (`established_in_scene`); retcon semantics (`status='retconned'` superseding) are recorded but not reasoned over.

Postgres `int4range` over story positions + GiST is the temporal backbone. "True at position T" = `valid_during @> T`. "Conflict" = `valid_during && valid_during`.

## Layers over the graph

1. **State layer** — stateful predicates valid over ranges: `alive`, `located_at`, `member_of`, `possesses`, `married_to`, `destroyed`…
2. **Epistemic layer** — `knows(character, fact_assertion)` valid from a position onward. The sleeper killer feature: "what does Dani know at scene 14?" Drama is asymmetric knowledge; this layer is what no codex-style competitor has.
3. **Constraint layer** — v0 ships only **universal structural invariants** (no per-world rule authoring):
   - dead characters don't act/speak after death interval starts;
   - one character, one location per overlapping interval;
   - characters can't act on facts before their `knows` interval starts;
   - destroyed locations/objects can't be used while destroyed;
   - assertions can't reference unresolved entities.
   Some invariants run at write time as exclusion constraints; the rest run as scan queries (`db/checks.sql`).
4. **Provenance layer** — every assertion: `established_in_scene`, extraction `confidence`, `confirmed_by_human`, `status`. The future rights-ledger attaches here; v0 just never drops the fields.

## Pipeline

```
script file ──► ingest ──► scenes (ids, story positions)
scenes ──► LLM extraction (structured output) ──► candidate assertions JSON
candidates ──► entity resolution (alias table + LLM pass) ──► resolved assertions
resolved ──► confidence gate ──► auto-accept │ human-confirm queue (CLI)
assertions ──► Postgres
                 ├──► ask  (NL → SQL → answer + citations)
                 ├──► check (checks.sql → findings report)
                 └──► networkx projection (graph queries/viz, later)
```

Design rule: **the LLM extracts and explains; SQL judges.** Findings are produced by deterministic queries over assertions, then optionally narrated by an LLM. Never let an LLM free-read the corpus and "decide" a violation — that's the drift we exist to eliminate.

## False-positive doctrine

Over-flagging is the trust killer. Mechanisms, in order:
1. Confidence gate routes shaky extractions to human confirmation instead of into checks.
2. Every check joins against `status != 'sealed'` — the writer's "mark intentional" is permanent and per-assertion-pair.
3. Severity tiers (`critical` / `warning` / `note`); default report shows critical+warning.
4. Tuning bias: prefer a missed borderline flag over a false alarm.

## Graph-shaped queries

Source of truth stays relational. For paths/patterns/centrality/visualization: load assertions into **networkx** in memory (corpus is tiny — tens of thousands of rows), compute, discard. No second database. Revisit-trigger documented in docs/decisions.md (D2).

## Security posture (v0)

- Anthropic API in no-training configuration; keys in env, never committed.
- Supabase: RLS from the moment auth exists; encrypted at rest by default.
- Rights hygiene: only owner-supplied or fixture material is ever persisted.
- Tenancy story for later: per-world RLS now → schema/db-per-show on self-hosted Postgres for enterprise. Same engine, low-drama migration.

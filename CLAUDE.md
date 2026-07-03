# Canon AI

Continuity and canon engine for serialized fiction (TV, vertical/microdrama, web series, adaptations). Canon ingests scripts and world docs, builds a temporal assertion graph, and **verifies** story logic — contradictions, knowledge-state violations, timeline conflicts, dangling setups — with citations back to source scenes.

## The one rule that governs everything

**Canon never writes the user's story.** No loglines, no dialogue, no pitches, no beats. Canon indexes, retrieves, checks, and explains — the writer is the only generator. This is the product's trust position with writers (post-2023 WGA environment) and its contractual safety (Canon never produces "literary material"). If a feature idea requires generating story content, it is out of scope. Period.

Corollary: every flag, answer, or finding **must cite its source** (episode/scene). A claim without a citation does not ship.

## Stack (decided — see docs/decisions.md before relitigating)

- **Supabase Postgres** is the source of truth. Local dev via `supabase start` (Docker).
- **Temporal model:** Postgres range types (`int4range` over a story-position axis) + GiST indexes + **exclusion constraints** for write-time continuity invariants. Requires `btree_gist` extension.
- **No Neo4j.** Graph-shaped queries (paths, patterns, viz) run on an in-memory **networkx projection** of the assertions table. Revisit only if a hero feature needs interactive pathfinding the projection can't handle.
- **pgvector** — deferred until the Audience Memory feature (beat-similarity). Schema reserves space; do not build yet.
- **LLM calls:** Anthropic API with structured outputs for extraction. Use no-training endpoints/config only.
- **Language:** Python (pipeline + checks), simple web UI later. Keep dependencies boring.

## Repo map

- `PLAN.md` — phased plan, exit/kill criteria, current phase. **Check this first each session.**
- `SPEC.md` — product spec (PRD): goals, non-goals, P0/P1/P2, acceptance criteria.
- `docs/architecture.md` — assertion model, the two clocks, pipeline stages, layers.
- `docs/extraction.md` — extraction pipeline spec: JSON schema, prompts, entity resolution, confidence/confirm queue.
- `docs/decisions.md` — ADR log. Read before proposing architecture changes.
- `docs/workstreams.md` — workstream split + merge order. Read before opening a branch; never run two branches with in-flight DB migrations.
- `docs/readers-report.md` — Reader's Report product spec (note families, grounding law, funnel, launch tiers).
- `db/schema.sql` — DDL: tables, ranges, exclusion constraints, indexes.
- `db/checks.sql` — MVP continuity checks as SQL, with severity and explanation.
- `fixtures/greyharbor/` — original 2-episode test show with **planted continuity errors** + `answer-key.md`. This is the grading harness for Phase 0.
- `corpus/` — owned-copy scripts for **internal smoke testing only** (gitignored except README.md; files exist only on the operator's machine — see `corpus/README.md` for canonical paths, rights, and allowed uses). Never eval-graded, never demo material, never committed.

## Working conventions

- **Rights hygiene is absolute.** Never ingest, fetch, or test on scripts we don't own. Fixtures are original material written for this repo. If the user pastes third-party script text, flag it and don't persist it.
- **Phase discipline:** we are in Phase 0 (see PLAN.md). Do not build UI, auth, agents, pgvector, Build-mode bible generation, or anything not in the Phase 0 exit criteria. Park ideas in PLAN.md's parking lot.
- **Definition of done, Phase 0:** `ingest fixtures → assertions in Postgres → ask-the-bible CLI answers with citations → checks.sql finds the planted errors in answer-key.md with ≤ 2 false positives per episode.`
- **False positives are the product risk.** When tuning extraction or checks, prefer missing a borderline flag over crying wolf. Every check must support a `sealed` status (writer marked intentional) that suppresses it.
- Migrations via supabase CLI; never edit schema in the dashboard.
- Small commits, plain-English messages; note which PLAN.md item each serves.

## Design System
Always read DESIGN.md before making any visual or UI decisions.
All font choices, colors, spacing, and aesthetic direction are defined there.
Do not deviate without explicit user approval.
In QA mode, flag any code that doesn't match DESIGN.md.

## Owner context

Solo founder, nights/weekends (~10–15 hrs/wk), building with Claude Code + Codex. Optimize for: fewest moving parts, fastest path to the Phase 0 demo, and code a single person can hold in their head.

# Canon AI

AI-native studio for serialized fiction (TV, vertical/microdrama, web series, adaptations). Canon generates, branches, rewrites, and tests scripted-story material against a temporal assertion graph, with deterministic continuity checks and citations back to source scenes wherever Canon makes a claim.

## The principle that governs generation

**AI output is a proposal. The writer decides what becomes the story.** Canon may generate, branch, rewrite, and transform story material only as proposed changes with provenance, branch isolation, and explicit writer acceptance/canonization. Canon never silently makes AI output canon, never silently regenerates downstream material, and never treats a model response as authoritative story state without writer action.

Corollary: every flag, answer, finding, and Story Test **must cite its source** (episode/scene). Generated candidates carry provider, model, prompt-version, context-manifest, branch, and candidate provenance instead of pretending to be canon.

## Stack (decided — see docs/decisions.md before relitigating)

- **Supabase Postgres** is the source of truth. Local dev via `supabase start` (Docker).
- **Temporal model:** Postgres range types (`int4range` over a story-position axis) + GiST indexes + **exclusion constraints** for write-time continuity invariants. Requires `btree_gist` extension.
- **No Neo4j.** Graph-shaped queries (paths, patterns, viz) run on an in-memory **networkx projection** of the assertions table. Revisit only if a hero feature needs interactive pathfinding the projection can't handle.
- **pgvector** — deferred until the Audience Memory feature (beat-similarity). Schema reserves space; do not build yet.
- **LLM calls:** existing extraction/resolution/report paths use Anthropic structured outputs. New generation uses a multi-provider layer (deterministic fake, Anthropic, OpenAI) with server-side keys, model profiles, structured outputs, normalized streaming events, and documented provider retention posture.
- **Language:** Python (pipeline + checks + web UI). Keep dependencies boring.

## Repo map

- `PLAN.md` — phased plan, exit/kill criteria, current phase. **Check this first each session.**
- `SPEC.md` — product spec (PRD): goals, non-goals, P0/P1/P2, acceptance criteria.
- `docs/architecture.md` — assertion model, the two clocks, pipeline stages, layers.
- `docs/extraction.md` — extraction pipeline spec: JSON schema, prompts, entity resolution, confidence/confirm queue.
- `docs/decisions.md` — ADR log. Read before proposing architecture changes.
- `docs/generation-pivot.md` — generation-first doctrine, frozen G0 contracts, Story DNA/context/provider/branch/proposal shapes.
- `docs/workstreams.md` — workstream split + merge order. Read before opening a branch; never run two branches with in-flight DB migrations.
- `docs/readers-report.md` — Reader's Report product spec (note families, grounding law, funnel, launch tiers).
- `db/schema.sql` — DDL: tables, ranges, exclusion constraints, indexes.
- `db/checks.sql` — MVP continuity checks as SQL, with severity and explanation.
- `fixtures/greyharbor/` — original 2-episode test show with **planted continuity errors** + `answer-key.md`. This is the grading harness for Phase 0.
- `corpus/` — owned-copy scripts for **internal smoke testing only** (gitignored except README.md; files exist only on the operator's machine — see `corpus/README.md` for canonical paths, rights, and allowed uses). Never eval-graded, never demo material, never committed.

## Working conventions

- **Rights hygiene is absolute.** Never ingest, fetch, or test on scripts we don't own. Fixtures are original material written for this repo. If the user pastes third-party script text, flag it and don't persist it.
- **Wave discipline:** follow `docs/workstreams.md`. The generation pivot is serialized by waves G0-G4; do not open migration work outside the one migration lane, and do not build beyond the active workstream's scope.
- **Definition of done for the first generation slice:** writer selects a scene end, streams three alternate continuations, compares hard Canon tests, accepts one into a branch, main remains unchanged, accepted text re-ingests as proposed assertions, restoration works, branch exports as Fountain, and all tests pass without live API calls.
- **False positives are the product risk.** When tuning extraction or checks, prefer missing a borderline flag over crying wolf. Every check must support a `sealed` status (writer marked intentional) that suppresses it.
- Migrations via supabase CLI; never edit schema in the dashboard.
- Small commits, plain-English messages; note which PLAN.md item each serves.

## Design System
Always read DESIGN.md before making any visual or UI decisions.
All font choices, colors, spacing, and aesthetic direction are defined there.
Do not deviate without explicit user approval.
In QA mode, flag any code that doesn't match DESIGN.md.

## Owner context

Solo founder, nights/weekends (~10–15 hrs/wk), building with Codex + Codex. Optimize for: fewest moving parts, fastest path to the Phase 0 demo, and code a single person can hold in their head.

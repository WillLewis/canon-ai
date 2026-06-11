# Canon AI

**The continuity and canon engine for serialized fiction. Canon never writes your story — it keeps it true.**

Verification, not generation: ingest scripts and world docs → temporal assertion graph in Postgres → ask questions and run continuity checks, every answer and flag cited to its source scene.

## Status

Pre-code handoff. Phase 0 (see `PLAN.md`) builds the spike: `ingest → extract → store → ask/check` CLI, graded against `fixtures/greyharbor`.

## Read in this order

1. `CLAUDE.md` — project rules, stack, conventions (Claude Code reads this automatically)
2. `PLAN.md` — phases, exit criteria, kill triggers, parking lot
3. `SPEC.md` — PRD: goals, non-goals, P0/P1/P2 requirements
4. `docs/architecture.md` — assertion model, layers, pipeline, false-positive doctrine
5. `docs/extraction.md` — extraction JSON schema, predicate vocabulary, prompting rules
6. `docs/decisions.md` — why Postgres/no-Neo4j, why verification-only, etc. **Read before relitigating.**
7. `db/schema.sql` + `db/checks.sql` — validated against Postgres 16 (schema applies clean; exclusion constraint and all 7 checks tested)
8. `fixtures/greyharbor/` — original 2-episode test show with planted errors + answer key (the Phase 0 grading harness)

## Quickstart (target developer experience — to be built)

```bash
supabase start                          # local Postgres stack
psql $DB_URL -f db/schema.sql           # apply schema
canon ingest fixtures/greyharbor/*.fountain --world greyharbor
canon confirm --world greyharbor        # review low-confidence assertions
canon ask "what does Cole know about the ledger, and when?" --world greyharbor
canon check --world greyharbor          # should find P1–P4 from the answer key
```

## The one rule

If a proposed feature generates story content, it's out of scope. Forever. See `docs/decisions.md` D1.

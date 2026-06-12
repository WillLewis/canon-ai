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
python eval/run_eval.py --assertions out/assertions.json --findings out/findings.json # Phase 0 gates: PASS/FAIL (try --demo now)
```

## Ingestion (implemented — Phase 0, step 1 of the pipeline)

The `ingest` step (`docs/extraction.md` Stage 1 "Segment") is built: Fountain → scenes
with `slug`, global `story_position`, `is_flashback`, and planted-annotation-free
`raw_text`, loaded into `worlds`/`works`/`scenes`. The Fountain parser is stdlib-only;
only the Postgres loader needs a dependency.

```bash
# Preview segmentation — no database, no dependencies:
python -m canon ingest fixtures/greyharbor/*.fountain --world greyharbor --dry-run

# Load into Postgres (after `supabase start`):
pip install -r requirements.txt
export CANON_DB_URL=postgresql://postgres:postgres@127.0.0.1:54322/postgres
psql "$CANON_DB_URL" -f db/schema.sql
python -m canon ingest fixtures/greyharbor/*.fountain --world greyharbor --reset-world

# Tests (graded against the greyharbor fixtures):
python -m pytest -q            # or: python tests/test_fountain.py
```

## Extraction (implemented — Phase 0, step 2 of the pipeline)

The `extract` step (`docs/extraction.md` Stage 2) runs one structured-output Claude
call per scene (rolling synopsis for context) → candidate assertions JSON using the
closed predicate vocabulary, each with a verbatim `supporting_quote`. Assertions
whose quote can't be found in the scene are dropped (citations are the trust
mechanism). Entity resolution and the confidence gate are later steps — candidates
still name subjects/objects as written and are not yet loaded into Postgres.

```bash
# Preview the exact prompts — no API key, no dependencies:
python -m canon extract fixtures/greyharbor/*.fountain --world greyharbor --dry-run

# Run extraction (needs anthropic + a no-training/ZDR API key):
pip install -r requirements.txt
export ANTHROPIC_API_KEY=...    # must belong to a no-training / zero-data-retention org
python -m canon extract fixtures/greyharbor/*.fountain --world greyharbor \
  --out build/greyharbor.candidates.json
#   --model (default claude-opus-4-8) · --effort · --limit N · --no-verify-quotes
```

Model note: `temperature` is intentionally not sent (removed on Opus 4.7/4.8);
determinism comes from prompting + conservative extraction + `effort`.

## Entity resolution (implemented — Phase 0, step 3 of the pipeline)

The `resolve` step (`docs/extraction.md` Stage 3) turns candidate names into
canonical entities: **exact → fuzzy → LLM disambiguation**. Unresolvable
subjects/objects become `provisional` entities flagged for the confirm queue;
merging two entities is human-only and logged with provenance. Output is the
eval I/O contract (`{"assertions": [...]}` with canonical names) plus a resolution
state file (entities + aliases + queue + merge log) for the later Postgres load.

```bash
# Deterministic only (no API key) — queues role-refs/initials for confirmation:
python -m canon resolve build/greyharbor.candidates.json --no-llm \
  --state build/greyharbor.resolve.json --out out/assertions.json

# With the LLM disambiguation pass (maps "the deputy"/"C.B." → Cole, etc.):
python -m canon resolve build/greyharbor.candidates.json \
  --state build/greyharbor.resolve.json --out out/assertions.json

# Review the confirm queue, or merge by hand (human-only collision policy):
python -m canon confirm --state build/greyharbor.resolve.json --out out/assertions.json
python -m canon merge   --state build/greyharbor.resolve.json --keep Cole --drop "the deputy" \
  --reason "deputy is Cole" --out out/assertions.json

# Score the resolved assertions against ground truth (PLAN.md step 6):
python eval/run_eval.py --assertions out/assertions.json --findings out/findings.json
```

Next steps in the pipeline (not yet built): load entities/aliases/assertions into
Postgres (the "store" step), then `check` (findings.json) and `ask`.

## Rights guard

This is a public repo and Canon's standing rule is **only rights-clean, original
material, ever** — so the rule is enforced mechanically, not just by policy.
`scripts/rights_guard.py` blocks screenplay containers (.fdx/.pdf/.docx/…)
everywhere, `.fountain` files outside `fixtures/`, screenplay-formatted text in
any non-fixture file, fixtures missing their originality credit line, and API
keys. CI (`.github/workflows/rights-guard.yml`) runs it on the working tree
*and the full git history* on every push. Activate the local pre-commit hook
once per clone:

```bash
git config core.hooksPath scripts/hooks
```

Real scripts you're testing against locally belong in ignored scratch dirs
(`out/`, `build/`) — they can never be committed.

## The one rule

If a proposed feature generates story content, it's out of scope. Forever. See `docs/decisions.md` D1.

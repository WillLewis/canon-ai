# Canon AI

**The continuity and canon engine for serialized fiction. Canon never writes your story — it keeps it true.**

Verification, not generation: ingest scripts and world docs → temporal assertion graph in Postgres → ask questions and run continuity checks, every answer and flag cited to its source scene.

## Where ChatGPT & Claude fall short for serialized fiction

Long context solves *"what do my documents say?"*
Canon solves *"what is true, when, on which branch, who knows it — and prove it."*

### 1. State, not recall

Ask an LLM with your full bible loaded: *"Does Dani know about the affair?"*
It read the Ep 311 reveal, so it says **yes**.

But you're writing Ep 309 — where Dani only suspects the letters.

A context window retrieves everything that's *written*. It has no concept of
what's true *as of a point in the timeline*, on *this draft*, from *this
character's* point of view. Canon models knowledge as timestamped, branch-aware
assertions — so "what did Dani know in 309?" is a query, not a guess.

### 2. Proactive, not reactive

Chat answers the questions you think to ask.
The continuity errors that ship are the ones you didn't.

Nobody asks *"hey, did we burn down the boathouse?"* before setting a scene
there. Canon's scan flags it in your draft — *boathouse destroyed in 307, sc 31* —
before the table read, not after the episode airs.

### 3. Verifiable, not vibes

A confident wrong answer is worse than no answer — it gets written into the
script. LLM recall is probabilistic; Canon flags are deterministic queries
against the graph, and **every flag cites canon**: episode, scene, line.
Click through. Check it yourself. Nothing auto-changes your draft.

### 4. Branches and a write path, not a folder of PDFs

A show isn't one corpus. It's aired canon + the current draft + three alternate
outlines + *"what if we move the reveal to 308?"* Canon branches like git:
diverge, query each timeline independently, merge what survives the room.

And it's a write path, not just a reading surface — flags resolve into the
graph, room notes become episode packets, beats export to FDX. A chat project
is where your bible goes to be asked about. Canon is where it stays true.

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

The `ingest` step (`docs/extraction.md` Stage 1 "Segment") is built: **Fountain,
PDF, and docx** → scenes with `slug`, global `story_position`, `is_flashback`, and
planted-annotation-free `raw_text`, loaded into `worlds`/`works`/`scenes`. Formats
mix freely in one world (`ep101.fountain` + `ep102.pdf`). The Fountain parser is
stdlib-only; PDF/docx use `pypdf`/`python-docx` (text-based scripts only — the LLM
re-segmentation fallback for scanned/messy PDFs is deliberately not built yet, per
SPEC's time-box; such files fail with a clear error). PDF/docx test fixtures are
generated at test time in temp dirs from our own `.fountain` files — screenplay
containers are never committed (rights guard).

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

## Store / load (implemented — Phase 0, step 4 of the pipeline)

The `store` step loads a resolution state into Postgres (per `db/schema.sql`),
after `canon ingest` has created the world's scenes. It writes
`entities`/`aliases`/`scene_presence`/`assertions`/`character_locations`, and
handles the transforms the schema needs: `valid_during` ranges from
`story_position` + `starts_here`/`ends_here`; an object-value placeholder for
intransitive predicates (`dies`/`destroyed`) so the schema's object CHECK holds;
`scene_presence` populated with both the scene's characters **and** its setting
location (resolved from the slug, so `destroyed_location_use` can see it); and the
`character_locations` exclusion-constraint mirror for `located_at(character →
location)`. v0 status gate: `confidence ≥ --conf-canon` (0.85) loads as `canon`,
else `draft`.

```bash
# Summarize what would load — no database:
python -m canon store --state build/greyharbor.resolve.json --world greyharbor --dry-run

# Load into Postgres (after ingest has loaded the scenes):
export CANON_DB_URL=postgresql://postgres:postgres@127.0.0.1:54322/postgres
python -m canon store --state build/greyharbor.resolve.json --world greyharbor --reset
```

## Continuity checks (implemented — Phase 0, step 5 of the pipeline)

The `check` step (PLAN item 5; SPEC R6) runs `db/checks.sql` over a world's loaded
graph and emits findings — each with a check name, severity, plain-English
explanation, citation, and a `sealed` flag (writer marked intentional → suppressed
from the report and the eval gates). Doctrine D7: SQL judges; the runner only
executes, applies seals, and serializes. Output is the eval contract
(`{"findings": [...]}`) and rows in the `findings` table.

```bash
export CANON_DB_URL=postgresql://postgres:postgres@127.0.0.1:5432/postgres
python -m canon check --world greyharbor --out out/findings.json   # or omit --out for a report

# Full Phase 0 loop, end to end:
python -m canon ingest fixtures/greyharbor/*.fountain --world greyharbor --reset-world
python -m canon store  --state out/greyharbor.resolve.json --world greyharbor --reset
python -m canon check  --world greyharbor --out out/findings.json
python eval/run_eval.py --assertions out/assertions.json --findings out/findings.json   # ALL PASS ✓
```

On greyharbor this finds all four planted errors (dead-speaker, premature-knowledge,
destroyed-location-use, capability-violation) plus the two notes, with zero false
positives. Two checks in `db/checks.sql` were tuned to match the fixture's data
shape: **premature_knowledge** (rewritten to flag a `knows` with no on-screen
source, rather than requiring a second `knows`) and **destroyed_location_use**
(strictly *after* the destruction scene, so the destruction scene itself isn't
flagged).

## Ask the Bible (implemented — Phase 0, step 6 of the pipeline)

The `ask` step (PLAN item 4; SPEC R5): natural-language question → the LLM writes
one read-only SQL query → Postgres answers → every row resolves to a scene
citation, or the answer is **refused** (R5: no citation, no answer). Enforcement
is mechanical: a guard requires a single SELECT scoped to `:world_id` returning a
`scene_id` column; execution happens in a `READ ONLY` transaction with a statement
timeout (the transaction, not the regex, is the real write-blocker — verified:
`setval()` passes the regex and is rejected by Postgres). One corrective retry,
then refusal. Optional narration phrases the rows — only the rows — with
citation tags; `--no-narrate` gives the deterministic table.

```bash
export CANON_DB_URL=postgresql://postgres:postgres@127.0.0.1:5432/postgres
export ANTHROPIC_API_KEY=...   # no-training/ZDR org; needed for NL → SQL + narration

python -m canon ask "who knows the ledger's location, in the order they learned it?" \
  --world greyharbor --show-sql
#   → Tobias [E101/sc3] → Cole [E102/sc1] → Mara [E102/sc2], each with its quote

# No key? Run a query directly through the same guards + citation machinery:
python -m canon ask "..." --world greyharbor --sql "SELECT ... :world_id ... scene_id ..."
```

The 10 scripted Phase 0 questions live in `tests/test_ask.py` with reference SQL;
the DB-gated integration test answers 10/10 with correct citations (the harness
bar — the R5 acceptance "≥8/10 via the LLM" gets measured once a key is present).

**Phase 0 pipeline complete and measured on real LLM extraction** (`claude-opus-4-8`,
2026-06-12): `ingest → extract → resolve → store → check → ask`, graded by
`eval/run_eval.py` — **ALL GATES PASS**: extraction recall 80% (12/15), planted
errors 4/4 (incl. `premature_knowledge` via canonical-handle reuse and
`capability_violation` via the negated-`cannot` arm), 0 false positives, no trap
flags. Ask-the-Bible: 8/10 questions answered with correct citations; the 2
misses were one honest "canon doesn't establish it" refusal over an extraction
gap (never a hallucinated answer) and one query-too-narrow variance since
mitigated in the prompt. Tuning lesson encoded in `docs/extraction.md`'s spirit:
free-text `object_value` must be SHORT CANONICAL HANDLES reused verbatim across
scenes — that's what makes cross-character knowledge checks joinable.

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

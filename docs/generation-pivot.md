# Canon AI - Generation Pivot

Source brief: Canon AI Generation Pivot, 2026-07-27. Governing workstream:
`docs/workstreams.md` -> "The generation pivot - waves G0-G4".

New product principle:

> AI output is a proposal. The writer decides what becomes the story.

This document freezes the doctrine and cross-branch contracts for G0-PIVOT. It
does not implement code, migrations, prompts, templates, or copy outside the
explicit G0 doctrine files. G1 and later workstreams consume these shapes.

## 1. Current-State Repository Inventory

Canon is currently a verification-first continuity engine with a shipped local
pipeline, web surface, billing/trust scaffolding, and original fixtures.

- `canon/`, `ingest/`, `extract/`, and `ask/` implement the Phase 0 spine:
  parse owned scripts into scenes, extract cited assertions with Anthropic
  structured outputs, resolve entities, load Postgres, run deterministic SQL
  checks, and answer cited Ask-the-Bible queries.
- `db/schema.sql` and `supabase/migrations/` define the source-of-truth schema:
  worlds, works, scenes, entities, aliases, assertions, `character_locations`,
  scene presence, findings, seals, coverage notes, auth/membership, sharing,
  usage/billing, world rules, and pipeline run ledgers.
- `db/checks.sql` and `db/holes.sql` are deterministic. SQL/rules judge hard
  story-state issues; LLMs may extract, resolve, or phrase but do not decide
  core violations.
- `canon/report.py`, `canon/families.py`, and `docs/readers-report.md` define
  the Reader's Report layer: grounded coverage notes backed by query candidates
  and citation validation.
- `canon/ripple.py` already models deterministic "what breaks if this changes?"
  reports for proposed graph changes; this becomes useful in branch and merge
  workflows.
- `canon/export/` and `canon/trust_delete.py` provide leave-anytime mechanics:
  complete export and hard deletion. Grep tests keep LLM machinery out of
  export and rules paths.
- `ui/` is a FastAPI app with the note surface, script view, ask pane, confirm
  queue, upload/theater flow, auth gates, share views, trust pages, and billing.
  The ingest theater already proves the append-only event ledger plus short-poll
  pattern that generation will clone in new tables.
- `ops/` meters LLM calls, rate limits actions, estimates per-run cost, and
  alerts on COGS or enforcement failures.
- `fixtures/greyharbor/` and `fixtures/federaltrace/` are original, rights-clean
  test worlds. `corpus/` is gitignored operator-only smoke-test material and is
  never demo or eval material.
- `scripts/rights_guard.py` enforces repository rights hygiene: no committed
  script containers, no non-fixture `.fountain`, no screenplay-formatted text
  outside fixtures, fixture originality credit lines, and no API keys.

Known stale prose remains in README, SPEC, DESIGN, docs/architecture,
docs/readers-report, UI templates, UI README, and several tests. G0 only flips
the doctrine files and ADRs requested by `docs/workstreams.md`; G1-RECONCILE
owns the broad copy/test reconciliation.

## 2. Components Unchanged

The pivot preserves the existing canon engine. These components remain
load-bearing and are not rewritten for the slice:

- Supabase Postgres remains the source of truth.
- Postgres range types, GiST indexes, and exclusion constraints remain the
  temporal backbone.
- The existing extraction pipeline remains Anthropic-duck-typed. Multi-provider
  abstraction applies to new generation code only.
- Entity resolution, confidence gates, confirm/reject semantics, and citations
  remain part of ingestion and verification.
- `sealed` remains the writer's STET action for intentional inconsistency. It is
  not repurposed for generation locks.
- Deterministic SQL/rules remain the judge for objective story-state violations.
- Reader's Report candidate families remain grounded in queries, with LLMs used
  for salience/phrasing only where already designed.
- Ask-the-Bible still refuses uncited answers.
- Export/delete completeness remains a test-enforced trust contract.
- Rights hygiene is unchanged: only original, licensed, or rights-clean material
  enters the repo.
- No-LLM grep tests for `canon/export/` and rules paths remain.
- Existing upload/theater pipeline tables and `/api/runs/{id}/events` remain
  frozen and additive-only. Generation receives its own ledger.

## 3. Components Requiring Extension

The first vertical slice requires additive extensions rather than a rewrite:

- Schema: branches, snapshots, artifact versions, generation runs, generation
  candidates, generation run events, generation change sets, locked elements,
  prompt/provider provenance, Story DNA storage, branch dimensions, and a new
  `proposed` assertion status.
- Store/check surfaces: branch filters and status filters must prevent proposed
  AI assertions from leaking into canon, reports, exports, checks on other
  branches, or confirm queue rows.
- Generation provider layer: a new provider-neutral interface with fake,
  Anthropic, and OpenAI adapters.
- Generation skills: versioned, declarative skill definitions instead of one-off
  provider-specific writing buttons.
- Context compiler: a provider-neutral compiler that selects only relevant
  branch, artifact, Story DNA, lock, character, thread, assertion, knowledge,
  adjacent-scene, and format-profile context.
- Branch engine: create/list/name branches, branch-local replacement rows,
  snapshots, restore, selected-object merge/canonization, and branch-scoped
  checks.
- Acceptance loop: accepted candidate text is applied to a branch, snapshotted,
  re-segmented where needed, extracted as `proposed`, tested, and canonized only
  by explicit writer action.
- Studio UI: a sibling shell for Map/Board/Page, AI Composer, candidate rack,
  context manifest, and Story Tests drawer.
- Rights guard policy: generated screenplay-like output must stay in ignored
  runtime output unless converted into original fixtures with the required
  credit line.
- Design tokens: proposal and locked lanes require fresh tokens without
  redefining note blue or sealed/STET wax.

## 4. Superseded Decisions and Instructions

The pivot supersedes D1 by new ADR, not by editing D1 in place. Superseded or
scheduled-for-reconciliation material:

- D1's "Verification, never generation" identity is superseded by D12.
- CLAUDE.md and AGENTS.md "one rule" sections are rewritten in G0.
- CLAUDE.md's Anthropic-only stack line becomes: existing extraction remains
  Anthropic; new generation uses a multi-provider layer.
- README, SPEC, DESIGN, docs/architecture, docs/readers-report, UI README,
  landing/upload/base/billing/terms copy, and old copy-asserting tests contain
  stale no-generation language. G1-RECONCILE updates each atomically with its
  asserting tests.
- Extraction prompts and extractor rules that say not to invent story material
  are not superseded. They remain true for extraction.
- The ToS statement that Canon produces no literary material must change before
  launch, but G0 does not edit legal copy.

## 5. Target Architecture

The target loop is:

Describe -> generate -> edit directly -> branch -> test -> merge -> export.

Generation sits above the canon engine, not beside it:

1. Writer selects an artifact, branch, scope, format profile, and skill.
2. The context compiler builds a `StoryContextBundle` with a manifest of what
   was available and what was included.
3. The generation service invokes a `GenerationProvider` through a versioned
   skill, streams normalized events into `generation_run_events`, and stores
   candidates with full provenance.
4. Each candidate is attached to a candidate branch/snapshot and remains a
   proposal until the writer accepts it.
5. Acceptance applies selected text to a branch-local artifact version, creates
   a snapshot, runs re-segmentation/extraction as needed, stores new assertions
   as `proposed`, runs deterministic checks branch-scoped, and returns Story
   Tests results.
6. Only explicit writer action promotes accepted material into canon on the
   active branch.
7. Export uses branch-specific artifact versions and canonized/proven status
   filters. Runtime generation output never bypasses export/delete contracts.

New code homes are intentionally narrow:

- `generation/` for provider contracts, events, service orchestration, skills,
  validation, prompts, and context.
- `ui/studio_db.py` for Studio reads/writes; do not grow the existing large
  `ui/db.py`.
- `ui/templates/studio_base.html` as a sibling to `base.html`.
- `st-`-prefixed CSS for Studio; existing `style.css` remains owned by the
  current surface until G3.

## 6. Data-Model Changes

G1-SCHEMA owns DDL. G0 freezes shapes and names.

### Phase F Branch Design

The gitignored `ARCHITECTURE_PLAN.md` Phase F design is inlined here so worktree
sessions can use it:

> Two axes: story time (`valid_during`, exists) + revision/canon time. Plus a branch
> model. Build, in order: `branches`, `revisions` per branch, **branch-local replacement
> assertions** (never mutate `story_position` in place — supersede the old branch-local
> row and retire it via revision-time, preserving provenance), query/check scoped by
> branch, finding-diff by branch/revision. **Defer merge/conflict UX** until enough
> branch use shows what conflicts actually look like. **Skip copy-on-write** (premature
> for a tens-of-thousands-of-rows corpus — just `branch_id` + row copies). Branching
> multiplies the state model, so heavy branch features sit *after* #1/#2 are solid.
> Migration is cheap: adding `branch_id` is one additive column + an afternoon of
> `AND branch_id = :branch` on ~7 checks you're already rewriting for fact_id/epistemic.
> **Acceptance test:** fork `main` → `move-reveal-308`; create a branch-local replacement
> for the reveal at the new position; run checks on both; finding-diff shows what the
> move breaks/fixes; `main` untouched.

This pivot extends that model from assertion replacement rows to story-artifact
version rows, because accepted generated text needs a branch-local home before
it can be re-ingested.

### Branches

Shape:

```text
branches
  id uuid
  world_id bigint
  name text
  parent_branch_id uuid nullable
  base_snapshot_id uuid nullable
  status text: active | archived
  is_main boolean
  created_by uuid nullable
  created_at timestamptz
  updated_at timestamptz
```

Contract:

- Branches inherit world membership and RLS through `world_id`.
- The main branch is a real branch row.
- Branch-as-world-clone is rejected.
- Every branch-scoped table stores `branch_id`.

### Revisions and Snapshots

Shape:

```text
revisions
  id uuid
  branch_id uuid
  parent_revision_id uuid nullable
  reason text: manual_edit | generation_completed | acceptance | merge | canonization | restore | ingest
  actor_id uuid nullable
  created_at timestamptz

snapshots
  id uuid
  branch_id uuid
  revision_id uuid
  parent_snapshot_id uuid nullable
  label text nullable
  reason text: explicit_save | generation_completion | acceptance | merge | periodic_checkpoint | restore
  manifest jsonb
  created_by uuid nullable
  created_at timestamptz
```

Contract:

- Snapshot boundaries are practical: explicit save, generation completion,
  acceptance, merge, restore, or periodic checkpoint. Never one snapshot per
  keystroke.
- Restore creates a new revision/snapshot event; it does not erase history.

### Story Artifacts and Artifact Versions

Artifact levels:

```text
project
story_dna
series_or_film
season_or_major_arc
episode_act_or_sequence
scene
beat
script_passage
dialogue_line
character
relationship
story_thread
world_rule
canon_assertion
```

Shape:

```text
story_artifacts
  id uuid
  world_id bigint
  kind text
  parent_artifact_id uuid nullable
  source_work_id bigint nullable
  source_scene_id bigint nullable
  stable_key text nullable
  created_at timestamptz

artifact_versions
  id uuid
  artifact_id uuid
  branch_id uuid
  revision_id uuid
  snapshot_id uuid nullable
  predecessor_version_id uuid nullable
  replaces_version_id uuid nullable
  status text: active | superseded | discarded
  text_content text nullable
  structured_content jsonb
  story_position int nullable
  valid_during int4range nullable
  provenance jsonb
  created_by uuid nullable
  created_at timestamptz
```

Contract:

- Accepted text lives in artifact-version rows before any re-ingestion.
- Branch-local replacement rows supersede prior branch-local rows; they do not
  mutate original rows in place.
- Current artifact state is a branch/revision projection.

### Assertions and Character Locations

Changes:

- Add `proposed` to `assertion_status`.
- Add `branch_id` to branch-scoped assertions and all branch-aware mirrors.
- `character_locations` gets `branch_id` and the exclusion key becomes:

```text
branch_id with =,
character_id with =,
valid_during with &&
```

Contract:

- `proposed` assertions are out of canon, out of confirm queue, out of checks on
  other branches, and out of paid bible/export surfaces unless specifically
  exporting complete raw stored data.
- `sealed` remains intentional inconsistency only.

### Locked Elements

Shape:

```text
locked_elements
  id uuid
  world_id bigint
  branch_id uuid nullable
  artifact_id uuid nullable
  assertion_id bigint nullable
  element_kind text: text | beat | fact | character | relationship | thread | rule | location | production_constraint
  scope text: exact_text | preserve_fact | preserve_presence | preserve_order | preserve_tone | preserve_constraint
  selector jsonb
  reason text nullable
  created_by uuid nullable
  created_at timestamptz
  disabled_at timestamptz nullable
```

Contract:

- Locked or pinned means "generation must preserve this."
- It is visually and semantically separate from sealed/STET.

### Generation Runs, Candidates, and Events

Run shape:

```text
generation_runs
  id uuid
  world_id bigint
  branch_id uuid
  snapshot_id uuid nullable
  user_id uuid nullable
  skill_id text
  skill_version text
  provider text
  model text
  model_profile text
  prompt_version text
  context_hash text
  selected_artifact_id uuid nullable
  status text: queued | running | succeeded | failed | cancelled
  phase text: queued | context_compiling | provider_streaming | candidate_validating | story_tests | persisting | completed
  candidates_requested int
  input_tokens int default 0
  output_tokens int default 0
  cost_usd numeric(10,4) default 0
  retry_count int default 0
  error_class text nullable
  error text nullable
  created_at timestamptz
  started_at timestamptz nullable
  heartbeat_at timestamptz
  completed_at timestamptz nullable
```

Candidate shape:

```text
generation_candidates
  id uuid
  run_id uuid
  branch_id uuid
  snapshot_id uuid nullable
  candidate_branch_id uuid nullable
  status text: streaming | completed | saved | accepted | partially_accepted | discarded | failed
  ordinal int
  generated_text text
  structured_output jsonb
  beat_summary text nullable
  new_facts jsonb
  threads_opened jsonb
  threads_closed jsonb
  estimated_length jsonb
  validation jsonb
  provider_provenance jsonb
  created_at timestamptz
  completed_at timestamptz nullable
```

Event shape:

```text
generation_run_events
  run_id uuid
  seq int
  kind text
  data jsonb
  created_at timestamptz
  primary key (run_id, seq)
```

Run lifecycle contract:

- Statuses: `queued | running | succeeded | failed | cancelled`.
- Phases: `queued | context_compiling | provider_streaming |
  candidate_validating | story_tests | persisting | completed`.
- Event kinds: `generation_started`, `planning`, `text_delta`,
  `structured_delta`, `candidate_completed`, `validation_started`,
  `check_completed`, `generation_completed`, `generation_failed`,
  `cancel_requested`, `cancelled`.
- Cost-cap aborts use `generation_failed` with `error_class = cost_cap`.
- Events are append-only and replayable.

Poll endpoint JSON shape:

```json
{
  "run": {
    "id": "uuid",
    "world_id": 1,
    "branch_id": "uuid",
    "status": "running",
    "phase": "provider_streaming",
    "skill_id": "continue_scene",
    "skill_version": "1",
    "provider": "fake",
    "model_profile": "explore",
    "selected_artifact_id": "uuid",
    "candidates_requested": 3,
    "input_tokens": 0,
    "output_tokens": 0,
    "cost_usd": 0.0,
    "error": null,
    "error_class": null,
    "resumable": false
  },
  "events": [
    {
      "seq": 1,
      "kind": "text_delta",
      "data": {
        "candidate_id": "uuid",
        "delta": "text"
      }
    }
  ],
  "next": 1
}
```

### Generation Change Sets and Proposal Lifecycle

Shape:

```text
generation_change_sets
  id uuid
  candidate_id uuid
  branch_id uuid
  snapshot_id uuid
  status text: proposed | accepted | canonized | rejected
  accepted_scope text nullable
  accepted_ranges jsonb
  artifact_version_ids uuid[]
  assertion_ids bigint[]
  story_tests_result jsonb
  created_by uuid nullable
  created_at timestamptz
  resolved_at timestamptz nullable
```

Lifecycle:

```text
proposed -> accepted -> canonized
proposed -> rejected
```

Distinctions:

- Proposed: AI output or extracted assertions that are reviewable but not canon.
- Accepted: writer chose material for a branch; it is applied and testable.
- Canonized: writer explicitly made accepted material authoritative on the
  branch.
- Rejected/discarded: writer declined the proposal.
- Sealed/STET: writer marks an apparent inconsistency intentional.
- Locked/pinned: writer marks an element generation must preserve.

## 7. UI Architecture

The Studio UI preserves the paper, ink, and editorial language while adding a
generation-native workspace.

Shell:

- Top bar: project, branch, format, mode, history, export.
- Left rail: hierarchical story navigator.
- Center: Map, Board, or Page artifact. Page view is the first slice; Map/Board
  can be stubs.
- Right rail: collapsible AI Composer.
- Bottom drawer: Story Tests and generation activity.

Selection contract:

- Every visible story object has a stable artifact id, branch id, and scope.
- Composer actions are contextual to the selected object.
- The selected object, branch, locks, Story DNA, and relevant canon state are
  passed structurally through `StoryContextBundle`, not pasted into a generic
  chat prompt.

Candidate rack:

- Streams three candidate continuations through short-poll ledger events.
- Shows generated text, beat summary, new facts introduced, threads opened or
  closed, estimated length, locked-constraint results, and deterministic
  continuity results.
- Supports accept full candidate, accept selected passage, save, discard, and
  request variations of one candidate.

Story Tests drawer:

- Red is hard Canon failure only.
- Graphite is metadata/advisory.
- Advisory creative evaluators, if later added, are visually and architecturally
  separate from deterministic Canon tests.

Semantic color lanes:

- Black ink: user-authored, accepted, or canonized material.
- Proposal blue `#2F6F9F`: streamed/unaccepted AI proposal.
- Red pencil: Canon caught a conflict or failed hard constraint.
- Graphite `#6F675A`: neutral metadata and advisory information.
- Locked/pinned umber `#6B5338`: constraints generation must preserve.
- Existing note blue `#41708F`: coverage-note/non-photo pencil lane, unchanged.
- Existing sealed/STET wax `#5C4632`: intentional inconsistency lane, unchanged.
- Faded paper: discarded or inactive alternatives.

## 8. Generation-Skill Architecture

Generation skills are versioned definitions consumed by the generation service
and provider layer. A skill definition has exactly these 15 contract fields:

```text
id
name
applicable_formats
allowed_artifact_scopes
required_inputs
optional_controls
context_policy
prompt_template_and_version
structured_output_schema
validation_rules
default_model_profile
returns
creates
ui_affordance
telemetry_fields
```

Field constraints:

- `returns`: `prose | structured | both`.
- `creates`: `proposal | candidate_branch | direct_transformation`.
- `default_model_profile`: `fast | best | explore | critic | structured`.
- `applicable_formats`: any of `television`, `film`, `microdrama`.
- `allowed_artifact_scopes`: any artifact levels listed in section 6.
- `context_policy`: declares required context groups, optional context groups,
  token budget, stable-prefix ordering, and omission rules.

Initial slice skills:

- `continue_scene`
- `generate_alternate_continuations`
- `generate_scene_from_beats`
- `rewrite_selection`

Later examples parked by the brief:

- `generate_beat_plan`
- `generate_episode_outline`
- `generate_microdrama_cliffhanger`

Variation dimension control for `generate_alternate_continuations`:

```text
plot | character_choice | reveal | tone | conflict | cliffhanger | surprise | production_cost
```

Provider protocol:

```python
class GenerationProvider(Protocol):
    async def generate(
        self,
        request: GenerationRequest,
    ) -> AsyncIterator[GenerationEvent]:
        ...
```

`GenerationRequest` shape:

```text
id uuid
world_id bigint
branch_id uuid
snapshot_id uuid nullable
skill_id text
skill_version text
model_profile text
selected_artifact_id uuid
context_bundle StoryContextBundle
candidates_requested int
controls jsonb
user_instruction text nullable
timeout_seconds int nullable
```

`GenerationEvent` shape:

```text
kind text
run_id uuid
candidate_id uuid nullable
seq int nullable
data jsonb
created_at timestamptz nullable
```

Event vocabulary:

- `generation_started`
- `planning`
- `text_delta`
- `structured_delta`
- `candidate_completed`
- `validation_started`
- `check_completed`
- `generation_completed`
- `generation_failed`
- `cancel_requested`
- `cancelled`

Provider adapters:

- Deterministic fake provider first; CI never requires live keys.
- Anthropic adapter.
- OpenAI adapter.

Adapters normalize streaming, structured outputs, cancellation, token/cost
reporting, prompt caching where available, timeouts, retries, and error classes.
Domain logic uses model profiles, not provider model ids.

## 9. Migration and Backward-Compatibility Risks

Highest-risk changes are schema and status filters:

- `assertion_status ADD VALUE proposed` is irreversible in normal Postgres enum
  flows. The name is frozen here.
- Every status filter that currently excludes `retconned`, `sealed`, or
  `rejected` must be audited for `proposed`.
- `character_locations` constraint drop/re-add must backfill main branch and
  keep loader skip logic aligned.
- `scenes unique(work_id, story_position)` must either widen for branch or be
  shadowed by replacement-row semantics.
- `canon/store.py` currently performs full-world teardown on reset; G2-BRANCH
  must make this branch-scoped/status-preserving before acceptance/re-ingestion
  is safe.
- Export/delete registries must include every new world/user-scoped table.
- RLS should inherit through `world_id`; avoid duplicate branch access systems.
- The existing `pipeline_runs` theater contract is frozen. Generation uses new
  tables and endpoint shape, not changes to existing run tables.
- Tests that assert old no-generation identity must be updated atomically with
  G1-RECONCILE copy changes.
- Rights guard RULE 3 can flag committed generated screenplay-like text outside
  fixtures. Fake-provider samples must account for that.

## 10. Privacy, Rights, and API-Key Handling

Rules that survive the pivot:

- API keys are server-side only.
- Do not expose provider keys to the browser.
- Do not commit user material.
- Do not place real scripts in fixtures.
- Continue using original Greyharbor and Federal Trace material for tests/demos.
- Preserve export-everything and delete-everything.
- Do not send more story context than a generation request requires.
- Do not send raw screenplay text to product analytics.
- Record generation provenance without unnecessary sensitive content.
- Document actual provider data-retention posture; do not claim unsupported
  zero-retention behavior.
- Preserve and adapt the rights guard rather than removing it.

Generated text policy:

- Runtime generated screenplay-like output belongs in ignored `out/` or the
  database.
- Committed generated fixtures must be original fixture material under
  `fixtures/` and carry the required credit line.
- No third-party source material may be ingested, fetched, pasted into fixtures,
  or persisted.

## 11. Phased Roadmap

The roadmap is exactly `docs/workstreams.md` waves G0-G4.

- G0-PIVOT: this pivot document, doctrine flip, contracts, ADRs, and CLAUDE.md /
  AGENTS.md rewrite.
- G0-CI: pytest/eval safety net and existing billing-test noise cleanup.
- G1-SCHEMA: all generation DDL, proposed status, branch dimension, registries,
  RLS, tests, and schema mirror.
- G1-PROVIDER: multi-provider layer, deterministic fake, Anthropic/OpenAI
  adapters, cost/token/error normalization.
- G1-SKILLS: versioned skill registry and the four slice skills.
- G1-CONTEXT: StoryContextBundle compiler and manifest.
- G1-RECONCILE: broad docs/copy/design/test reconciliation and rights-guard
  adaptation docs/tests.
- G2-BRANCH: branch/snapshot/candidate state layer, branch-scoped store/check
  behavior, restore, and deterministic Fountain writer leaf as scheduled.
- G3-GENSERVICE: service orchestration, event ledger, poll endpoint,
  cancellation, provider streaming, and generalized spawn harness.
- G3-ACCEPT: acceptance, re-ingestion, proposed assertions, Story Tests, and
  explicit canonization.
- G3-STUDIO: Studio shell, candidate rack, Composer, Story Tests drawer, and
  branch-aware slice UI.
- G4-SLICE-QA: complete the 16-test matrix, local setup docs, gate, and
  completion report.
- G4-ADVISORY: optional advisory creative evaluators, parkable without harming
  the slice.

## 12. Non-Goals for the First Vertical Slice

Do not build in the first slice:

- Full-season autonomous generation.
- Image generation.
- Video generation.
- Table-read audio.
- Real-time multiplayer.
- Billing changes.
- Enterprise tenancy.
- FDX editing.
- Mobile applications.
- Fine-tuning.
- Named-author style imitation.
- Prompt marketplace.
- Multi-agent writers' room.
- Automatic global propagation.
- Automatic promotion of AI output into canon.
- New SSE infrastructure.
- Broad agent orchestration.
- Rewrites of stable extraction/report/ask/check components.

## 13. Open Decisions

### StoryContextBundle Shape

```text
StoryContextBundle
  bundle_id uuid
  world_id bigint
  branch_id uuid
  snapshot_id uuid nullable
  selected_artifact artifact_ref
  story_position int nullable
  story_dna story_dna_ref
  format_profile format_profile_ref
  locked_elements locked_element_summary[]
  characters character_card[]
  relationships relationship_summary[]
  story_threads thread_summary[]
  canon_assertions assertion_summary[]
  character_knowledge_as_of knowledge_summary[]
  adjacent_scenes scene_excerpt[]
  parent_plan structured_artifact nullable
  child_beats structured_artifact[]
  user_instruction text nullable
  controls jsonb
  prompt_version text
  token_budget jsonb
  manifest ContextManifest
```

Manifest counts line:

```text
{included_facts} of {available_facts} facts | {locked_beats} locked beats |
{adjacent_scenes} adjacent scenes | {character_cards} character cards |
Story DNA version {story_dna_version} | Prompt version {prompt_version}
```

Minimum manifest fields:

```text
facts_available
facts_included
locked_beats
locked_elements
adjacent_scenes
character_cards
story_dna_version
prompt_version
token_budget
estimated_tokens
omitted_context_reasons
context_hash
```

### Acceptance API and Story Tests Shape

Acceptance endpoints:

```text
POST /api/generation/candidates/{candidate_id}/accept
POST /api/generation/candidates/{candidate_id}/accept-passage
POST /api/branches/{branch_id}/restore
```

Accept-full request:

```json
{
  "target_branch_id": "uuid",
  "create_branch": true,
  "branch_name": "Alternate continuation",
  "snapshot_label": "Before generated continuation",
  "canonize": false
}
```

Accept-passage request:

```json
{
  "target_branch_id": "uuid",
  "candidate_ranges": [
    {"start": 0, "end": 120}
  ],
  "target_artifact_id": "uuid",
  "insert_after": {"artifact_id": "uuid", "offset": 320},
  "snapshot_label": "Before partial acceptance",
  "canonize": false
}
```

Acceptance response:

```json
{
  "change_set_id": "uuid",
  "branch_id": "uuid",
  "snapshot_id": "uuid",
  "artifact_version_ids": ["uuid"],
  "proposal_status": "accepted",
  "story_tests": {}
}
```

Story Tests result:

```json
{
  "branch_id": "uuid",
  "snapshot_id": "uuid",
  "change_set_id": "uuid",
  "status": "passed",
  "summary": {
    "checks_run": 7,
    "failures": 0,
    "warnings": 0,
    "new_findings": 0,
    "resolved_findings": 0,
    "locked_constraints_passed": 4,
    "locked_constraints_failed": 0
  },
  "findings": [
    {
      "check_name": "premature_knowledge",
      "severity": "warning",
      "explanation": "text",
      "scene_id": 1,
      "assertion_a": 10,
      "assertion_b": null,
      "sealed": false,
      "delta": "new"
    }
  ],
  "locked_constraints": [
    {
      "locked_element_id": "uuid",
      "status": "passed",
      "explanation": "preserved"
    }
  ],
  "proposed_assertions": {
    "created": 0,
    "canonized": 0,
    "rejected": 0
  },
  "citations": [
    {
      "scene_id": 1,
      "quote": "text"
    }
  ]
}
```

`status`: `passed | failed | warning | cancelled | error`.

### Story DNA Shape and Format Profiles

Story DNA shape:

```text
story_dna
  id uuid
  world_id bigint
  branch_id uuid nullable
  version int
  format text: television | film | microdrama
  runtime_or_page_target text nullable
  genre text nullable
  subgenre text nullable
  tone text[]
  intended_audience text nullable
  premise text nullable
  story_engine text nullable
  themes_and_dramatic_questions text[]
  character_principles text[]
  dialogue_characteristics text[]
  visual_language text[]
  content_boundaries text[]
  production_constraints text[]
  structural_preferences text[]
  things_to_avoid text[]
  canonical_terminology jsonb
  user_authored_style_samples jsonb
  creative_latitude_preference text: conservative | balanced | expansive
  created_by uuid nullable
  created_at timestamptz
```

Craft attributes are composable descriptors, not named-author imitation
presets. Examples: economical dialogue, high subtext, propulsive scene endings,
naturalistic interruptions, heightened melodrama, minimal exposition, visually
driven storytelling, bleak humor.

Format profiles are defaults and optional guidance, not mandatory grammars.

Television profile fields:

```text
series_engine
season_arc
episode_grid
pilot
a_b_c_stories
teaser_or_cold_open
acts
act_outs
tag
episode_outline
scene_generation
character_reveal_tracking
season_reveal_tracking
```

Film profile fields:

```text
premise
logline
synopsis
treatment
act_or_sequence_architecture
beat_sheet
scene_list
opening_sequence
midpoint
climax
alternate_ending
screenplay_pages
```

Microdrama profile fields:

```text
vertical_native_premise
episode_ladder
high_frequency_hooks
short_episode_runtime
compressed_dialogue
frequent_reversals
episode_cliffhangers
small_cast_constraints
location_constraints
mobile_oriented_visual_storytelling
multi_episode_batch_planning
```

### G0 decisions — RATIFIED 2026-07-27 (owner sign-off)

All nine formerly-open decisions were ruled on by the owner on 2026-07-27. G0-PIVOT
transcribes them into docs/generation-pivot.md §13; no re-litigation without a new ADR.

1. **Branch representation: `branch_id` + branch-local replacement rows.** No world
   clones. *Implementation notes:* branches inherit world RLS (canon_has_role via
   world_id — no new access resolvers needed); `scenes unique(work_id, story_position)`
   must widen to include the branch dimension (or shadow via replacement-row semantics)
   in the same migration.
2. **Proposal status: new `proposed` enum value.** `draft` is not reused — the confirm
   queue lists every `draft` row and stays uncontaminated. Enum `ADD VALUE` is
   irreversible; the G1-SCHEMA status-filter audit (grep ground truth) is mandatory.
3. **Streaming transport: `generation_run_events` ledger + short poll**, batched
   `text_delta` events every ~250–500 ms per candidate. No SSE in the slice. (Worst-case
   row volume ≈ 700–800 rows per 3-candidate run — fine for an append-only ledger, and
   reload-replay comes free.)
4. **Generation-run lifecycle:** statuses `queued | running | succeeded | failed |
   cancelled`; phases `queued | context_compiling | provider_streaming |
   candidate_validating | story_tests | persisting | completed`; event kinds = the nine
   registry provider events + `cancel_requested` + `cancelled`. *Notes:* naming
   deliberately diverges from pipeline_runs (`done`/`aborted`) — separate table,
   separate contract, and the theater contract stays frozen; mid-run cost-cap aborts
   map to `generation_failed` with a `cost_cap` error class, not a new event kind.
5. **`character_locations` under branches: `branch_id` joins the GiST exclusion key**
   (`branch_id with =, character_id with =, valid_during with &&`). Non-main writes are
   not hard-failed. *Implementation notes:* drop/re-add of the existing constraint +
   main-branch backfill of a `NOT NULL branch_id`, all inside G1-SCHEMA's migration;
   `canon/store.py`'s exclusion-conflict skip logic references this constraint and must
   track the change.
6. **Semantic color lanes: fresh tokens, no redefinition.** Proposal blue `#2F6F9F`
   (streamed/unaccepted AI text); locked/pinned umber `#6B5338`; graphite remains
   neutral metadata/advisory; existing note `#41708F` and sealed/STET `#5C4632` keep
   their meanings untouched. *Note for the G1-RECONCILE design pass:* umber (#6B5338)
   and STET wax (#5C4632) are perceptually close — different contexts (pinned chips vs
   seal stamps), but verify distinguishability + contrast when DESIGN.md is updated, and
   extend `test_design_system.py` selector rules for both new tokens.
7. **Rights guard: preserved unchanged.** Generated screenplay-like runtime output stays
   in gitignored `out/`, unless it is an original fixture under `fixtures/` carrying the
   "Credit: Original fixture material" line. *Trap to engineer around:* RULE 3 also
   fires on ≥3 column-0 sluglines inside committed `tests/*.py` string literals — fake
   provider fixtures must indent, stay under threshold, or live under `fixtures/`.
8. **No-LLM grep tests: both kept.** Generation code never lives under `canon/export/`
   or the rules paths (`canon/rules*.py`, `canon/rules_store.py`, `ui/rules_ui.py`). The
   deterministic Fountain writer may live in `canon/export/` because it is LLM-free.
9. **ToS §4** ("Canon produces no literary material"): **legal-review launch blocker,
   not a build blocker.** G0 records that it must change; nobody edits terms copy until
   the legal pass — and then only atomically with tests/test_trust.py:648, via
   G1-RECONCILE or the legal track.

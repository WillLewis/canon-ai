# Canon AI — Workstreams & Merge Order

How the phases in PLAN.md decompose into **parallel workstreams**. Operator reality: one
human + multiple Claude Code / Codex sessions. "Parallel" means parallel branches/worktrees;
the scarce resource is the operator's review bandwidth, so merges must be small, ordered,
and never fight over the same files.

## Working agreement (all phases)

- **One workstream = one branch = one agent session.** Worktrees for true parallelism.
  Branch naming: `ws/<id>-<slug>` (e.g. `ws/p0-extraction`).
- **Contracts merge before consumers.** A change to a contract (scene JSON, assertion
  schema in docs/extraction.md, DB DDL, eval I/O) lands in its own small PR *first*; then
  dependent branches rebase. Never smuggle a contract change inside a feature branch.
- **The migration lane is serialized.** `db/schema.sql` + supabase migrations are the #1
  conflict source. At most ONE branch with in-flight migrations at any time; every other
  workstream builds against the last merged schema. Queue migration needs; don't fork them.
- **Main is always green:** `eval/run_eval.py` passes after every merge (Phase 0 exit bars
  are the CI gate). A merge that drops recall or adds false positives reverts, no debate.
- **Merge order ≠ ship order.** Feature flags let incomplete features merge early and often.
  Long-lived branches are how a solo repo dies.

## Contract registry (what makes parallel work possible)

| Contract | Lives in | Producer | Consumers |
|---|---|---|---|
| Scene record (id, slug, story_position, is_flashback, text) | docs/architecture.md | Ingest | Extraction, UI |
| Candidate assertion JSON | docs/extraction.md | Extraction | Loader, eval |
| Assertion row DDL / statuses | db/schema.sql | Schema lane | checks, ask, report engine, UI |
| Check finding shape (severity, explanation, citation, sealed) | db/checks.sql | Checks | report renderer, UI |
| `coverage_notes` row + note phrasing contract | docs/readers-report.md | Report engine | UI, renderer |
| Eval I/O | eval/run_eval.py | Eval | everyone |
| Read API for the surface (worlds, scenes, notes, findings) | to be written at P3 kickoff | Backend | Web surface |

---

## Phase 0 — five workstreams

All five can run concurrently today; the contracts above already exist as specs.

| ID | Workstream | Scope | Files | Depends on |
|---|---|---|---|---|
| P0-SCHEMA | Schema & checks | schema.sql applies clean, exclusion constraints, checks.sql, loader | `db/`, loader module | — (owns migration lane) |
| P0-INGEST | Ingestion | Fountain/PDF/docx → scene records | `ingest/` | scene contract only |
| P0-EXTRACT | Extraction & resolution | per-scene LLM pass, alias table, confirm queue CLI | `extract/`, prompts | assertion contract; can develop against hand-made scene JSON before P0-INGEST lands |
| P0-EVAL | Eval harness & fixtures | run_eval.py scoring, answer-key diff | `eval/`, `fixtures/` | consumes all contracts; build early — it gates all tuning |
| P0-ASK | Ask CLI | NL → SQL → cited answer | `ask/` | merged schema; prototype on a seeded fixture DB |

**Phase 0 merge order:** `P0-SCHEMA → P0-INGEST → P0-EXTRACT → P0-EVAL → P0-ASK`, then the
tuning loop (prompt/check changes) iterates on main with eval as the gate. Rationale:
everything reads the schema; eval can't score until the pipeline emits; ask is a leaf.

---

> **Sequencing note (2026-07-02, PLAN.md):** execution order is Phase 0 → Phase 3 (build &
> launch) → Phase 1 (market validation, post-launch) → Phase 2. Phase numbers are stable
> IDs, not order. Workstream IDs keep their historical prefixes.

## Build phase, Wave 0 — report engine (directly after Phase 0 exit)

| ID | Workstream | Scope | Depends on |
|---|---|---|---|
| P1-REPORT | Report engine v0 | families **F2 → F1 → F3 → F4** (build order per docs/readers-report.md), citation validator, `coverage_notes` migration, CLI renderer | Phase 0 exit; takes the migration lane |

The former P1-CONCIERGE stream is dissolved: bible-assembly helpers move into
P3-ARTIFACTS; per-section engagement logging (seal/dismiss/addressed/forwards) moves into
P3-OPS as launch analytics. Test A discovery calls are zero-code operator time and may run
in parallel with any wave. The Phase 0 tuning loop stays open on main throughout the
build, gated by eval — now against both graded fixtures once the spec FBI script lands.

---

## Build waves 1–4 (the "Phase 3" scope) — twelve workstreams

The waves are the merge order. Within a wave, streams are independent (disjoint files, no
shared migrations) and can land in any order.

### Wave 1 — no dependency on identity (P3-FDX and P3-CONTENT can start at Phase 0 exit; P3-ENGINE waits for P1-REPORT)

| ID | Workstream | Scope | Notes |
|---|---|---|---|
| P3-ENGINE | Report engine hardening | retcon ripple queries, per-world check-family toggles (backend), performance | extends P1-REPORT; first in migration lane |
| P3-FDX | FDX import | .fdx parser → scene contract | pure ingest; leaf |
| P3-CONTENT | Demo worlds | original spec FBI script + answer key; verify display rights on the purchased FBI script; load as fixtures | no code; broadens eval genre coverage |

### Wave 2 — the identity chokepoint (merges alone, everything after rebases on it)

| ID | Workstream | Scope | Notes |
|---|---|---|---|
| P3-IDENTITY | Auth, tenancy, multi-user | Supabase auth (email/Google), RLS, worlds, roles (owner/editor/viewer), attributed seal/dismiss | **The** blocking merge: billing, sharing, confirm-queue, rate limits, and the surface session all hang off it. Takes the migration lane for its whole wave — keep it as thin as possible and land it early. |

### Wave 3 — user-scoped product (parallel after identity)

| ID | Workstream | Scope | Depends on |
|---|---|---|---|
| P3-SURFACE | Web app / note surface | split view, anchored notes, triage keys, ask pane, draft-2 diff, ingest theater. Grows from `ui/triage-workbench` — land that branch as the internal read-only tool *before* Phase 3 so this stream doesn't diverge from it | read API contract + P3-IDENTITY; UI develops against greyharbor fixtures in the meantime |
| P3-ARTIFACTS | Bible export, PDF, share link | R7 renderer; share link needs auth | P3-IDENTITY, P3-ENGINE |
| P3-CONFIRM | Confirm-queue UX | "verify your canon" checklist | P3-SURFACE shell, P3-IDENTITY |
| P3-OPS | Ops floor | per-account rate limits, per-report COGS metering, alerting, abuse guards | P3-IDENTITY (limits are per-account) |

### Wave 4 — monetization & launch gate (last because they gate, not because they're big)

| ID | Workstream | Scope | Depends on |
|---|---|---|---|
| P3-BILLING | Stripe + plan gating | free/paid split per PLAN.md Phase 3 §8 | P3-IDENTITY; gates features from waves 1–3, so it merges after they exist behind flags |
| P3-RULES | Rule builder v1 | structured rules over the closed predicate vocabulary → SQL checks; UI in the surface | P3-ENGINE (toggles first), P3-SURFACE; migration lane |
| P3-TRUST | Trust mechanics | export-everything, delete-everything, ToS/no-training warranty on the pricing page | P3-IDENTITY; ToS is a human/legal track that runs parallel from wave 1 |

### Wave 5 — schema sync & integration (after Wave 4, before the launch gate)

| ID | Workstream | Scope | Notes |
|---|---|---|---|
| P3-SCHEMA-SYNC | Schema mirror sync + deferred DDL | (1) Land the DDL queued in MIGRATIONS-NEEDED.md (e.g. `assertions.confirmed_by/confirmed_at`) as one migration and switch the confirm queue's attribution from usage_events audit rows to the real columns. (2) Regenerate `db/schema.sql` (the plain-Postgres mirror) so it matches the full supabase/migrations/ chain — it has drifted since the identity migration. (3) Verify the whole chain applies clean on a fresh `supabase start`. | Takes the migration lane — nothing else with DDL in flight |
| P3-WIRING | Deferred cross-boundary wiring | The one-liners each wave deferred at its boundary: `ops.metering.meter` on the extract/llm.py call site; `ops.middleware.rate_limited` on surface + ask routes, with `billing.store.make_tier_resolver` as its tier hook; `billing.gating.plan_gate` on the paid features (bible export, retcon ripple, rule-authoring writes); HTTP routes for share links (canon/export/share.py → ui/); report findings loaded through `canon.rules.compose_findings` so writer rules + exceptions apply. | Small; can ride with P3-SCHEMA-SYNC or land beside it |

### Wave 6 — the front door (design ratified 2026-07-03; DESIGN.md is law)

| ID | Workstream | Scope | Notes |
|---|---|---|---|
| P3-POLISH | One design system everywhere | "The Continuity Desk" (DESIGN.md): token rewrite of ui/static/style.css, self-hosted fonts (Fraunces/Instrument Sans/Courier Prime), writer-first nav, all 22 templates reskinned, seal → STET language | Lands FIRST — establishes the tokens FRONTDOOR consumes |
| P3-FRONTDOOR | Landing, upload, ingest theater, demo | canon/runner.py (pipeline orchestration + check cycles), ui/jobs.py (upload gate chain, poll endpoint, resume), theater page, anonymous landing poster, /demo/report | Takes the migration lane (pipeline_runs + pipeline_run_events) |

### Build-phase merge order, flattened

```
Phase 0 exit ─► P1-REPORT ─► P3-ENGINE ─┐
               P3-FDX ──────────────────┼─► P3-IDENTITY ─► { P3-SURFACE · P3-ARTIFACTS · P3-CONFIRM · P3-OPS } ─► { P3-BILLING · P3-RULES · P3-TRUST } ─► { P3-SCHEMA-SYNC · P3-WIRING } ─► launch gate
               P3-CONTENT ──────────────┘
```

**Launch gate:** billing works end-to-end on a fresh account · trust endpoints verified
(export + delete actually export and delete) · demo world renders a full Reader's Report
logged-out · eval green · ops alarms firing in staging.

## Why this order (the three chokepoints)

1. **The migration lane.** Postgres DDL is global state; parallel migrations are merge
   hell. Order of lane occupancy: P0-SCHEMA → P1-REPORT → P3-ENGINE → P3-IDENTITY →
   P3-RULES/P3-BILLING. Everything else is code-only and free to parallelize.
2. **Identity.** Every user-scoped feature rebases on auth/RLS. Merging it mid-wave-3 would
   force four simultaneous rebases; merging it early and thin costs nothing.
3. **Engine before surface.** The UI renders rows the engine produces. The surface stream
   never blocks (it builds against fixtures), but it must not *merge* ahead of the engine
   that makes its data real.

---

# The generation pivot — waves G0–G4 (added 2026-07-27)

The 2026-07-27 pivot brief re-identifies Canon as a **generation-first studio**: writers
generate, branch, and rewrite against the canon engine, which becomes the story-state
backend and automated test harness beneath generation. The new principle — **"AI output is
a proposal. The writer decides what becomes the story."** — supersedes the old
"verification, never generation" identity (ADR D1) via a new superseding decision record,
not an in-place edit. Full brief: `docs/generation-pivot.md` (authored in G0-PIVOT).

These waves assume all build waves 0–6 above are merged (they are) and the migration lane
is **empty** (MIGRATIONS-NEEDED.md queue is clear as of `20260703110000_pipeline_runs.sql`).

**Target: the first vertical slice.** A writer opens a Greyharbor-based project, selects a
scene end, streams three alternate continuations, compares them with per-candidate
deterministic test results, accepts one (whole or selected passages) into a new branch;
the accepted branch re-ingests, new assertions stay draft/proposed until explicitly
canonized, the writer can restore the pre-generation snapshot, and the branch exports as
valid Fountain. Both provider adapters (OpenAI + Anthropic) plus a deterministic fake
satisfy one contract; all tests pass with no live API calls. Nothing beyond the slice's
needs gets built (scope fence below).

## Standing constraints carried into the pivot

- All the working agreements at the top of this file still bind: one workstream = one
  branch = one session, contracts merge before consumers, **one** in-flight-migration
  branch, main always green, feature flags over long-lived branches.
- **Don't rewrite stable pipeline components.** The existing Anthropic-duck-typed
  extraction path (extract/resolve/report/ask + CostGuard) is load-bearing in five
  modules and stays as-is. Multi-provider abstraction applies to *new* generation code
  only (`generation/` package).
- **The theater contract is frozen.** `pipeline_run_events` kinds and the
  `/api/runs/{id}/events` shape that theater.js polls are additive-only. Generation gets
  its **own** ledger tables and poll endpoint.
- **No-LLM grep tests stay.** `tests/test_export_cli.py` and `tests/test_rules.py` ban LLM
  machinery in `canon/export/` and the rules paths. Generation code never lands there
  (a deterministic Fountain writer in `canon/export/` is fine — it's LLM-free).
- **Identity copy and its tests move atomically.** At least six tests grep-assert the old
  covenant strings (test_landing 106/112/128, test_trust 648, test_holes 68,
  test_wiring 405, test_extract 108). A branch changing copy updates its tests in the
  same PR.
- **Rights hygiene is unchanged** (ADR D4): original/licensed fixtures only, no
  third-party text, rights guard preserved and *adapted* — never removed.

## New contract registry rows (every SHAPE is frozen in G0-PIVOT before any consumer branch opens; the named streams implement)

| Contract | Lives in | Implemented by | Consumers |
|---|---|---|---|
| GenerationProvider protocol + event vocabulary (generation_started, planning, text_delta, structured_delta, candidate_completed, validation_started, check_completed, generation_completed, generation_failed) | docs/generation-pivot.md → `generation/providers/base.py` | G1-PROVIDER | G1-SKILLS, G3-GENSERVICE, G3-STUDIO |
| Versioned skill definition (id, name, formats, artifact scopes, inputs, controls, context policy, prompt template + version, output schema, validation, model profile, returns prose / structured artifacts / both, creates proposal / candidate branch / direct transformation, UI affordance, telemetry) | docs/generation-pivot.md | G1-SKILLS | G3-GENSERVICE, G3-STUDIO |
| StoryContextBundle + context manifest (counts line: facts included/available, locked beats, adjacent scenes, character cards, Story DNA version, prompt version) | docs/generation-pivot.md | G1-CONTEXT | G3-GENSERVICE, G3-STUDIO |
| Branch / snapshot / candidate / artifact-version / locked-element DDL + proposal lifecycle (proposed → accepted → canonized \| rejected) | supabase migration + db/schema.sql | G1-SCHEMA | G2-BRANCH, all of G3, checks |
| Generation-run lifecycle: run statuses (**including cancelled** — pipeline_runs has no such state, and a status CHECK constraint is DDL), phases, ledger event kinds, poll-endpoint JSON shape. Frozen in G0 precisely so the DDL can land in G1 while the service ships in G3 without reopening the lane | docs/generation-pivot.md | G1-SCHEMA (DDL) + G3-GENSERVICE (service) | G3-STUDIO composer |
| Acceptance API + Story Tests result shape (apply/accept-passage/restore calls; the branch-scoped findings diff the drawer renders) | docs/generation-pivot.md | G2-BRANCH (state ops) + G3-ACCEPT (verification loop) | G3-STUDIO |
| Semantic color + type lanes for generation. **Collision note (G0 must resolve):** the brief wants non-photo blue = AI proposal and wax brown = locked/pinned/STET, but DESIGN.md already assigns #41708F to *note*-severity pencils and #5C4632 to *sealed/STET* — either mint fresh tokens for proposal + locked or explicitly redefine the note/sealed lanes. Graphite = neutral metadata/advisory joins the ratified set (it must not wait for optional G4-ADVISORY). Red stays caught-only; black ink = user/accepted; faded = discarded. `test_design_system.py` selector rules follow the decision | DESIGN.md + tests/test_design_system.py allowed selectors | G1-RECONCILE, G3-STUDIO | all UI |
| Story DNA object (full field list frozen in G0 — G1-SCHEMA lays down its storage and G1-CONTEXT compiles it, so siblings must not be guessing at the shape) + format profiles (television / film / microdrama — defaults, not grammars) | docs/generation-pivot.md (shape) + G1-SCHEMA (storage) | G1-SKILLS | G1-CONTEXT, G3-STUDIO |

### Wave G0 — doctrine & contracts (merges alone, before any parallel session spawns)

| ID | Workstream | Scope | Notes |
|---|---|---|---|
| G0-PIVOT | Pivot doc, doctrine flip, contracts | `docs/generation-pivot.md` (the brief's 13 sections: inventory, unchanged/extended components, superseded decisions, target architecture, data-model changes, UI architecture, skill architecture, migration risks, privacy/rights, roadmap, slice non-goals, open decisions); **inline the ARCHITECTURE_PLAN.md Phase F branch design into the pivot doc** (branches, revisions, branch-local replacement rows — ARCHITECTURE_PLAN.md is gitignored, so worktree sessions can't read it); new ADRs — D12 generation-first (supersedes D1), D13 multi-provider model layer, D14 branch model, D15 proposal lifecycle & locked-vs-sealed-vs-canonized; rewrite the "one rule" sections of CLAUDE.md **and** AGENTS.md (currently drifted duplicates — reconcile or retire one); freeze every contract shape above (run-lifecycle vocabulary, Story DNA field list, color-lane resolution, `character_locations` GiST-key decision — all lane-bound or sibling-shared, none may drift to a later wave) | **Must land first and alone.** Every agent session loads CLAUDE.md and is currently instructed to refuse generation work; parallel branches opened before this merge will fight the pivot. |
| G0-CI | Test safety net | GitHub workflow running pytest (suite is ~3s, fully offline) + the eval gate; fix the two billing tests that are **already failing on main** (fixed `NOW=2026-07-03` seeds vs real clock — detonated 2026-07-15; "main is always green" is false today, and every pivot branch will see these failures as noise until this lands); convert DB-gated early-return skips to real `pytest.skip` | Code-only, disjoint from G0-PIVOT — runs in parallel with it. Today the **only** CI is the rights guard; the pivot's parallel branches need an automated merge gate before they exist. |

### Wave G1 — foundations (all five in parallel after G0)

| ID | Workstream | Scope | Depends on |
|---|---|---|---|
| G1-SCHEMA | Generation DDL | One migration (or tight queued sequence): `branches`, `snapshots`, **branch-scoped story-artifact/document-version storage** (the Phase F replacement-row design — accepted text must live somewhere, and it is DDL, so it lands here, not in G2), `generation_runs` + `generation_candidates` + `generation_run_events` (pipeline_runs shape — heartbeat, jsonb payload, append-only ledger, member-read RLS, service-role writes — with the status/phase/event CHECKs implementing the **G0-frozen run lifecycle, cancelled state included**), **generation change sets** + acceptance/rejection events, `locked_elements` (world_rules shape), prompt-version + provider-provenance columns, Story DNA storage, the branch dimension per D14, the `character_locations` GiST-key outcome from G0 (branch_id in the key, or a write-guard), and the `proposed` assertion status **with an audit of every status filter** — ground truth is `grep -rn "retconned"` across `*.py`/`*.sql`, not this list, but at minimum: checks.sql ×7, holes.sql ×4, report.py, ripple.py, rules.py `_MUTED`, canon/ask.py **and** ask/engine.py `_STATUS_EXCLUDE`, ui/db.py, **canon/export/bible.py:102** (a leaked `proposed` row ships AI text in the paid bible), bench/failure_modes.py, tests/test_ask.py; registries (`full_export.py`, `trust_delete.py` — test-enforced), RLS resolvers, db/schema.sql regeneration (also fix its stale "Derived from" header); **the migration ships with tests** (fresh-apply + RLS resolver coverage) and is reversible where practical | G0 decisions (D14/D15, lifecycle, GiST key). **Takes the migration lane for the whole wave.** Enum `ADD VALUE` is irreversible — names are decided in G0, not here. |
| G1-PROVIDER | Multi-provider layer | New `generation/providers/`: base protocol + normalized events; **deterministic fake provider first** (CI never needs keys); Anthropic + OpenAI adapters; streaming, **structured outputs**, cancellation, token/cost reporting, **prompt caching (adapter side — the compiler's stable-prefix ordering is the other half)**, timeouts, retries, provider-error normalization; extend `ops/metering.py` PRICING + usage normalization for OpenAI models (today unknown models meter $0 as `:unpriced` — a silent COGS hole); model profiles (fast/best/explore/critic/structured) via env/DB config, no model IDs in domain logic; API keys server-side only; document each provider's real data-retention posture (no unsupported "zero retention" claims) | G0 protocol contract. Pure new code — no migrations, no edits to the existing pipeline's call sites. |
| G1-SKILLS | Skill framework + slice skills | Versioned skill registry per the G0 contract; the four slice skills — `continue_scene`, `generate_alternate_continuations` (**with the optional variation-dimension control: plot / character choice / reveal / tone / conflict / cliffhanger / surprise / production cost**), `generate_scene_from_beats`, `rewrite_selection`; prompt templates + structured-output schemas + validation rules; format profiles (tv/film/microdrama); Story DNA object + a seeded default for greyharbor; craft-attribute style controls (no named-author imitation) | G0 contracts; rebases onto G1-PROVIDER's fake provider as soon as it merges (build against the protocol until then). |
| G1-CONTEXT | Context compiler | Provider-neutral pure function over `canon/report.load_world_snapshot` + a story-position/branch filter: selected artifact, Story DNA, locked elements, relevant characters/relationships/threads/assertions, **character knowledge as-of the selected position**, adjacent scenes, **parent plan / child beats for the selected artifact** (generate_scene_from_beats needs it), format profile, user instruction, explicit controls → `StoryContextBundle` + manifest counts; token budgeting (never the full corpus); stable-prefix ordering so provider prompt caching works. Ready-made ingredients: `ripple.ASSERTION_SELECT`, `extract.build_synopsis`, bible per-entity dossiers, `load_scene_open_questions` | G0 contract. No DDL; builds against fixture DB. Branch filter activates when G1-SCHEMA merges. |
| G1-RECONCILE | Doc & copy reconciliation | The full pass G0 doesn't cover: README (its branching pitch finally becomes true), SPEC positioning/non-goals/stories, DESIGN voice + refusal-hero replacement (+ the color-lane decision from G0 written into DESIGN.md), docs/architecture.md (+ the generate → propose → accept → re-ingest loop), docs/readers-report.md positioning section, ui/README boundary section, landing/upload/base/billing/terms copy — **each copy change lands atomically with its asserting test**; rights-guard adaptation: define the policy for AI-generated screenplay text (RULE 3 can't distinguish it from third-party text), add the guard's first unit tests; ops/metering docs. **PLAN.md + ARCHITECTURE_PLAN.md restructuring rides with this stream but is an operator task in the main checkout** — both are gitignored and invisible in worktrees | G0 doctrine. Docs/copy only — no structural collisions (Studio uses its own shell, see G3). terms.html §4 ("no literary material") is a **legal-review** item: launch-blocking, not build-blocking. |

### Wave G2 — the branch engine (G2-BRANCH is the structural chokepoint and merges alone; G2-FOUNTAIN is an unrelated leaf parked here by name only)

| ID | Workstream | Scope | Depends on |
|---|---|---|---|
| G2-BRANCH | Branch/snapshot/candidate state layer | Create/list/name branches; snapshots at practical boundaries (explicit save, generation completion, acceptance, merge, periodic checkpoint — never per-keystroke); apply accepted text to a branch; **end the wipe-and-reload collision**: `canon/store.py`'s `_delete_world_graph` full-world teardown becomes branch-scoped/status-preserving (today any re-ingest destroys confirm-queue rulings and settled statuses — the deepest DB-layer change the pivot forces); branch-local story-position allocation (the dense axis renumbers on insert today); restore any snapshot; traceable change events for every generation/edit/acceptance/merge/canonization; branch-scoping of checks.sql/holes.sql/ripple/rules query surfaces | G1-SCHEMA. **Owns `canon/store.py` + the SQL check files — merges alone; no checks-tuning branch in flight.** The design is the Phase F branch model (branches, revisions, branch-local replacement rows) **as inlined into docs/generation-pivot.md by G0-PIVOT** — the original lives in gitignored ARCHITECTURE_PLAN.md, which a worktree session cannot see. |
| G2-FOUNTAIN | Fountain writer | Deterministic scene-records → valid `.fountain` export; branch parameter once G2-BRANCH lands; runtime output only to gitignored `out/` (rights guard RULE 3 blocks slugline text elsewhere) | Leaf. LLM-free, so it may live in `canon/export/` without breaking the no-LLM grep tests. Can start any time after G0 against main-line worlds. |

### Wave G3 — the slice (parallel after G2-BRANCH)

| ID | Workstream | Scope | Depends on |
|---|---|---|---|
| G3-GENSERVICE | Generation service | Skill invocation → context compile → provider stream → candidate rows with full provenance (provider, model, prompt version, context hash, branch, selected artifact, latency, input/output tokens, estimated cost, status, retry count, error class — never API keys, never raw scripts to analytics) → append `generation_run_events` + poll endpoint in **its own router module + its own `include_router` line in ui/app.py** (own ledger; the theater contract stays frozen); the jobs.py refusal-matrix gate chain **in the real docstring order: auth → tier → rate limit → concurrency → page/size caps → COGS estimate** with new action kinds; cancellation (new capability — no run today can be cancelled, only die or finish); **streamed text lands as batched `text_delta` events (~250–500 ms per candidate) against a ~1 s poll** — user-visible streaming as chunked deltas, not per-token rows; **owns the spawn call site for accept-triggered re-ingest too** (one generalized harness, two run kinds) | G1-PROVIDER, G1-SKILLS, G1-CONTEXT, G1-SCHEMA, G2-BRANCH for branch targets. Generalizes the `ui/jobs.py` spawn/heartbeat/resume harness rather than duplicating it. |
| G3-STUDIO | Studio surface | `studio_base.html` shell as a **sibling** of base.html (topbar: project/branch/format/**mode (Map · Board · Page)**/history/export · left rail story navigator · center **Page view** for the slice, Map/Board as stubs · right collapsible AI Composer rail · bottom Story Tests drawer); candidate rack: three concurrently streaming candidates (ledger short-poll on the theater.js pattern), per-candidate beat summary, new facts, threads opened/closed, est. length, locked-constraint + deterministic check results; context-manifest strip; accept full candidate / accept selected passage / **request variations of one candidate** / discard / save; restore; own router module + one `include_router` line in ui/app.py (per convention — the two one-line app.py insertions, STUDIO's and GENSERVICE's, are this wave's only sanctioned shared-file touch); own `ui/studio_db.py` read module (**don't** grow the 924-line ui/db.py); `st-`prefixed page CSS (style.css untouched) | Read/write API contracts from G3-GENSERVICE + G2-BRANCH, **and the G0-frozen acceptance/Story-Tests contract that G3-ACCEPT implements** (the drawer and accept buttons are stubbed against the contract until ACCEPT merges); builds against fixtures + fake provider from day one, **merges last in the wave**. Proposal color per the G0 lane decision; `test_design_system.py`'s route walk sweeps every new page. Keep `/worlds/{world_id}/...` path shape so `auth.py` works unchanged. |
| G3-ACCEPT | Acceptance → verification loop | Accept → G2 apply + snapshot → re-segmentation where required → the **existing** extraction path (Anthropic duck-type kept) → new assertions land branch-scoped as `proposed` — out of canon, out of the confirm queue, invisible to checks on other branches → deterministic checks run branch-scoped → results + findings diff in the G0-frozen Story Tests shape → **only explicit writer action canonizes**. Locked/pinned elements enforced (locked text never changes); `sealed` is not overloaded — sealed = intentional inconsistency, locked = preserve during generation, canonized = authoritative on the branch | G2-BRANCH, G1-SCHEMA. Owns the `canon/runner.py` extensions; the run is **spawned through G3-GENSERVICE's generalized harness** (ACCEPT owns the pipeline logic, GENSERVICE owns the spawn seam — that contract edge, not file overlap, is the coupling). |

### Wave G4 — slice hardening & completion

| ID | Workstream | Scope | Depends on |
|---|---|---|---|
| G4-SLICE-QA | The 16 required tests + the gate | Most tests land inside their streams; this closes the matrix: provider-adapter normalization, fake-provider streaming, context selection + token budget, locked text never changing, candidate-branch isolation, main branch unchanged, acceptance, partial acceptance, snapshot restore, proposals staying out of canon, accepted-text extraction flow, checks after acceptance, provider failure + cancellation, prompt-version persistence, rights guard post-pivot, Fountain export — **no test may require live credentials**; run the slice acceptance-criteria checklist; README/local-setup update; completion report (files changed, migrations added, tests added, commands run, known limitations, next recommended slice) | Everything above. |
| G4-ADVISORY *(optional — parkable to the backlog without harming the slice)* | Advisory creative evaluators | Dialogue explicitness, scene energy, repetitive beats, tonal fit, candidate similarity — labeled advisory/subjective/model-generated, visually and architecturally separate from deterministic Canon tests (graphite, never red); **no hidden critic silently filters unconventional candidates** — for alternates, diversity is a feature | G3-GENSERVICE, G3-STUDIO. |

### Pivot merge order, flattened

```
G0-PIVOT ──► ┌ G1-SCHEMA ────────► G2-BRANCH ─► { G3-GENSERVICE · G3-ACCEPT } ─► G3-STUDIO ─► G4-SLICE-QA (─► G4-ADVISORY?)
(G0-CI ∥)    ├ G1-PROVIDER ──┐                        ▲                            (branch opens at wave start;
             ├ G1-SKILLS ────┼────────────────────────┘                             merges last in the wave)
             ├ G1-CONTEXT ───┘  (code streams feed G3 directly)
             ├ G1-RECONCILE  (docs/copy; independent of the code lanes)
             └ G2-FOUNTAIN   (leaf; any time after G0, branch param after G2-BRANCH)
```

Migration-lane occupancy continues the historical order: … → P3-FRONTDOOR → **G1-SCHEMA**.
No later pivot wave carries DDL — that only holds because G0 freezes every DDL-shaped
decision up front (run lifecycle incl. cancelled, `character_locations` GiST key,
artifact-version storage, enum names); queue any straggler in MIGRATIONS-NEEDED.md.

### Why this order (the pivot's chokepoints)

1. **Doctrine before parallelism.** CLAUDE.md/AGENTS.md currently instruct every session
   to refuse generation work, and identity strings are test-asserted across the tree. One
   small G0 commit flips the doctrine and lands the contracts; only then do parallel
   worktrees make sense.
2. **The migration lane, again.** The pivot wants at least four DDL-bearing concerns —
   they are ONE lane occupant (G1-SCHEMA), with registries, RLS, and the schema mirror
   riding in the same PR. Enum choices are irreversible, so they're decided in G0.
3. **store.py before everything that touches state.** The wipe-and-reload loader is the
   single biggest structural collision (it re-mints assertion ids, breaking seals and any
   pinned UI reference, and destroys rulings on every re-ingest). One stream (G2-BRANCH)
   owns it, alone.
4. **Ledger over SSE.** No streaming machinery exists anywhere; the event-ledger +
   short-poll pattern is proven, replayable on reload, and matches the daemon-thread
   architecture. Generation clones it with new tables — but note the precedent is
   phase-granularity events, not token deltas: the slice satisfies "streaming for
   user-visible generation" with **batched `text_delta` events (~250–500 ms per
   candidate) against a ~1 s poll**, and first-SSE-in-the-repo stays a separate decision
   for later, not a slice dependency.
5. **New code in new homes.** `generation/` package + `ui/studio_db.py` + `studio_base.html`
   + `st-`prefixed CSS keep the no-LLM grep tests green, ui/db.py stable, and the 24
   base.html-extending templates undisturbed — which is what lets G1-RECONCILE, G3-STUDIO,
   and the existing surface coexist as parallel branches.

### Slice scope fence (from the brief — park, don't build)

Full-season autonomous generation · image/video generation · table-read audio · real-time
multiplayer · billing changes · enterprise tenancy · FDX editing · mobile · fine-tuning ·
named-author style imitation · prompt marketplace · multi-agent writers' room · automatic
global propagation · automatic promotion of AI output into canon.

### Open decisions (owner sign-off in G0)

Every one of these is lane-bound or sibling-shared, so **none may stay open past the
G0-PIVOT merge**; outcomes are recorded in docs/generation-pivot.md §13.

- **Branch representation:** `branch_id` + branch-local replacement rows (the Phase F
  design, inlined into the pivot doc — recommended) vs branch-as-world-clone (breaks
  membership/sharing, duplicates entities, multiplies owner rows).
- **Proposal status:** new `proposed` enum value (recommended — keeps the confirm queue,
  which lists every `draft` row, uncontaminated) vs reusing `draft`.
- **Streaming transport:** generation ledger + short poll with batched `text_delta`
  events, ~250–500 ms cadence (recommended — proven pattern, replay for free) vs the
  repo's first SSE.
- **Generation-run lifecycle vocabulary** — statuses (incl. `cancelled`), phases, event
  kinds, poll shape: frozen here because G1-SCHEMA casts it as CHECK-constraint DDL two
  waves before G3-GENSERVICE ships the service.
- **character_locations exclusion constraint under branches** — add branch_id to the GiST
  key, or hard-fail cross-branch writes into the canonical world. DDL-shaped, so the
  outcome lands inside G1-SCHEMA's migration.
- **Semantic color lanes** — resolve the collisions: non-photo blue is today's
  note-severity pencil, wax brown (#5C4632) is today's sealed/STET; proposal and
  locked/pinned/STET lanes need either fresh tokens or an explicit DESIGN.md
  redefinition (graphite = advisory/metadata ratifies now, not with optional
  G4-ADVISORY).
- **Rights-guard policy for AI-generated screenplay text** (RULE 3's slugline heuristic
  cannot distinguish generated from pasted third-party text; committed fixtures for the
  fake provider must live under `fixtures/` with the credit line or stay in `out/`).
- **Fate of the no-LLM grep tests** (recommended: keep both; generation code simply never
  lives in those paths).
- **ToS §4 rewrite** ("Canon produces no literary material") — legal review; blocks
  launch, not the build.

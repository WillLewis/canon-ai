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
| P3-WIRING | Deferred cross-boundary wiring | The one-liners each wave deferred at its boundary: `ops.metering.meter` on the extract/llm.py call site; `ops.middleware.rate_limited` on surface + ask routes; HTTP routes for share links (canon/export/share.py → ui/). | Small; can ride with P3-SCHEMA-SYNC or land beside it |

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

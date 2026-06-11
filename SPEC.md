# Canon AI — Product Spec (v0.1)

## Problem statement

Writers of serialized fiction must hold an ever-growing canon — who knows what, who's alive, what's been established, what's been promised — in their heads or in chaotic docs/spreadsheets. Continuity errors are objectively wrong, publicly embarrassing (fans catch them in hours), and expensive to fix late. The pain scales with corpus size and production velocity: a 41-episode TV show, a 100-episode vertical drama written at 20 pages/day, a novelist restructuring a manuscript into a series bible. General LLMs drift past ~10–20k tokens of canon and cannot enforce state; existing "story bible" features (Sudowrite, NovelCrafter) are prompt-injection wikis, not engines. Nothing on the market verifies.

## Positioning (load-bearing)

**Canon never writes your story.** It is a verification, retrieval, and structuring engine. The writer is the only generator. Every output cites its source. This is simultaneously the trust wedge with AI-wary writers, the contractual safety line (no "literary material" produced), and the differentiation from every generation-first competitor.

## Target users

- **v0 (this spec):** writers working on **material they fully own** — authors adapting their books, web-series/microdrama writers, working writers' development slates, aspiring writers with original pilots.
- **Two modes, corpus-dependent hero feature:**
  - **Build mode** (thin corpus — a pilot + world doc): structure chaos into a professional bible; interrogate the world and surface holes before an exec does.
  - **Verify mode** (thick corpus — seasons of material): continuity scan, knowledge-state checks, setup/payoff tracking.
- **Future (out of v0 scope):** writers' rooms / studios (script-coordinator cockpit), enterprise single-tenant.

## Goals

1. **Catch real errors:** on a multi-episode corpus, find genuine continuity violations a careful human reader would confirm, with ≤2 false positives per episode.
2. **Answer with receipts:** "Ask the Bible" returns correct, scene-cited answers to ≥80% of canon questions in test sets.
3. **Prove willingness to pay:** ≥3 of 5 concierge bible customers pay $99–199 and call the output pitch-grade (Phase 1, Test B).
4. **Prove retention shape:** ≥2 of 5 dev-slate writers return unprompted in week 2 (Phase 1, Test C).
5. **Validate or kill the microdrama wedge** with 10 discovery calls (Phase 1, Test A).

## Non-goals (v0)

- **No story generation of any kind** — not a goal at any version; see Positioning.
- **No writers'-room / enterprise product** — requires show-level data rights, security certifications (SOC 2, TPN), and a sales motion a solo side project can't run. Deferred until a prosumer wedge wins.
- **No Final Draft sidecar / editor integration** — only matters for the staffed-TV segment; premature.
- **No branching, canon-time vs. story-time bitemporality, or multi-writer governance** — v3 architecture; v0 is single branch, single writer, story-position axis only.
- **No agents-as-product (review panel, Audience Memory)** — requires pgvector + UI; the underlying checks ship first as a CLI/report.

## User stories (priority order)

1. As an **author adapting my own novel**, I want to upload my manuscript and receive a structured series bible (characters, relationships, timeline, world rules, open questions) with page citations, so I can attach a professional document when shopping the adaptation.
2. As a **writer with a pilot in development**, I want to ask my own world questions in plain language ("what does Mara know about the fire, and when did she learn it?") and get cited answers, so I can draft without rereading everything.
3. As a **writer with a pilot in development**, I want a report of questions my world doc doesn't answer, so I can fix holes before a pitch meeting surfaces them.
4. As a **vertical-drama writer producing 20 pages/day**, I want a continuity scan of my new episode against the prior 60, so errors are caught before shooting, not by commenters.
5. As a **writer reviewing flags**, I want to mark a flag "intentional" (sealed) so deliberate mysteries stop being reported — and stay sealed in future scans.
6. As a **writer**, I want certainty that my uploaded material is never used to train models and can be fully exported/deleted, so I can trust the tool with unproduced work.

## Requirements

### P0 — Phase 0 cannot exit without these
- **R1 Ingestion:** Fountain, PDF, docx → scene-segmented text, stable scene IDs, integer story positions. *AC: greyharbor fixtures ingest losslessly; scene count matches answer key.*
- **R2 Extraction:** LLM structured-output pass → assertions per `docs/extraction.md` schema (entity, predicate, object, story-position range, citation, confidence). *AC: ≥80% recall vs. answer key.*
- **R3 Entity resolution v0:** alias table + disambiguation; assertions below confidence threshold route to a human-confirm queue. *AC: "MARCUS" / "Marcus Hale" / "her husband" resolve to one entity in fixtures.*
- **R4 Storage:** Postgres schema with `int4range` validity, GiST indexes, exclusion-constraint invariants, `status ∈ {draft, canon, sealed, retconned}`. *AC: schema.sql applies clean on `supabase start`; presence-overlap insert is rejected by the constraint.*
- **R5 Ask the Bible (CLI):** NL question → SQL → answer + citations; refuses to answer without a citation. *AC: 8/10 test questions correct with correct citations.*
- **R6 Continuity checks (CLI):** dead-speaker, presence-conflict, premature-knowledge, destroyed-location-use, dangling-reference — each finding carries severity, plain-English explanation, citation, and respects `sealed`. *AC: all planted greyharbor errors found; ≤2 false positives/episode.*

### P1 — fast follows once a wedge wins
- **R7 Build-mode bible export:** canon graph → formatted series-bible document with citations (the Test B deliverable, productized).
- **R8 Hole-finder report:** unanswered-question generation over the graph (gaps in rules, unresolved knowledge states, dangling setups).
- **R9 Minimal web app:** Supabase auth + RLS, upload, report viewing; one writer = one world.
- **R10 Setup/payoff tracker:** open hooks and idle threads with age ("Maya's debt — unresolved since 104").

### P2 — architectural insurance (design for, don't build)
- Agent review panel (the checks, re-skinned as cited findings feed) + Audience Memory via pgvector.
- Canon-time vs. story-time bitemporality; branches; multi-writer merge.
- FDX sidecar; show-level enterprise deploy; provenance/rights-ledger layer.

## Success metrics

- **Leading:** Phase 0 exit-criteria pass; Test B conversion (5 paid at $99–199) and cleanup-hours trend; Test C week-2 unprompted return rate; Test A go/kill signal tally.
- **Lagging (post-convergence):** MRR from the chosen wedge; % of scans producing a finding the writer acts on; sealed-rate (proxy for false-positive trust); count of "can I run this on my show?" bridge signals.

## Open questions

- **(Product)** What's the minimum bible format managers/producers consider professional? → resolve via Test B feedback.
- **(Engineering)** Best PDF script-parsing path for non-Fountain sources — layout-aware parse vs. LLM re-segmentation? → resolve in Phase 0 week 1; time-box 2 evenings.
- **(Engineering)** Partial-order story time: integer positions suffice for v0, but flashbacks need explicit handling — annotate scenes as flashback with their own diegetic position? → decide when a fixture or real corpus breaks the integer axis.
- **(Market)** Does microdrama continuity pain exist at all? → Test A. Blocking for that wedge only.
- **(Legal, non-blocking)** At productization, confirm ToS language for user-IP custody, no-training warranty, and deletion guarantees.

## Timeline

No hard deadlines. Phasing per PLAN.md: Phase 0 weeks 1–3, Phase 1 weeks 3–8, convergence weeks 9–12. The only date-like commitment: if Phase 0 exit criteria aren't met by end of week 5 (calendar slack included), trigger the rethink protocol in PLAN.md rather than extending silently.

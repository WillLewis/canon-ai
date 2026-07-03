# Canon AI — Decision Log

Read before proposing architecture or scope changes. Each entry: decision, rationale, revisit-trigger.

---

**D1 — Verification, never generation.**
Canon indexes, retrieves, checks, explains; it never writes story content (loglines, beats, dialogue, pitches). Rationale: trust position with AI-wary writers post-2023 WGA; contractual safety (no "literary material"); differentiation — frontier LLMs win generation, nobody owns verification. Even AI-friendly buyers (microdrama platforms) draw the line at AI-generated concepts. Revisit: never. This is identity, not strategy.

**D2 — Supabase Postgres; no Neo4j; no SQLite.**
The assertion table *is* a graph (edge list); storage engine ≠ data model. Postgres wins specifically on: native range types + GiST for interval algebra (the hard part of our model, and the part graph DBs are weakest at); **exclusion constraints** that turn continuity invariants into schema; text-to-SQL ≫ text-to-Cypher reliability (Ask the Bible is an LLM writing queries); pgvector adjacency for later; Supabase auth/RLS/storage = solo-founder backend-in-a-box. Graph-shaped queries run on an in-memory networkx projection. SQLite was the original spike choice but loses ranges, exclusion constraints, and the week-5 multi-user path; starting on Postgres avoids a pointless migration. Revisit Neo4j only if: a hero feature needs interactive variable-length pathfinding beyond the projection (unlikely at tens-of-thousands-of-rows scale), or enterprise multi-writer concurrent graph editing arrives.

**D3 — Three wedge tests before product; one shared build.**
Wedges: (A) microdrama studios — discovery calls only; (B) author→bible concierge — paid from day one; (C) dev-slate writers — white-glove, watch retention. Rationale: solo capacity; two of three tests are barely code; the Phase 0 pipeline is the shared demo asset. Pre-committed convergence rule in PLAN.md. Revisit: after Phase 2 scoring.

**D4 — Rights hygiene is absolute; the spec-writer segment is dead as designed.**
Never ingest material the user doesn't own. The "spec an existing show" segment required indexing unlicensed scripts — brand-fatal for a company whose enterprise thesis is rights-clean provenance. Salvaged form: aspiring writers on **original** pilots (Build mode). Revisit: only via licensed content partnerships, never via gray-market ingestion.

**D5 — Two modes, corpus-dependent hero feature.**
Thin corpus → Build mode (structure a bible, find holes); thick corpus → Verify mode (continuity scan). Same graph, two front doors. Rationale: a thin-corpus user hitting an empty continuity report churns on day one. Revisit: post-convergence, the winning wedge's mode leads the product.

**D6 — Story-time only in v0; bitemporality deferred.**
Single integer story-position axis; flashbacks annotated, possibly excluded from interval checks; retcons recorded as status, not reasoned over. Rationale: bitemporal branching is v3 complexity with v0 corpora that don't need it. Schema keeps provenance fields so the upgrade is additive. Revisit: when a real corpus (prequel material, heavy nonlinear structure) breaks the integer axis.

**D7 — Deterministic judging; LLM at the edges.**
LLM extracts assertions and narrates findings; SQL/rules produce the findings. Rationale: drift-free, auditable, citable — the product's whole premise. Revisit: never for core checks; LLM-judged "soft" critiques (tone, similarity) may exist later but must be visibly labeled as non-cited opinion (and that contrast is itself a feature).

**D8 — Sealed/intentional is a first-class status from day one.**
Writers want apparent contradictions (mysteries, unreliable narrators). Every check respects `sealed`; sealing is permanent, per-assertion-pair, and provenance-logged. Rationale: over-flagging is the #1 trust killer; sealing data is also a proprietary signal corpus. Revisit: n/a.

**D9 — Enterprise everything is parked.**
SOC 2, TPN, single-tenant, FDX sidecar, writers'-room cockpit, rights-ledger/licensing rail: parked with explicit triggers (see PLAN.md table). Rationale: solo side project; the canon engine must exist before the rights ledger means anything. Revisit: per-trigger.

**D10 — Free-text fact handles are short, canonical, and reused verbatim; checks join on normalized handles.**
`object_value` is a 2–5-word canonical handle ("ledger location", "drive", "find Danny"), never a sentence — detail goes in `notes`. The same fact carries the identical handle across scenes AND characters: extraction copies handles forward from the rolling synopsis, and `object_fact_ref` collapses into `object_value` in v0 (both are free-text handles; assertion-id epistemic refs are post-v0). Checks join on a normalized form (lowercase, alphanumerics only) — deterministic SQL, never embeddings or LLM-judged similarity at check time (D7). Rationale: measured on greyharbor (2026-06-12) — with sentence-length values, cross-character joins starve and recall graded 33% with the signature `premature_knowledge` check structurally unable to fire; with canonical handles the same pipeline passes every Phase 0 gate. Handle wording still varies run-to-run; the confirm queue is the designed catch for residual drift. Revisit: if real corpora show handle drift breaking cross-character joins, add a deterministic canonicalization pass (alias table for facts, confirm-queue promotion) — not similarity scoring inside checks.

**D11 — PDF/docx ingestion is layout-first; LLM re-segmentation is an explicit fallback.**
For P0-INGEST, Fountain is parsed natively. PDF/docx ingestion first uses deterministic text/layout extraction (`pypdf`, `python-docx`) and screenplay heading rules. If a text-based PDF/docx yields no scene boundaries, the ingester can attempt Anthropic re-segmentation only when explicitly enabled with a caller-supplied model; it never silently sends writer material to an API. Scanned/OCR-only PDFs fail clearly rather than pretending to ingest. Rationale: the two-evening time-box favored the boring parser for clean script exports, while preserving a controlled escape hatch for messy layout without expanding Phase 0 into document forensics. Revisit: if more than one owned pilot in Phase 1 fails layout parsing, add OCR or a richer layout parser before broadening LLM fallback.

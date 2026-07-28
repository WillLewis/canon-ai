# corpus/ — owned-copy scripts for internal smoke testing

Script files in this directory are **gitignored by design** (`.gitignore` blocks all
script container formats; `scripts/rights_guard.py` enforces this in pre-commit and CI).
Only this README is committed. Files here exist on the operator's machine only and do
**not** appear in worktrees — reference them by the absolute canonical path below.

## Inventory

### FBI-2018-Pilot_.pdf
- **Canonical path (worktrees, use this):**
  `/Users/WL/Documents/Documents - William’s MacBook Pro (4)/GitHub/canon-ai/corpus/FBI-2018-Pilot_.pdf`
- **Provenance:** purchased copy; copy ownership only — **underlying rights NOT held.**
- **Public-display rights:** none assumed. This is a produced show's pilot; treat as
  **permanently internal**.
- **Allowed uses:** local PDF-ingestion smoke tests, extraction/pipeline tuning on the
  operator's machine, performance testing on a real-world corpus.
- **Forbidden uses:** committing to git (enforced) · demo worlds or any user-visible
  surface · fixtures/ or answer-key grading · marketing material · persisting its text or
  derived assertions into any shared/staging/production database. Local scratch DBs only;
  wipe after use.

## Rules for agents

1. Nothing in corpus/ is ever eval-graded — graded material lives in `fixtures/` with
   answer keys and is original to this repo.
2. If a corpus file is missing in your worktree, that is expected — use the canonical
   path above; do not ask for the file to be committed and do not copy it into the repo
   tree under another name or format.
3. Adding a new corpus file = add an inventory entry here first (provenance, rights,
   allowed/forbidden uses). No entry, no use.

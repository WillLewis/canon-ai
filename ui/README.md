# Canon AI — Triage Workbench + Note Surface (`ui/`)

Two skins on one FastAPI app and one database:

- the internal, read-only **state inspector** (a microscope for the engine), and
- the writer-facing **note surface** (P3-SURFACE): the Reader's Report view, the
  script-first split view, the ask pane, and the draft-2 diff — the same rows the
  engine wrote, rendered writer-grade.

It honors Canon's one rule: **it never generates story content.** No dialogue, no
loglines, no summaries — it only indexes, displays, and records the writer's
rulings. There are no LLM calls anywhere in `ui/`: the report engine
(`canon/report.py`) writes `coverage_notes` rows offline; this app renders them.
The writes it performs: seal/unseal a finding, and the two **permanent** note
transitions (seal / dismiss).

## Run it

```bash
pip install -r ui/requirements.txt          # fastapi, uvicorn, jinja2 (psycopg is already a root dep)

# Point at your database (defaults to the local dev URL below if unset):
export CANON_DB_URL=postgresql://postgres:postgres@127.0.0.1:5432/postgres

# Make sure findings exist for the world you want to inspect:
python -m canon check --world greyharbor_s1

python -m ui.app                             # → http://127.0.0.1:8000
#   or: uvicorn ui.app:app --reload
```

Run it from the repo root (so `canon` and `ui` are importable). Override the bind
with `CANON_UI_HOST` / `CANON_UI_PORT`. If the database is unreachable or empty,
the app says so with a hint instead of a stack trace.

## What you can do

- **Overview** — per-world dashboard: entity/assertion/scene counts and findings
  by severity. The world picker (top right) switches between loaded worlds.
- **Entities** — filter by kind, search name/alias; each entity page shows its
  dossier, aliases, and every assertion where it's the subject or the object.
- **Assertions** — filter by subject / predicate / status, full-text search across
  names, object values, and quotes. Each row carries its `valid_during` span
  (glossed to plain story positions), status, confidence, and a citation chip.
- **Scenes** — ordered by global story position, grouped by work; each scene page
  shows who/what is present, the findings located there, and the raw text.
- **Findings** — output of `db/checks.sql`, grouped critical → warning → note, with
  a separate **Sealed** section. Each finding shows its explanation, cited scene,
  and linked assertion(s).
- **Citation inspector** — click any assertion (or "open full scene") and its
  establishing scene renders with the verbatim supporting quote **highlighted**.
  On a finding page, the cited scene highlights the anchor assertion's quote when
  that quote lives in that scene.
- **Seal / unseal** — on a finding page, seal a flag a writer marked intentional
  (with an optional reason) or unseal it. See the next section for the semantics.

## The note surface (`/worlds/{id}/…`)

- **Reader's Report** (`/worlds/{id}/report`) — sections in spec order
  (docs/readers-report.md): continuity findings, load-bearing canon, note
  families F1–F4 from `coverage_notes`, corpus appendix. Every note renders its
  summary collapsed and its evidence + citations on expand; family caps and the
  ≤ 2-page ethos are enforced in the view. Sealed/dismissed notes live in a
  collapsed **Settled** footer, permanently — nothing in this app can re-open one.
- **Per-note actions**, no modals: **Seal** (intentional), **Dismiss** (wrong) —
  both POSTs gate on the editor role and stamp `status_changed_by/at`;
  **Show me** jumps to the cited scene in the script view with the stored quote
  highlighted; **Ask** pre-fills the ask pane scoped to the note's entities.
- **Keyboard triage**: `j`/`k` next/prev note, `enter` expand, `s` seal,
  `d` dismiss — a full report is triageable start-to-finish on the keyboard.
- **Script view** (`/worlds/{id}/script`) — the writer's text on the left,
  scene-navigable; findings + notes anchored to the scene in view on the right
  (PR-review model); gutter markers where notes anchor; clicking a citation
  highlights and scrolls to the cited quote (`?scene=&quote=` is the server-side
  fallback; `static/surface.js` upgrades it in-page).
- **Ask pane** — docked on both views; `POST /worlds/{id}/ask` calls
  `ask/engine.py` (SQL templates, no LLM) and renders the cited answer, or the
  refusal verbatim. Every citation links into the script view.
- **Draft-2 diff** (`/worlds/{id}/report/diff`) — "N notes resolved (addressed)
  · M new · K sealed" computed from `coverage_notes` run/status fields;
  `addressed` is set by the engine when a later run finds the gap closed.
- **Auth**: read views are open; viewers see action buttons disabled; editors+
  can seal/dismiss (`ui/auth.py` `require_role('editor')`, world resolved from
  the path id). `AUTH_DISABLED=1` keeps the single-user local flow working.

## How sealing works (and why it's faithful)

Sealing is **DB-native**: it writes the existing `seals` table on the exact key the
checker reads — `(world_id, check_name, assertion_a, coalesce(assertion_b, 0))`,
the same tuple `canon/check.py` uses to mark a finding suppressed. So a seal made
here is the real thing: re-running `python -m canon check --world …` keeps the
finding sealed (and out of the eval gates). The workbench also flips the matching
`findings.sealed` rows immediately so you see the change without re-running checks.

One consequence worth knowing: the seal key is the **anchor assertion**, not the
finding row. If several findings share an anchor (e.g. one dead character flagged
in two later scenes both point at the single "dies" assertion), sealing one seals
them all. That mirrors the checker exactly — it isn't a behavior this UI invented.

Unseal deletes the seal row and clears the mirrored flags.

## Scope (deferred, on purpose)

Read views + seal/unseal only. **Not** built here: fact merge/split,
knowledge-edge review, entity merge/confirm (those operate on the pre-store
resolution-state file, not the DB), branch switching, auth, file upload, and any
customer-facing polish.

## Boundaries this code respects

- **New files only, all under `ui/`.** It does not modify any pipeline file
  (`db/checks.sql`, `db/schema.sql`, `canon/store.py`, `canon/extract.py`,
  `canon/resolve.py`, …). It read-only-imports `connect()` / `resolve_db_url()`
  from `canon/ingest.py` so it talks to the same database the pipeline loads.
- **Reads the existing schema only** — `worlds, works, scenes, entities, aliases,
  assertions, scene_presence, findings, seals, coverage_notes, world_members`.
  Writes only `seals` (+ the mirrored `findings.sealed` flag) and the permanent
  `coverage_notes` status transition (open → sealed/dismissed, with attribution).
- **Rights hygiene** — it only reads the DB; it never ingests or persists script
  text. Any scratch output belongs in `out/` (gitignored).

## Layout

```
ui/
  app.py              FastAPI app: workbench routes + the note-surface routes
  auth.py             Supabase JWT verification + role checks (and peek_user for read views)
  db.py               all SQL (read) + the writes: seal/unseal finding, set_note_status
  format.py           pure display helpers: range gloss, quote highlighter, object side
  notes.py            pure note view-models: family grouping/caps, scene anchors, draft diff
  templates/          server-rendered Jinja (base + one per view; _macros.html, _surface.html)
  static/style.css    one stylesheet, no build step (light "surface" skin included)
  static/surface.js   vanilla JS: keyboard triage, citation→quote jump, scroll-spy
  requirements.txt    fastapi · uvicorn · jinja2 · PyJWT
```

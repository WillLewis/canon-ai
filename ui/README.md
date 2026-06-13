# Canon AI — Triage Workbench (`ui/`)

An internal, read-only **state inspector** for a loaded Canon world: a microscope
for the engine, not a product shell. It browses the assertion graph and the
continuity findings straight from Postgres, with every claim and every flag shown
next to its citation — and it can **seal/unseal a finding**. That's the only write
it performs.

It honors Canon's one rule: **it never generates story content.** No dialogue, no
loglines, no summaries — it only indexes, displays, and (for seals) records the
writer's intent. There are no LLM calls at all.

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
  assertions, scene_presence, findings, seals`. Writes only `seals` (+ the mirrored
  `findings.sealed` flag).
- **Rights hygiene** — it only reads the DB; it never ingests or persists script
  text. Any scratch output belongs in `out/` (gitignored).

## Layout

```
ui/
  app.py              FastAPI app: routes + the seal/unseal POST handlers
  db.py               all SQL (read) + seal_finding / unseal_finding (the one write)
  format.py           pure display helpers: range gloss, quote highlighter, object side
  templates/          server-rendered Jinja (base + one per view, shared _macros.html)
  static/style.css    one stylesheet, no build step
  requirements.txt    fastapi · uvicorn · jinja2
```

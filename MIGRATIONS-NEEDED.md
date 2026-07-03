# Migrations needed — queue for the next migration-lane occupant

The migration lane is serialized (docs/workstreams.md: at most one branch with
in-flight migrations). Workstreams that shipped without DDL record their needs
here; whoever next holds the lane picks these up.

**Queue empty as of `20260703110000_pipeline_runs.sql`.**

History: `20260703110000_pipeline_runs.sql` (Wave 6, P3-FRONTDOOR — the lane
was free, queue empty) landed `pipeline_runs` + `pipeline_run_events`: one row
per background ingest run (status/phase/progress counters, cost, heartbeat,
and the cumulative candidate extractions as jsonb resume state) plus the
append-only event ledger the ingest-theater page replays. RLS member-read via
`canon_has_role` (new `canon_run_world` resolver for events); no client write
policies — the runner writes via the service role / direct URL. Both tables
registered in `canon/export/full_export.py` (OPTIONAL_WORLD_TABLES) and
`canon/trust_delete.py` (OPTIONAL_WORLD_DELETES, events before runs); the
`db/schema.sql` mirror updated in the same commit.

Earlier: `20260703100000_queued_ddl_and_attribution.sql` (Wave 5,
P3-SCHEMA-SYNC) landed everything previously
queued here — P3-CONFIRM's `assertions.confirmed_by/confirmed_at` (now written
by `ui/db.py::rule_assertion`, usage_events audit rows kept as launch
analytics), P3-BILLING's `billing_customers` + `billing_subscriptions` (RLS
self-read only, fail-closed writes), and P3-RULES' `world_rules` (member-read /
editor-write RLS) — and resolved the P3-TRUST follow-up by keeping
`worlds.owner_id` (the RLS helpers treat it as owner) with
`canon/trust_delete.py::delete_account` now reassigning it to the earliest
surviving owner when a co-owned world survives. The plain-Postgres mirror
`db/schema.sql` was regenerated to match the full chain at the same commit.

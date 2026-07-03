# Migrations needed — queue for the next migration-lane occupant

The migration lane is serialized (docs/workstreams.md: at most one branch with
in-flight migrations). Workstreams that shipped without DDL record their needs
here; whoever next holds the lane picks these up.

## P3-CONFIRM: ruling attribution on `assertions`

The confirm queue (`/worlds/{id}/confirm`) transitions draft assertions to
`canon` or `rejected`, but the assertions table has no column recording who
ruled or when. Interim mechanism: each applied ruling writes a `usage_events`
audit row (kind `confirm_ruling` / `reject_ruling`, zero tokens, user + world
attributed) via `ops.metering.record_usage` — which attributes the ruling to a
user and a world, but not to the specific assertion.

Requested DDL:

```sql
-- Who ruled on a draft assertion in the confirm queue, and when.
-- Plain uuid, no FK to auth.users: rulings must survive user deletion
-- (same pattern as seals.ruled_by and coverage_notes.status_changed_by in
-- 20260703060000_identity_and_access.sql).
alter table assertions
  add column confirmed_by uuid,
  add column confirmed_at timestamptz;

comment on column assertions.confirmed_by is
  'auth.users.id of the editor whose confirm-queue ruling set status to canon/rejected; null = pipeline/gate decision';
comment on column assertions.confirmed_at is
  'when the confirm-queue ruling landed';
```

After it lands: `ui/db.py::rule_assertion` should set both columns inside its
existing guarded UPDATE (grep `MIGRATIONS-NEEDED` in ui/db.py for the exact
spot). Keep the `usage_events` audit rows — they double as launch analytics.
Backfill is not possible (`usage_events` rows carry no assertion id); ruling
attribution starts at the migration.

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

## P3-RULES: the `world_rules` table

The rule builder (`/worlds/{id}/rules`, `canon rules list|run`,
`canon/rules.py` + `canon/rules_store.py`) reads and writes a `world_rules`
table that has no migration yet — Wave 5 holds the lane. `params` is the
whole structured rule (validated by `canon.rules.from_params` before any
insert); `label` is the only free text; `disabled_at` implements the
enable/disable toggle without losing the rule. `created_by` is a plain uuid
with no FK to auth.users, so rules survive user deletion (same pattern as
`seals.ruled_by` and `assertions.confirmed_by` above).

Requested DDL:

```sql
-- Per-world structured rules (P3-RULES): cannot / only / exception,
-- compiled at read time by canon/rules.py. Never free text beyond label.
create table world_rules (
  id          uuid primary key default gen_random_uuid(),
  world_id    bigint not null references worlds(id) on delete cascade,
  kind        text not null check (kind in ('cannot','only','exception')),
  params      jsonb not null,
  label       text,
  created_by  uuid,
  created_at  timestamptz not null default now(),
  disabled_at timestamptz
);
create index world_rules_world on world_rules (world_id, kind);

comment on table world_rules is
  'Writer-authored structured rules over the closed predicate vocabulary; '
  'compiled to cited SQL checks by canon/rules.py. disabled_at set = rule off.';

-- RLS: member read, editor write — the same shape as the other world-scoped
-- content tables in 20260703060000_identity_and_access.sql. Delete is an
-- editor action too (the builder exposes it next to the toggle).
alter table world_rules enable row level security;
create policy world_rules_member_select on world_rules
  for select using (canon_has_role(world_id, 'viewer'));
create policy world_rules_editor_insert on world_rules
  for insert with check (canon_has_role(world_id, 'editor'));
create policy world_rules_editor_update on world_rules
  for update using (canon_has_role(world_id, 'editor'))
  with check (canon_has_role(world_id, 'editor'));
create policy world_rules_editor_delete on world_rules
  for delete using (canon_has_role(world_id, 'editor'));
```

Until it lands, every `world_rules` query in the code raises undefined-table
on a real database; the offline tests (tests/test_rules.py) drive the CRUD
through fakes and are green regardless.

-- P3-SCHEMA-SYNC (Wave 5) — lands every DDL item queued in MIGRATIONS-NEEDED.md:
--
--   1. P3-CONFIRM:  assertions.confirmed_by / confirmed_at (ruling attribution)
--   2. P3-BILLING:  billing_customers + billing_subscriptions (RLS fail-closed)
--   3. P3-RULES:    world_rules (member read / editor write RLS)
--
-- Additive-only on top of 20260703090000_report_engine_hardening.sql; existing
-- migrations are never edited. SQL below is copied verbatim from the queue
-- (MIGRATIONS-NEEDED.md); no deviations were needed — the queued DDL matches
-- the real schema (worlds.id bigint, canon_has_role from the identity
-- migration, pgcrypto's gen_random_uuid from the coverage_notes migration).

-- ---------------------------------------------------------------------------
-- 1. P3-CONFIRM: ruling attribution on assertions
-- ---------------------------------------------------------------------------

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

-- ---------------------------------------------------------------------------
-- 2. P3-BILLING: billing_customers + billing_subscriptions
-- ---------------------------------------------------------------------------

-- One Stripe customer per account. Plain uuid, no FK to auth.users: billing
-- history must survive user deletion (same pattern as seals.ruled_by in
-- 20260703060000_identity_and_access.sql).
create table billing_customers (
  user_id            uuid primary key,
  stripe_customer_id text unique not null,
  created_at         timestamptz not null default now()
);

comment on table billing_customers is
  'auth.users.id -> Stripe customer id, written by billing/webhooks.py under the service role';

-- One live subscription per account; upserts keep the latest Stripe state.
create table billing_subscriptions (
  user_id                uuid primary key,
  stripe_subscription_id text unique not null,
  status                 text not null,
  tier                   text not null default 'free' check (tier in ('free', 'paid')),
  current_period_end     timestamptz,
  updated_at             timestamptz not null default now()
);

comment on table billing_subscriptions is
  'latest Stripe subscription state per user; tier resolution = billing/store.py resolve_tier';
comment on column billing_subscriptions.current_period_end is
  'null right after checkout.session.completed (event carries no period end); '
  'customer.subscription.updated fills it — resolve_tier treats null-on-active as paid';

-- RLS: self-read only; ALL writes come from the backend via service_role
-- (which bypasses RLS). No insert/update/delete policies on purpose —
-- enabling RLS with no write policy fails closed for every client role.
alter table billing_customers     enable row level security;
alter table billing_subscriptions enable row level security;

create policy billing_customers_select_own on billing_customers
  for select using (user_id = auth.uid());
create policy billing_subscriptions_select_own on billing_subscriptions
  for select using (user_id = auth.uid());

-- ---------------------------------------------------------------------------
-- 3. P3-RULES: the world_rules table
-- ---------------------------------------------------------------------------

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

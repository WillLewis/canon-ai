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

## P3-BILLING: `billing_customers` + `billing_subscriptions`

`billing/store.py` reads and writes these two tables; they do not exist yet.
Until this lands, `resolve_tier` fails soft to `'free'` (with a
`billing_db_error` alert) and `CANON_FORCE_TIER` is the dev override, so the
billing branch merges safely ahead of the DDL. Webhook writes go through the
backend's service-role connection (bypasses RLS); browser clients get
self-read ONLY — there is no client-side write path to billing state, ever.

Requested DDL:

```sql
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
```

After it lands: nothing in `billing/` needs to change (the SQL above is
exactly what `billing/store.py` targets); delete the `CANON_FORCE_TIER`
override from any deployed env so tiers come from Stripe alone.

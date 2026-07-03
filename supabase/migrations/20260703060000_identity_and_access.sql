-- P3-IDENTITY — auth, tenancy, roles, attribution, RLS (docs/workstreams.md, Wave 2).
--
-- Additive-only DDL on top of 20260703033004_canon_schema.sql and
-- 20260703050000_coverage_notes.sql. Never edits existing migrations.
-- Targets Supabase (assumes the `auth` schema and `auth.uid()`); the plain-postgres
-- mirror in db/schema.sql is owned by the schema lane and is NOT edited here.
--
-- This migration also carries the tables Wave 3 workstreams need (share_links for
-- P3-ARTIFACTS, usage_events for P3-OPS, the 'rejected' assertion status for
-- P3-CONFIRM) because the migration lane allows only one DDL branch in flight.
--
-- Service-role note: Supabase's service_role (and the table owner `postgres`,
-- which the pipeline CLIs and the workbench reach via the direct DB URL) bypasses
-- RLS by design. Enabling RLS below therefore does not affect `canon ingest/store/
-- check` or the local workbench; it gates only anon/authenticated API clients.

-- ---------------------------------------------------------------------------
-- 1. Assertion status: widen for the confirm-queue workstream (P3-CONFIRM).
--    The spec asked to widen a CHECK constraint, but assertions.status is an
--    ENUM (`assertion_status`, see 20260703033004), so the additive equivalent
--    is ADD VALUE. Safe inside a transaction on PG >= 12 as long as the new
--    label is not used in this same transaction (it is not).
-- ---------------------------------------------------------------------------
alter type assertion_status add value if not exists 'rejected';

-- ---------------------------------------------------------------------------
-- 2. Identity tables
-- ---------------------------------------------------------------------------

-- One profile row per Supabase auth user (email and Google sign-ins both land
-- in auth.users; we never mint identities ourselves).
create table profiles (
  user_id      uuid primary key references auth.users(id) on delete cascade,
  display_name text,
  created_at   timestamptz not null default now()
);

-- Tenancy: worlds.id is bigint identity (not uuid) in this schema, so all
-- world-scoped identity tables key on bigint world_id. owner_id is NULLABLE on
-- purpose: worlds loaded pre-auth by the pipeline have no owner yet; an owner
-- can be attached later without rewriting rows.
alter table worlds
  add column owner_id uuid references auth.users(id) on delete set null;

create table world_members (
  world_id   bigint not null references worlds(id) on delete cascade,
  user_id    uuid   not null references auth.users(id) on delete cascade,
  role       text   not null check (role in ('owner','editor','viewer')),
  invited_by uuid,                              -- attribution only; no FK so rows
                                                -- survive inviter account deletion
  created_at timestamptz not null default now(),
  primary key (world_id, user_id)               -- satisfies unique(world_id, user_id)
);
create index world_members_user on world_members (user_id);  -- "my worlds" lookups

-- ---------------------------------------------------------------------------
-- 3. Attribution columns (who ruled, when). Plain uuid, no FK: rulings must
--    outlive the account that made them (the seal itself is world data).
-- ---------------------------------------------------------------------------
alter table seals
  add column ruled_by uuid,
  add column ruled_at timestamptz;

alter table coverage_notes
  add column status_changed_by uuid,
  add column status_changed_at timestamptz;

-- ---------------------------------------------------------------------------
-- 4. Sharing (P3-ARTIFACTS) and usage metering (P3-OPS)
-- ---------------------------------------------------------------------------
create table share_links (
  id         uuid primary key default gen_random_uuid(),
  token      text not null unique,
  world_id   bigint not null references worlds(id) on delete cascade,
  kind       text not null check (kind in ('report','bible')),
  created_by uuid,
  created_at timestamptz not null default now(),
  revoked_at timestamptz                         -- null = live
);

create table usage_events (
  id         bigserial primary key,
  user_id    uuid,
  world_id   bigint references worlds(id) on delete set null,
  kind       text,
  tokens_in  int not null default 0,
  tokens_out int not null default 0,
  cost_usd   numeric(10,4) not null default 0,
  created_at timestamptz not null default now()
);
create index usage_events_user_time on usage_events (user_id, created_at);

-- ---------------------------------------------------------------------------
-- 5. Row-level security
--
-- Helper functions are SECURITY DEFINER (owned by the migration role, which
-- RLS does not constrain), so policies can consult worlds/world_members without
-- recursing into their own policies. Membership hierarchy: owner > editor >
-- viewer. worlds.owner_id counts as 'owner' even without a world_members row.
-- ---------------------------------------------------------------------------

create or replace function canon_role_rank(r text) returns int
language sql immutable as $$
  select case r when 'owner' then 3 when 'editor' then 2 when 'viewer' then 1 else 0 end;
$$;

create or replace function canon_world_role(w bigint) returns text
language sql stable security definer set search_path = public as $$
  select coalesce(
    (select 'owner' from worlds where id = w and owner_id = auth.uid()),
    (select m.role from world_members m where m.world_id = w and m.user_id = auth.uid())
  );
$$;

create or replace function canon_has_role(w bigint, min_role text) returns boolean
language sql stable security definer set search_path = public as $$
  select canon_role_rank(canon_world_role(w)) >= canon_role_rank(min_role);
$$;

-- World-id resolvers for tables scoped through a parent (scenes -> works,
-- aliases -> entities, ...). SECURITY DEFINER so the lookup itself is not
-- re-filtered by the parent table's RLS.
create or replace function canon_work_world(w bigint) returns bigint
language sql stable security definer set search_path = public as $$
  select world_id from works where id = w;
$$;

create or replace function canon_scene_world(s bigint) returns bigint
language sql stable security definer set search_path = public as $$
  select wk.world_id from scenes sc join works wk on wk.id = sc.work_id where sc.id = s;
$$;

create or replace function canon_entity_world(e bigint) returns bigint
language sql stable security definer set search_path = public as $$
  select world_id from entities where id = e;
$$;

create or replace function canon_assertion_world(a bigint) returns bigint
language sql stable security definer set search_path = public as $$
  select world_id from assertions where id = a;
$$;

-- Enable RLS on every table (fails closed: no policy = no access for
-- anon/authenticated; service_role and the direct-URL pipeline are unaffected).
alter table worlds              enable row level security;
alter table works               enable row level security;
alter table scenes              enable row level security;
alter table entities            enable row level security;
alter table aliases             enable row level security;
alter table assertions          enable row level security;
alter table character_locations enable row level security;
alter table scene_presence      enable row level security;
alter table findings            enable row level security;
alter table seals               enable row level security;
alter table coverage_notes      enable row level security;
alter table profiles            enable row level security;
alter table world_members       enable row level security;
alter table share_links         enable row level security;
alter table usage_events        enable row level security;

-- ----- profiles: users manage their own row --------------------------------
create policy profiles_select_own on profiles
  for select using (user_id = auth.uid());
create policy profiles_insert_own on profiles
  for insert with check (user_id = auth.uid());
create policy profiles_update_own on profiles
  for update using (user_id = auth.uid()) with check (user_id = auth.uid());

-- ----- worlds ---------------------------------------------------------------
-- Any member sees the world; any authenticated user can create a world they
-- own; only the owner updates the world row itself (an editor UPDATE here
-- could reassign owner_id, so world-row changes are owner-only).
create policy worlds_member_select on worlds
  for select using (canon_has_role(id, 'viewer'));
create policy worlds_owner_insert on worlds
  for insert with check (owner_id = auth.uid());
create policy worlds_owner_update on worlds
  for update using (canon_has_role(id, 'owner'))
  with check (canon_has_role(id, 'owner'));

-- ----- world_members: readable by members, managed by the owner -------------
create policy world_members_member_select on world_members
  for select using (canon_has_role(world_id, 'viewer'));
create policy world_members_owner_insert on world_members
  for insert with check (canon_has_role(world_id, 'owner'));
create policy world_members_owner_update on world_members
  for update using (canon_has_role(world_id, 'owner'))
  with check (canon_has_role(world_id, 'owner'));
create policy world_members_owner_delete on world_members
  for delete using (canon_has_role(world_id, 'owner'));

-- ----- world-scoped content: members read, editor+ writes -------------------
-- Tables with a direct world_id column.
create policy works_member_select on works
  for select using (canon_has_role(world_id, 'viewer'));
create policy works_editor_insert on works
  for insert with check (canon_has_role(world_id, 'editor'));
create policy works_editor_update on works
  for update using (canon_has_role(world_id, 'editor'))
  with check (canon_has_role(world_id, 'editor'));

create policy entities_member_select on entities
  for select using (canon_has_role(world_id, 'viewer'));
create policy entities_editor_insert on entities
  for insert with check (canon_has_role(world_id, 'editor'));
create policy entities_editor_update on entities
  for update using (canon_has_role(world_id, 'editor'))
  with check (canon_has_role(world_id, 'editor'));

create policy assertions_member_select on assertions
  for select using (canon_has_role(world_id, 'viewer'));
create policy assertions_editor_insert on assertions
  for insert with check (canon_has_role(world_id, 'editor'));
create policy assertions_editor_update on assertions
  for update using (canon_has_role(world_id, 'editor'))
  with check (canon_has_role(world_id, 'editor'));

create policy findings_member_select on findings
  for select using (canon_has_role(world_id, 'viewer'));
create policy findings_editor_insert on findings
  for insert with check (canon_has_role(world_id, 'editor'));
create policy findings_editor_update on findings
  for update using (canon_has_role(world_id, 'editor'))
  with check (canon_has_role(world_id, 'editor'));

-- seals also get editor DELETE: unsealing removes the row.
create policy seals_member_select on seals
  for select using (canon_has_role(world_id, 'viewer'));
create policy seals_editor_insert on seals
  for insert with check (canon_has_role(world_id, 'editor'));
create policy seals_editor_update on seals
  for update using (canon_has_role(world_id, 'editor'))
  with check (canon_has_role(world_id, 'editor'));
create policy seals_editor_delete on seals
  for delete using (canon_has_role(world_id, 'editor'));

create policy coverage_notes_member_select on coverage_notes
  for select using (canon_has_role(world_id, 'viewer'));
create policy coverage_notes_editor_insert on coverage_notes
  for insert with check (canon_has_role(world_id, 'editor'));
create policy coverage_notes_editor_update on coverage_notes
  for update using (canon_has_role(world_id, 'editor'))
  with check (canon_has_role(world_id, 'editor'));

-- Tables scoped through a parent row.
create policy scenes_member_select on scenes
  for select using (canon_has_role(canon_work_world(work_id), 'viewer'));
create policy scenes_editor_insert on scenes
  for insert with check (canon_has_role(canon_work_world(work_id), 'editor'));
create policy scenes_editor_update on scenes
  for update using (canon_has_role(canon_work_world(work_id), 'editor'))
  with check (canon_has_role(canon_work_world(work_id), 'editor'));

create policy aliases_member_select on aliases
  for select using (canon_has_role(canon_entity_world(entity_id), 'viewer'));
create policy aliases_editor_insert on aliases
  for insert with check (canon_has_role(canon_entity_world(entity_id), 'editor'));
create policy aliases_editor_update on aliases
  for update using (canon_has_role(canon_entity_world(entity_id), 'editor'))
  with check (canon_has_role(canon_entity_world(entity_id), 'editor'));

create policy scene_presence_member_select on scene_presence
  for select using (canon_has_role(canon_scene_world(scene_id), 'viewer'));
create policy scene_presence_editor_insert on scene_presence
  for insert with check (canon_has_role(canon_scene_world(scene_id), 'editor'));
create policy scene_presence_editor_update on scene_presence
  for update using (canon_has_role(canon_scene_world(scene_id), 'editor'))
  with check (canon_has_role(canon_scene_world(scene_id), 'editor'));

create policy character_locations_member_select on character_locations
  for select using (canon_has_role(canon_assertion_world(assertion_id), 'viewer'));
create policy character_locations_editor_insert on character_locations
  for insert with check (canon_has_role(canon_assertion_world(assertion_id), 'editor'));
create policy character_locations_editor_update on character_locations
  for update using (canon_has_role(canon_assertion_world(assertion_id), 'editor'))
  with check (canon_has_role(canon_assertion_world(assertion_id), 'editor'));

-- ----- share_links: members read, owner manages -----------------------------
create policy share_links_member_select on share_links
  for select using (canon_has_role(world_id, 'viewer'));
create policy share_links_owner_insert on share_links
  for insert with check (canon_has_role(world_id, 'owner'));
create policy share_links_owner_update on share_links
  for update using (canon_has_role(world_id, 'owner'))
  with check (canon_has_role(world_id, 'owner'));
create policy share_links_owner_delete on share_links
  for delete using (canon_has_role(world_id, 'owner'));

-- ----- usage_events: users see their own; world members see the world's -----
-- Inserts normally come from the backend via service_role (bypasses RLS); the
-- client-side policy only lets an editor+ log an event as themselves.
create policy usage_events_select on usage_events
  for select using (user_id = auth.uid() or canon_has_role(world_id, 'viewer'));
create policy usage_events_insert_own on usage_events
  for insert with check (user_id = auth.uid() and canon_has_role(world_id, 'editor'));

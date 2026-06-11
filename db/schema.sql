-- Canon AI — schema v0
-- Apply on Supabase local: supabase start && psql ... -f db/schema.sql
-- Requires: btree_gist (exclusion constraints mixing = with &&)

create extension if not exists btree_gist;
-- create extension if not exists vector;  -- pgvector: deferred until Audience Memory (P2)

-- ---------- enums ----------
create type entity_kind as enum ('character','location','object','faction','event','rule','other');
create type assertion_status as enum ('draft','canon','sealed','retconned','rejected');
create type alias_kind as enum ('name_variant','nickname','role_reference');

-- ---------- worlds (one writer's show/book/series) ----------
create table worlds (
  id          bigint generated always as identity primary key,
  name        text not null,
  created_at  timestamptz not null default now()
);

-- ---------- works & scenes (provenance spine) ----------
create table works (                       -- an episode, chapter, or document
  id          bigint generated always as identity primary key,
  world_id    bigint not null references worlds(id) on delete cascade,
  title       text not null,               -- 'S1E01', 'Manuscript ch. 4', 'World doc'
  source_file text,
  sort_order  int not null
);

create table scenes (
  id             bigint generated always as identity primary key,
  work_id        bigint not null references works(id) on delete cascade,
  slug           text,                     -- 'INT. BOATHOUSE — NIGHT'
  story_position int not null,             -- global integer axis per world (v0; see D6)
  is_flashback   boolean not null default false,
  raw_text       text not null,
  unique (work_id, story_position)
);
create index scenes_story_pos on scenes (story_position);

-- ---------- entities & aliases ----------
create table entities (
  id        bigint generated always as identity primary key,
  world_id  bigint not null references worlds(id) on delete cascade,
  kind      entity_kind not null,
  name      text not null,                 -- canonical
  dossier   text,                          -- short LLM-maintained summary for disambiguation
  provisional boolean not null default false,
  unique (world_id, name, kind)
);

create table aliases (
  entity_id bigint not null references entities(id) on delete cascade,
  alias     text not null,
  kind      alias_kind not null,
  primary key (entity_id, alias)
);
create index aliases_alias on aliases (lower(alias));

-- ---------- assertions: the atomic unit ----------
-- (subject) —predicate→ (object) valid over [story positions), cited, statused.
create table assertions (
  id              bigint generated always as identity primary key,
  world_id        bigint not null references worlds(id) on delete cascade,
  subject_id      bigint not null references entities(id),
  predicate       text not null,           -- closed vocabulary; see docs/extraction.md
  object_id       bigint references entities(id),          -- entity-valued objects
  object_value    text,                                     -- literal/fact-valued objects
  object_assertion_id bigint references assertions(id),     -- epistemic: knows/believes THIS assertion
  polarity        boolean not null default true,            -- false = explicit negation ('cannot drive')
  valid_during    int4range not null,       -- story-position validity; unbounded ends allowed
  established_in_scene bigint not null references scenes(id),
  supporting_quote text,
  confidence      real not null check (confidence >= 0 and confidence <= 1),
  status          assertion_status not null default 'draft',
  confirmed_by_human boolean not null default false,
  superseded_by   bigint references assertions(id),         -- retcon chain (recorded, not reasoned)
  created_at      timestamptz not null default now(),
  check (object_id is not null or object_value is not null or object_assertion_id is not null)
);
create index assertions_subj   on assertions (subject_id, predicate);
create index assertions_valid  on assertions using gist (valid_during);
create index assertions_status on assertions (world_id, status);

-- ---------- write-time continuity invariants (exclusion constraints) ----------
-- A continuity rule as schema: one character cannot be located_at two places
-- over overlapping story intervals. Enforced via a thin typed table that mirrors
-- located_at assertions (kept in sync by the ingest layer).
create table character_locations (
  assertion_id bigint primary key references assertions(id) on delete cascade,
  character_id bigint not null references entities(id),
  location_id  bigint not null references entities(id),
  valid_during int4range not null,
  exclude using gist (
    character_id with =,
    valid_during with &&
  )
);
-- Same pattern available for alive-intervals if death/resurrection churn appears:
-- exclude using gist (character_id with =, valid_during with &&) on a character_lifespans table.

-- ---------- scene presence (high-volume, so typed, not generic assertions) ----------
create table scene_presence (
  scene_id  bigint not null references scenes(id) on delete cascade,
  entity_id bigint not null references entities(id),
  primary key (scene_id, entity_id)
);

-- ---------- check findings (output of db/checks.sql runs) ----------
create table findings (
  id          bigint generated always as identity primary key,
  world_id    bigint not null references worlds(id) on delete cascade,
  check_name  text not null,
  severity    text not null check (severity in ('critical','warning','note')),
  explanation text not null,
  scene_id    bigint references scenes(id),
  assertion_a bigint references assertions(id),
  assertion_b bigint references assertions(id),
  sealed      boolean not null default false,   -- writer marked intentional
  run_at      timestamptz not null default now()
);

-- ---------- seals: permanent 'intentional' marks, independent of finding runs ----------
create table seals (
  world_id    bigint not null references worlds(id) on delete cascade,
  check_name  text not null,
  assertion_a bigint not null references assertions(id),
  assertion_b bigint references assertions(id),
  reason      text,
  created_at  timestamptz not null default now(),
  coalesce_b  bigint generated always as (coalesce(assertion_b, 0)) stored,
  primary key (world_id, check_name, assertion_a, coalesce_b)
);

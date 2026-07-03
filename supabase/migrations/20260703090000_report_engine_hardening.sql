-- P3-ENGINE report-engine hardening:
-- per-world deterministic family toggles plus indexes for retcon ripple scans.

alter type assertion_status add value if not exists 'rejected';

create table if not exists world_family_config (
  world_id    bigint not null references worlds(id) on delete cascade,
  family      text not null,
  enabled     boolean not null default true,
  updated_at  timestamptz not null default now(),
  primary key (world_id, family),
  check (family ~ '^[A-Za-z0-9_]+$')
);

create index if not exists world_family_config_disabled
  on world_family_config (world_id, family)
  where not enabled;

create index if not exists assertions_world_valid
  on assertions using gist (world_id, valid_during);

create index if not exists assertions_world_subject_predicate
  on assertions (world_id, subject_id, predicate);

create index if not exists assertions_world_object
  on assertions (world_id, object_id)
  where object_id is not null;

create index if not exists scene_presence_entity_scene
  on scene_presence (entity_id, scene_id);

create index if not exists findings_world_check
  on findings (world_id, check_name, sealed);

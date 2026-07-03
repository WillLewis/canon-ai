-- Reader's Report note lifecycle (docs/readers-report.md)
-- Notes persist across report runs so sealed/dismissed judgments do not re-raise.

create extension if not exists pgcrypto;

create table coverage_notes (
  id              uuid primary key default gen_random_uuid(),
  world_id        bigint not null references worlds(id) on delete cascade,
  note_key        text not null unique,
  family          text not null check (family in ('F1','F2','F3','F4')),
  summary         text not null,
  body            text not null,
  evidence        jsonb not null default '{}'::jsonb,
  salience        int not null default 0,
  status          text not null default 'open'
                  check (status in ('open','sealed','dismissed','addressed')),
  first_seen_run  uuid not null,
  last_seen_run   uuid not null,
  resolved_at     timestamptz,
  created_at      timestamptz not null default now(),
  updated_at      timestamptz not null default now()
);

create index coverage_notes_world_status_family
  on coverage_notes (world_id, status, family);
create index coverage_notes_evidence_gin
  on coverage_notes using gin (evidence);

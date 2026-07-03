-- P3-FRONTDOOR (Wave 6) — pipeline runs + their event streams.
--
-- One pipeline_runs row per background ingest ("ingest theater"): live phase /
-- progress counters the poll endpoint reads, plus the resume state (the
-- cumulative candidate extractions as jsonb) when a run fails partway.
-- pipeline_run_events is the append-only narration ledger the theater page
-- replays from seq 0 on reload — scene lines, entity lines, findings, stats.
--
-- Writes come ONLY from the server (service role / direct URL) — the runner
-- thread in ui/jobs.py. Clients get member-read and nothing else.

create table pipeline_runs (
  id           uuid primary key default gen_random_uuid(),
  world_id     bigint not null references worlds(id) on delete cascade,
  user_id      uuid,                    -- who started it; no FK (survives user deletion)
  status       text not null default 'running'
               check (status in ('running','done','failed','aborted')),
  phase        text not null default 'reading'
               check (phase in ('reading','extracting','resolving','checking','reporting','done')),
  scenes_total int default 0,
  scenes_done  int default 0,
  facts_total  int default 0,
  cost_usd     numeric(10,4) default 0,
  error        text,                    -- last failure message (resumable runs)
  candidates   jsonb,                   -- cumulative scene extractions = the resume state
  created_at   timestamptz default now(),
  heartbeat_at timestamptz default now()  -- runner liveness; stale (>90s) running = failed
);

create index pipeline_runs_world_time on pipeline_runs (world_id, created_at desc);

create table pipeline_run_events (
  run_id     uuid references pipeline_runs(id) on delete cascade,
  seq        int,
  kind       text not null,             -- scene | entity | finding | phase | stat | error | cost_abort | done
  data       jsonb not null default '{}',
  created_at timestamptz default now(),
  primary key (run_id, seq)
);

-- ---------------------------------------------------------------------------
-- RLS: member-read via the world (canon_has_role, identity migration); NO
-- client write policies on purpose — enabling RLS with no insert/update/delete
-- policy fails closed for every client role, and the runner writes via the
-- service role / direct URL which RLS does not constrain.
-- ---------------------------------------------------------------------------

-- World resolver for events (scoped through their run), SECURITY DEFINER so
-- the lookup is not re-filtered by pipeline_runs' own policy — same pattern as
-- canon_scene_world in 20260703060000_identity_and_access.sql.
create or replace function canon_run_world(r uuid) returns bigint
language sql stable security definer set search_path = public as $$
  select world_id from pipeline_runs where id = r;
$$;

alter table pipeline_runs       enable row level security;
alter table pipeline_run_events enable row level security;

create policy pipeline_runs_member_select on pipeline_runs
  for select using (canon_has_role(world_id, 'viewer'));
create policy pipeline_run_events_member_select on pipeline_run_events
  for select using (canon_has_role(canon_run_world(run_id), 'viewer'));

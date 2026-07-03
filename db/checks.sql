-- Canon AI — MVP continuity checks (Phase 0)
-- Each SELECT returns:
--   check_name, severity, explanation, scene_id, assertion_a, assertion_b
-- The runner treats scene_id/assertion ids as citations back to source scenes.
-- Convention: bind :world_id. Sealed assertions are writer-marked intentional
-- and suppressed by every check.

-- ============================================================
-- CHECK 1 · dead_speaker (critical)
-- A character is present/acting in a non-flashback scene after death.
-- ============================================================
select
  'dead_speaker' as check_name,
  'critical' as severity,
  format('%s appears in scene %s (pos %s) after death established in %s (pos %s): "%s".',
         e.name,
         coalesce(s.slug, 'scene ' || s.id::text),
         s.story_position,
         coalesce(ds.slug, 'scene ' || ds.id::text),
         ds.story_position,
         coalesce(nullif(d.supporting_quote, ''), 'no quote')) as explanation,
  s.id as scene_id,
  d.id as assertion_a,
  null::bigint as assertion_b
from assertions d
join entities e        on e.id = d.subject_id
join scenes ds         on ds.id = d.established_in_scene
join scene_presence sp on sp.entity_id = d.subject_id
join scenes s          on s.id = sp.scene_id
where d.world_id = :world_id
  and d.predicate = 'dies'
  and d.polarity
  and d.status != 'sealed'
  and s.story_position > lower(d.valid_during)
  and not s.is_flashback
  -- resurrection escape hatch: no later active alive assertion covering this scene.
  and not exists (
    select 1
    from assertions r
    where r.world_id = d.world_id
      and r.subject_id = d.subject_id
      and r.predicate = 'alive'
      and r.polarity
      and r.status != 'sealed'
      and r.valid_during @> s.story_position
      and lower(r.valid_during) > lower(d.valid_during)
  );

-- ============================================================
-- CHECK 2 · presence_conflict (critical)
-- Same subject located in two different places over overlapping intervals.
-- The typed character_locations exclusion catches writes through the loader;
-- this scan reports hand-seeded or legacy assertion rows.
-- ============================================================
select
  'presence_conflict' as check_name,
  'critical' as severity,
  format('%s is at %s during %s and also at %s during %s, and the intervals overlap.',
         e.name,
         la.name,
         a.valid_during::text,
         lb.name,
         b.valid_during::text) as explanation,
  b.established_in_scene as scene_id,
  a.id as assertion_a,
  b.id as assertion_b
from assertions a
join assertions b
  on b.world_id = a.world_id
 and b.subject_id = a.subject_id
 and b.id > a.id
join entities e  on e.id = a.subject_id
join entities la on la.id = a.object_id
join entities lb on lb.id = b.object_id
where a.world_id = :world_id
  and a.predicate = 'located_at'
  and b.predicate = 'located_at'
  and a.status != 'sealed'
  and b.status != 'sealed'
  and a.object_id is not null
  and b.object_id is not null
  and a.object_id <> b.object_id
  and a.valid_during && b.valid_during
  -- Open-lower backdated ranges can overlap everything by construction.
  -- Route those through confirmation/anchoring instead of noisy flags.
  and not lower_inf(a.valid_during)
  and not lower_inf(b.valid_during);

-- ============================================================
-- CHECK 3 · premature_knowledge (critical)
-- A character knows a fact whose source was established earlier, but there is
-- no on-screen scene where they were present with a prior active knower.
-- ============================================================
select
  'premature_knowledge' as check_name,
  'critical' as severity,
  format('%s knows "%s" at pos %s with no on-screen source before that citation.',
         e.name,
         k.object_value,
         coalesce(lower(k.valid_during), s_use.story_position)) as explanation,
  k.established_in_scene as scene_id,
  k.id as assertion_a,
  null::bigint as assertion_b
from assertions k
join entities e    on e.id = k.subject_id
join scenes s_use  on s_use.id = k.established_in_scene
where k.world_id = :world_id
  and k.predicate = 'knows'
  and k.object_value is not null
  and k.status != 'sealed'
  -- The same fact existed earlier. The first active knower is the originator
  -- for v0 and is not flagged by this rule.
  and coalesce(lower(k.valid_during), s_use.story_position) > (
    select min(coalesce(lower(k0.valid_during), s0.story_position))
    from assertions k0
    join scenes s0 on s0.id = k0.established_in_scene
    where k0.world_id = k.world_id
      and k0.predicate = 'knows'
      and k0.object_value is not null
      and k0.status != 'sealed'
      and regexp_replace(lower(k0.object_value), '[^a-z0-9 ]', '', 'g')
        = regexp_replace(lower(k.object_value),  '[^a-z0-9 ]', '', 'g')
  )
  -- No scene at/before the knowledge citation where the subject was present
  -- with someone who already knew the same fact.
  and not exists (
    select 1
    from scene_presence sp_self
    join scenes sc               on sc.id = sp_self.scene_id
    join scene_presence sp_other on sp_other.scene_id = sp_self.scene_id
    join assertions k2           on k2.subject_id = sp_other.entity_id
    where sp_self.entity_id = k.subject_id
      and sc.story_position <= coalesce(lower(k.valid_during), s_use.story_position)
      and k2.world_id = k.world_id
      and k2.predicate = 'knows'
      and k2.object_value is not null
      and k2.status != 'sealed'
      and k2.subject_id <> k.subject_id
      and regexp_replace(lower(k2.object_value), '[^a-z0-9 ]', '', 'g')
        = regexp_replace(lower(k.object_value),  '[^a-z0-9 ]', '', 'g')
      and k2.valid_during @> sc.story_position
  );

-- ============================================================
-- CHECK 4 · destroyed_location_use (critical)
-- A non-flashback scene uses a location/object after it was destroyed.
-- ============================================================
select
  'destroyed_location_use' as check_name,
  'critical' as severity,
  format('Scene %s (pos %s) uses %s after destruction established in %s (pos %s).',
         coalesce(s.slug, 'scene ' || s.id::text),
         s.story_position,
         e.name,
         coalesce(ds.slug, 'scene ' || ds.id::text),
         ds.story_position) as explanation,
  s.id as scene_id,
  d.id as assertion_a,
  null::bigint as assertion_b
from assertions d
join entities e        on e.id = d.subject_id and e.kind in ('location','object')
join scenes ds         on ds.id = d.established_in_scene
join scene_presence sp on sp.entity_id = d.subject_id
join scenes s          on s.id = sp.scene_id
where d.world_id = :world_id
  and d.predicate = 'destroyed'
  and d.polarity
  and d.status != 'sealed'
  -- The destruction scene itself is not a violation.
  and s.story_position > lower(d.valid_during)
  and not s.is_flashback;

-- ============================================================
-- CHECK 5 · dangling_reference (note)
-- Assertions whose subject or object is still a provisional entity.
-- ============================================================
select
  'dangling_reference' as check_name,
  'note' as severity,
  format('"%s" is referenced in scene %s (pos %s) but is still unresolved.',
         e.name,
         coalesce(s.slug, 'scene ' || s.id::text),
         s.story_position) as explanation,
  a.established_in_scene as scene_id,
  a.id as assertion_a,
  null::bigint as assertion_b
from (
  -- One note per provisional entity: cite its first active reference.
  select e2.id as entity_id, min(a2.id) as first_assertion_id
  from assertions a2
  join entities e2
    on e2.id in (a2.subject_id, a2.object_id)
   and e2.provisional
  where a2.world_id = :world_id
    and a2.status != 'sealed'
  group by e2.id
) firsts
join assertions a on a.id = firsts.first_assertion_id
join entities e   on e.id = firsts.entity_id
join scenes s     on s.id = a.established_in_scene;

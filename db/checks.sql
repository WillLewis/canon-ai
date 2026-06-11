-- Canon AI — MVP continuity checks (Phase 0)
-- Each check SELECTs findings: (check_name, severity, explanation, scene_id, assertion_a, assertion_b).
-- Runner inserts into findings, skipping pairs present in seals.
-- Doctrine: SQL judges; the LLM may narrate these rows afterward, never originate them.
-- Convention: :world_id parameter; checks ignore status in ('rejected','retconned').

-- ============================================================
-- CHECK 1 · dead_speaker (critical)
-- A character is present/acting in a scene whose story_position
-- falls after their death interval begins (flashbacks exempt).
-- ============================================================
select
  'dead_speaker'                                   as check_name,
  'critical'                                       as severity,
  format('%s appears in scene %s (pos %s) but died at pos %s (%s).',
         e.name, s.slug, s.story_position, lower(d.valid_during), d.supporting_quote) as explanation,
  s.id                                             as scene_id,
  d.id                                             as assertion_a,
  null::bigint                                     as assertion_b
from assertions d
join entities e        on e.id = d.subject_id
join scene_presence sp on sp.entity_id = e.id
join scenes s          on s.id = sp.scene_id
where d.world_id = :world_id
  and d.predicate = 'dies'
  and d.status not in ('rejected','retconned')
  and s.story_position > lower(d.valid_during)
  and not s.is_flashback
  -- resurrection escape hatch: no later 'alive' assertion covering this scene
  and not exists (
    select 1 from assertions r
    where r.subject_id = d.subject_id
      and r.predicate = 'alive' and r.polarity
      and r.valid_during @> s.story_position
      and lower(r.valid_during) > lower(d.valid_during)
  );

-- ============================================================
-- CHECK 2 · presence_conflict (critical)
-- Same character with two located_at assertions over overlapping
-- intervals at different locations. (The exclusion constraint
-- catches this at write time for synced rows; this scan catches
-- generic-assertion rows and pre-existing data.)
-- ============================================================
select
  'presence_conflict',
  'critical',
  format('%s is at %s during %s and also at %s during %s — intervals overlap.',
         e.name, la.name, a.valid_during::text, lb.name, b.valid_during::text),
  a.established_in_scene,
  a.id, b.id
from assertions a
join assertions b on b.subject_id = a.subject_id and b.id > a.id
join entities e  on e.id = a.subject_id
join entities la on la.id = a.object_id
join entities lb on lb.id = b.object_id
where a.world_id = :world_id and b.world_id = :world_id
  and a.predicate = 'located_at' and b.predicate = 'located_at'
  and a.object_id <> b.object_id
  and a.valid_during && b.valid_during
  and a.status not in ('rejected','retconned')
  and b.status not in ('rejected','retconned');

-- ============================================================
-- CHECK 3 · premature_knowledge (critical) — the signature check
-- A character acts on / references a fact (knows-assertion) in a
-- scene positioned BEFORE their knows-interval starts.
-- v0 proxy: a knows-assertion whose established scene precedes
-- the canonical start of the same character knowing the same fact.
-- ============================================================
select
  'premature_knowledge',
  'critical',
  format('%s appears to use "%s" at pos %s, but canon has them learning it at pos %s.',
         e.name, coalesce(k_early.object_value,'<fact>'),
         s_early.story_position, lower(k_canon.valid_during)),
  k_early.established_in_scene,
  k_early.id, k_canon.id
from assertions k_early
join assertions k_canon
  on  k_canon.subject_id = k_early.subject_id
  and k_canon.predicate = 'knows'
  and k_canon.object_value = k_early.object_value
  and k_canon.id <> k_early.id
join scenes s_early on s_early.id = k_early.established_in_scene
join entities e     on e.id = k_early.subject_id
where k_early.world_id = :world_id
  and k_early.predicate = 'knows'
  and k_early.status not in ('rejected','retconned')
  and k_canon.status = 'canon'
  and s_early.story_position < lower(k_canon.valid_during)
  and not s_early.is_flashback;

-- ============================================================
-- CHECK 4 · destroyed_location_use (critical)
-- A scene takes place in (or presence is asserted at) a location
-- during its destroyed-interval.
-- ============================================================
select
  'destroyed_location_use',
  'critical',
  format('Scene %s (pos %s) uses %s, destroyed at pos %s.',
         s.slug, s.story_position, e.name, lower(d.valid_during)),
  s.id,
  d.id, null::bigint
from assertions d
join entities e        on e.id = d.subject_id and e.kind in ('location','object')
join scene_presence sp on sp.entity_id = e.id
join scenes s          on s.id = sp.scene_id
where d.world_id = :world_id
  and d.predicate = 'destroyed' and d.polarity
  and d.status not in ('rejected','retconned')
  and d.valid_during @> s.story_position
  and not s.is_flashback;

-- ============================================================
-- CHECK 5 · capability_violation (warning)
-- A character does something canon says they cannot do
-- ('cannot' assertions, e.g. Maya cannot drive), proxied by a
-- conflicting positive assertion during the cannot-interval.
-- ============================================================
select
  'capability_violation',
  'warning',
  format('%s: canon says cannot "%s" (since pos %s), but a conflicting assertion appears at pos %s.',
         e.name, c.object_value, lower(c.valid_during), s.story_position),
  p.established_in_scene,
  p.id, c.id
from assertions c
join assertions p
  on  p.subject_id = c.subject_id
  and p.object_value = c.object_value
  and p.polarity and p.id <> c.id
join scenes s   on s.id = p.established_in_scene
join entities e on e.id = c.subject_id
where c.world_id = :world_id
  and c.predicate = 'cannot' and c.polarity
  and c.status not in ('rejected','retconned')
  and p.status not in ('rejected','retconned')
  and c.valid_during @> s.story_position;

-- ============================================================
-- CHECK 6 · dangling_reference (note)
-- Assertions whose subject or object is a provisional entity —
-- referenced but never established in canon.
-- ============================================================
select
  'dangling_reference',
  'note',
  format('"%s" is referenced (scene pos %s) but never established in canon.',
         e.name, s.story_position),
  a.established_in_scene,
  a.id, null::bigint
from assertions a
join scenes s on s.id = a.established_in_scene
join entities e on e.id in (a.subject_id, a.object_id) and e.provisional
where a.world_id = :world_id
  and a.status not in ('rejected','retconned');

-- ============================================================
-- CHECK 7 · idle_setup (note) — setup/payoff tracker, v0 form
-- 'promised' or 'goal' assertions with open-ended intervals and no
-- later assertion by the same subject touching the same object_value.
-- ============================================================
select
  'idle_setup',
  'note',
  format('Open thread: %s — "%s" set up at pos %s, untouched since.',
         e.name, a.object_value, lower(a.valid_during)),
  a.established_in_scene,
  a.id, null::bigint
from assertions a
join entities e on e.id = a.subject_id
where a.world_id = :world_id
  and a.predicate in ('promised','goal','secret_of')
  and upper_inf(a.valid_during)
  and a.status not in ('rejected','retconned')
  and not exists (
    select 1 from assertions later
    join scenes ls on ls.id = later.established_in_scene
    join scenes asrc on asrc.id = a.established_in_scene
    where later.subject_id = a.subject_id
      and later.object_value = a.object_value
      and later.id <> a.id
      and ls.story_position > asrc.story_position
  );

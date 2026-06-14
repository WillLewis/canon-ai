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
  -- unanchored intervals (open lower bound — backdated claims) overlap
  -- everything by construction; they are confirm-queue material, not flags
  -- (false-positive doctrine: prefer a missed borderline flag over crying wolf)
  and not lower_inf(a.valid_during)
  and not lower_inf(b.valid_during)
  and a.status not in ('rejected','retconned')
  and b.status not in ('rejected','retconned');

-- ============================================================
-- CHECK 3 · premature_knowledge (critical) — the signature check
-- A character knows a fact with no on-screen source: they are not the
-- originator (earliest knower) of it, and were never co-present in a scene
-- (at or before they know it) with another character who already knew it.
-- v0 proxy for "the reveal of HOW they know never lands" — see answer-key P2.
-- ============================================================
-- Anchor = where the knowledge is REVEALED (the establishing scene), not the
-- interval's lower bound: a backdated knows ("I've known since...") has an open
-- lower bound, and anchoring on lower() would make unsourced backdated claims
-- structurally unflaggable — they are the prime case (answer-key P2).
select
  'premature_knowledge',
  'critical',
  format('%s knows "%s" (pos %s) with no on-screen source — never present with a prior knower of it.',
         e.name, k.object_value, coalesce(lower(k.valid_during), s_use.story_position)),
  k.established_in_scene,
  k.id, null::bigint
from assertions k
join entities e on e.id = k.subject_id
join scenes s_use on s_use.id = k.established_in_scene
where k.world_id = :world_id
  and k.predicate = 'knows'
  and k.object_value is not null
  and k.status not in ('rejected','retconned')
  -- not the originator: the same fact was revealed strictly earlier by someone
  -- (object_value comparison is normalized: lowercase, alphanumerics only —
  --  extraction reuses canonical handles, this absorbs residual punct/case drift)
  and coalesce(lower(k.valid_during), s_use.story_position) > (
    select min(coalesce(lower(k0.valid_during), s0.story_position))
    from assertions k0
    join scenes s0 on s0.id = k0.established_in_scene
    where k0.world_id = k.world_id
      and k0.predicate = 'knows'
      and regexp_replace(lower(k0.object_value), '[^a-z0-9 ]', '', 'g')
        = regexp_replace(lower(k.object_value),  '[^a-z0-9 ]', '', 'g')
      and k0.status not in ('rejected','retconned')
  )
  -- and never co-present with an existing knower at or before learning it
  and not exists (
    select 1
    from scene_presence sp_self
    join scenes sc               on sc.id = sp_self.scene_id
    join scene_presence sp_other on sp_other.scene_id = sp_self.scene_id
    join assertions k2           on k2.subject_id = sp_other.entity_id
    where sp_self.entity_id = k.subject_id
      and sc.story_position <= coalesce(lower(k.valid_during), s_use.story_position)
      and k2.predicate = 'knows'
      and regexp_replace(lower(k2.object_value), '[^a-z0-9 ]', '', 'g')
        = regexp_replace(lower(k.object_value),  '[^a-z0-9 ]', '', 'g')
      and k2.subject_id <> k.subject_id
      and k2.status not in ('rejected','retconned')
      and k2.valid_during @> sc.story_position
  );

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
  -- strictly after destruction (the destruction scene itself is not a violation)
  and s.story_position > lower(d.valid_during)
  and not s.is_flashback;

-- ============================================================
-- CHECK 5 · capability_violation (warning)
-- A character does something canon says they cannot do. Two arms:
--   A) a positive assertion whose (normalized) object_value contains the
--      capability handle, during the cannot-interval;
--   B) an explicit contradiction: cannot(X, v, polarity=false) — "X is doing
--      the thing" — overlapping cannot(X, v, polarity=true).
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
  and p.id <> c.id
  and (
        ( p.polarity
          and p.predicate <> 'cannot'
          and position(regexp_replace(lower(c.object_value), '[^a-z0-9 ]', '', 'g')
                       in regexp_replace(lower(p.object_value), '[^a-z0-9 ]', '', 'g')) > 0 )
     or ( p.predicate = 'cannot'
          and not p.polarity
          and regexp_replace(lower(p.object_value), '[^a-z0-9 ]', '', 'g')
            = regexp_replace(lower(c.object_value), '[^a-z0-9 ]', '', 'g') )
  )
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
from (
  -- one note per provisional entity: cite its first reference
  select e2.id as eid, min(a2.id) as first_aid
  from assertions a2
  join entities e2 on e2.id in (a2.subject_id, a2.object_id) and e2.provisional
  where a2.world_id = :world_id
    and a2.status not in ('rejected','retconned')
  group by e2.id
) firsts
join assertions a on a.id = firsts.first_aid
join entities e   on e.id = firsts.eid
join scenes s     on s.id = a.established_in_scene;

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
  )
  -- one note per (subject, thread): promised+goal of the same handle dedupe
  and a.id = (
    select min(a3.id) from assertions a3
    where a3.subject_id = a.subject_id
      and a3.predicate in ('promised','goal','secret_of')
      and upper_inf(a3.valid_during)
      and a3.status not in ('rejected','retconned')
      and regexp_replace(lower(a3.object_value), '[^a-z0-9 ]', '', 'g')
        = regexp_replace(lower(a.object_value),  '[^a-z0-9 ]', '', 'g')
  );

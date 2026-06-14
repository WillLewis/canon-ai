-- Canon AI — hole-finder queries (Phase 1, SPEC R8)
-- "Questions your world doc doesn't answer." Each query SELECTs:
--   (category, question, detail, scene_id)
-- Deterministic gap-finding over the assertion graph — NOT story generation
-- (docs/decisions.md D1): it asks diagnostic questions, it never answers them.
-- Sibling of db/checks.sql (violations); this file is gaps. The unsourced /
-- open-thread logic intentionally mirrors checks.sql checks 3 & 7 — keep both in
-- sync if that logic is tuned. Convention: :world_id; ignore rejected/retconned.

-- ============================================================
-- HOLE 1 · unknown_entity — a provisional entity (referenced, never established)
-- ============================================================
select
  'unknown_entity'                                              as category,
  format('Who or what is "%s"?', e.name)                       as question,
  format('Referenced in the script (first at pos %s) but never appears or gets '
         'established in canon.', s.story_position)             as detail,
  a.established_in_scene                                        as scene_id
from (
  select e2.id as eid, min(a2.id) as first_aid
  from assertions a2
  join entities e2 on e2.id in (a2.subject_id, a2.object_id) and e2.provisional
  where a2.world_id = :world_id and a2.status not in ('rejected','retconned')
  group by e2.id
) firsts
join assertions a on a.id = firsts.first_aid
join entities e   on e.id = firsts.eid
join scenes s     on s.id = a.established_in_scene;

-- ============================================================
-- HOLE 2 · open_thread — a promise/goal/secret set up and never paid off
-- (mirrors checks.sql CHECK 7 idle_setup, framed as a question)
-- ============================================================
select
  'open_thread',
  format('Does %s ever follow through on "%s"?', e.name, a.object_value),
  format('Set up at pos %s, with no later scene touching it.', lower(a.valid_during)),
  a.established_in_scene
from assertions a
join entities e on e.id = a.subject_id
where a.world_id = :world_id
  and a.predicate in ('promised','goal','secret_of')
  and upper_inf(a.valid_during)
  and a.status not in ('rejected','retconned')
  and not exists (
    select 1 from assertions later
    join scenes ls   on ls.id = later.established_in_scene
    join scenes asrc on asrc.id = a.established_in_scene
    where later.subject_id = a.subject_id
      and later.object_value = a.object_value
      and later.id <> a.id
      and ls.story_position > asrc.story_position
  )
  and a.id = (
    select min(a3.id) from assertions a3
    where a3.subject_id = a.subject_id
      and a3.predicate in ('promised','goal','secret_of')
      and upper_inf(a3.valid_during)
      and a3.status not in ('rejected','retconned')
      and regexp_replace(lower(a3.object_value), '[^a-z0-9 ]', '', 'g')
        = regexp_replace(lower(a.object_value),  '[^a-z0-9 ]', '', 'g')
  );

-- ============================================================
-- HOLE 3 · unsourced_knowledge — a character knows a fact with no on-screen
-- source (mirrors checks.sql CHECK 3 premature_knowledge, framed as a question)
-- ============================================================
select
  'unsourced_knowledge',
  format('How does %s know "%s"?', e.name, k.object_value),
  format('They act on it at pos %s, but canon never shows them learning it — no '
         'scene where they discover it or are told.',
         coalesce(lower(k.valid_during), s_use.story_position)),
  k.established_in_scene
from assertions k
join entities e   on e.id = k.subject_id
join scenes s_use on s_use.id = k.established_in_scene
where k.world_id = :world_id
  and k.predicate = 'knows'
  and k.object_value is not null
  and k.status not in ('rejected','retconned')
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
-- HOLE 4 · unconfirmed_belief — someone believes X, but canon never establishes
-- whether X is true (no character ever `knows` the same fact)
-- ============================================================
-- handle is quoted (it's a paraphrased belief, often a fragment) so the question
-- reads cleanly regardless of how extraction phrased it
select
  'unconfirmed_belief',
  format('%s believes "%s" — does canon ever confirm it?', e.name, b.object_value),
  format('Stated as belief at pos %s, but no character is ever shown to know it '
         'for certain.', coalesce(lower(b.valid_during), s_use.story_position)),
  b.established_in_scene
from assertions b
join entities e   on e.id = b.subject_id
join scenes s_use on s_use.id = b.established_in_scene
where b.world_id = :world_id
  and b.predicate = 'believes'
  and b.object_value is not null
  and b.status not in ('rejected','retconned')
  and not exists (
    select 1 from assertions k
    where k.world_id = b.world_id
      and k.predicate = 'knows'
      and regexp_replace(lower(k.object_value), '[^a-z0-9 ]', '', 'g')
        = regexp_replace(lower(b.object_value), '[^a-z0-9 ]', '', 'g')
      and k.status not in ('rejected','retconned')
  )
  and b.id = (
    select min(b2.id) from assertions b2
    where b2.world_id = b.world_id
      and b2.predicate = 'believes'
      and b2.subject_id = b.subject_id
      and regexp_replace(lower(b2.object_value), '[^a-z0-9 ]', '', 'g')
        = regexp_replace(lower(b.object_value),  '[^a-z0-9 ]', '', 'g')
      and b2.status not in ('rejected','retconned')
  );

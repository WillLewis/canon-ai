"""Store / load — resolved assertions + entities into Postgres (db/schema.sql).

The pipeline's `... --> Postgres` arrow. Consumes a resolution state file (from
`canon resolve`) and loads, for a world whose scenes already exist (from
`canon ingest`):

  entities, aliases, scene_presence, assertions, character_locations

Key transforms done here (not in earlier stages):
  - valid_during: story_position + starts_here/ends_here -> int4range. Default
    [P, ) (open-ended from this scene); ends_here -> [P, P+1); backdated
    (starts_here false) -> open lower bound (NULL,) per docs/extraction.md.
  - object synthesis: db/schema.sql requires every assertion to have an object;
    intransitive predicates (dies/destroyed/alive) carry none, so we synthesize
    object_value = predicate to satisfy the CHECK (value is irrelevant to checks).
  - scene_presence: the extraction lists CHARACTERS; the destroyed_location_use
    check also needs the scene's setting LOCATION present, so we resolve each
    scene's slug to a location entity and add it.
  - character_locations: mirror located_at(character -> location) assertions into
    the typed table whose exclusion constraint enforces one-place-per-interval.

status (v0 gate): confidence >= conf_canon (default 0.85) -> 'canon', else
'draft', so the check layer has a populated canon graph. extraction.md's stricter
draft -> human-confirm -> canon promotion is a later refinement.

Driver-agnostic (psycopg2/psycopg3 cursor subset) and fake-connection testable;
not run against a live database in this environment (run after `supabase start`).
"""

from __future__ import annotations

import re

from . import resolve

CONF_CANON = 0.85
INTRANSITIVE = resolve.INTRANSITIVE if hasattr(resolve, "INTRANSITIVE") else frozenset(
    {"alive", "dies", "destroyed"}
)
# Point-in-time state-change events: they HAPPEN at the scene that establishes them, so a
# backdated (starts_here=False) one must still anchor its lower bound there — otherwise it
# stores as '(,)' and the "after the event" checks can never compare `pos > lower(...)`.
EVENT_PREDICATES = frozenset({"dies", "destroyed"})

_SLUG_PREFIX_RE = re.compile(r"^\s*(INT\.?/EXT\.?|EXT\.?/INT\.?|INT|EXT|EST|I/E|E/I)[.\s]+", re.I)
_SLUG_TIME_SEPS = (" - ", " — ", " – ")


# ---------------------------------------------------------------------------
# Pure transforms
# ---------------------------------------------------------------------------

def valid_range(story_position: int, starts_here: bool, ends_here: bool) -> tuple:
    """(lower, upper) for an int4range '[lower, upper)'. None = unbounded."""
    lower = story_position if starts_here else None
    upper = (story_position + 1) if ends_here else None
    return lower, upper


def synth_object_value(predicate: str, object_id, object_value):
    """Satisfy schema's object CHECK: intransitive predicates get a placeholder."""
    if object_id is not None or (object_value not in (None, "")):
        return object_value
    return predicate  # dies/destroyed/alive -> object_value = predicate


def status_for(confidence, confirmed: bool, conf_canon: float = CONF_CANON) -> str:
    if confirmed:
        return "canon"
    try:
        return "canon" if float(confidence) >= conf_canon else "draft"
    except (TypeError, ValueError):
        return "draft"


def slug_location(slug: str) -> str:
    """'INT. CHAPEL ON THE POINT - DAY' -> 'CHAPEL ON THE POINT'."""
    s = _SLUG_PREFIX_RE.sub("", slug or "").strip()
    for sep in _SLUG_TIME_SEPS:
        if sep in s:
            s = s.split(sep)[0]
    return s.strip()


# ---------------------------------------------------------------------------
# Loader
# ---------------------------------------------------------------------------

def _delete_world_graph(cur, world_id: int) -> None:
    """FK-safe teardown of a world's resolved graph (keeps worlds/works/scenes)."""
    cur.execute("DELETE FROM findings WHERE world_id = %s", (world_id,))
    cur.execute("DELETE FROM assertions WHERE world_id = %s", (world_id,))  # cascades character_locations
    cur.execute(
        "DELETE FROM scene_presence WHERE scene_id IN "
        "(SELECT s.id FROM scenes s JOIN works w ON w.id = s.work_id WHERE w.world_id = %s)",
        (world_id,),
    )
    cur.execute("DELETE FROM entities WHERE world_id = %s", (world_id,))  # cascades aliases


def store_state(conn, world_name: str, state_dict: dict, *, reset: bool = False,
                conf_canon: float = CONF_CANON) -> dict:
    state = resolve.state_from_dict(state_dict)
    reg = state.registry
    cur = conn.cursor()

    cur.execute("SELECT id FROM worlds WHERE name = %s", (world_name,))
    row = cur.fetchone()
    if not row:
        raise RuntimeError(f"world '{world_name}' not found — run `canon ingest` first.")
    world_id = row[0]

    cur.execute(
        "SELECT s.id, s.story_position, s.slug FROM scenes s "
        "JOIN works w ON w.id = s.work_id WHERE w.world_id = %s",
        (world_id,),
    )
    scene_rows = cur.fetchall()
    if not scene_rows:
        raise RuntimeError(f"world '{world_name}' has no scenes — run `canon ingest` first.")
    scene_id_by_pos = {pos: sid for sid, pos, _slug in scene_rows}

    if reset:
        _delete_world_graph(cur, world_id)

    # --- entities + aliases ---
    db_id: dict[int, int] = {}              # local_id -> db id
    by_norm: dict[str, tuple] = {}          # norm(name|alias) -> (db id, kind)
    n_aliases = 0
    for e in reg.entities:
        cur.execute(
            "INSERT INTO entities (world_id, kind, name, dossier, provisional) "
            "VALUES (%s, %s, %s, %s, %s) RETURNING id",
            (world_id, e.kind, e.name, e.dossier, e.provisional),
        )
        eid = cur.fetchone()[0]
        db_id[e.local_id] = eid
        by_norm[resolve._norm(e.name)] = (eid, e.kind)
        for alias, kind in e.aliases:
            by_norm.setdefault(resolve._norm(alias), (eid, e.kind))
            cur.execute(
                "INSERT INTO aliases (entity_id, alias, kind) VALUES (%s, %s, %s)",
                (eid, alias, kind),
            )
            n_aliases += 1

    # --- scene_presence: characters (from extraction) + setting location (slug) ---
    seen_presence: set[tuple] = set()
    n_presence = 0

    def add_presence(scene_id, entity_id):
        nonlocal n_presence
        key = (scene_id, entity_id)
        if scene_id and entity_id and key not in seen_presence:
            seen_presence.add(key)
            cur.execute(
                "INSERT INTO scene_presence (scene_id, entity_id) VALUES (%s, %s)",
                (scene_id, entity_id),
            )
            n_presence += 1

    for p in state.scene_presence:
        sid = scene_id_by_pos.get(p.get("story_position"))
        for name in p.get("entities") or []:
            hit = by_norm.get(resolve._norm(name))
            if hit:
                add_presence(sid, hit[0])

    for sid, pos, slug in scene_rows:
        status, ent = reg.match(slug_location(slug))
        if status in ("exact", "fuzzy") and ent is not None and ent.kind == "location":
            add_presence(sid, db_id.get(ent.local_id))

    # --- assertions (+ character_locations sync) ---
    # Scene-by-scene extraction emits located_at with open-ended intervals; a
    # subject moving to a new place CLOSES their previous open interval at the
    # new position ([1,) + move at 2 -> [1,2) + [2,)). Without this, successive
    # locations overlap and the exclusion constraint rejects ordinary movement.
    # A same-position pair (two places at once) is left open deliberately: the
    # character_locations sync is skipped for it and the presence_conflict scan
    # reports it as a finding instead of aborting the load.
    open_located: dict = {}  # subject_db_id -> (assertion_db_id, lower_bound, location_db_id)
    last_loc_start: dict = {}  # (subject_db_id, location_db_id) -> most recent anchored lower
    seen_events: set = set()   # (subject_db_id, predicate) — dedup redundant dies/destroyed
    n_assertions = n_char_loc = n_skipped = n_deduped = 0
    # Process in story-position order. The movement-closing logic, the located_at anchor,
    # and the event dedup ("keep the EARLIEST dies/destroyed") all require it — keeping the
    # earliest is only correct iterating earliest-first, and closing prior intervals needs a
    # non-decreasing position. Do not rely on the caller pre-sorting. Stable, so same-position
    # rows (e.g. a two-places-at-once conflict) keep their relative order.
    ordered = sorted(state.assertions,
                     key=lambda x: x.get("story_position") if x.get("story_position") is not None else (1 << 30))
    for a in ordered:
        subj = by_norm.get(resolve._norm(a.get("subject")))
        scene_id = scene_id_by_pos.get(a.get("story_position"))
        if subj is None or scene_id is None:
            n_skipped += 1
            continue
        obj = by_norm.get(resolve._norm(a["object_entity"])) if a.get("object_entity") else None
        obj_id = obj[0] if obj else None
        predicate = a.get("predicate")
        # A subject dies / is destroyed once. Extraction often re-states it later (a backdated
        # reference); storing that as a second event would double every "after the event"
        # finding for scenes past it. Keep the earliest (first in story order — also the one
        # dead_speaker needs to catch the in-between appearances) and drop the redundant rest.
        if predicate in EVENT_PREDICATES and (subj[0], predicate) in seen_events:
            n_deduped += 1
            continue
        object_value = synth_object_value(predicate, obj_id, a.get("object_value"))
        lower, upper = valid_range(a.get("story_position"), a.get("starts_here", True),
                                   a.get("ends_here", False))
        # A backdated point-in-time event (dies/destroyed) loses its anchor as '(,)', which
        # makes destroyed_location_use / dead_speaker unable to compare "scene after the
        # event". Anchor it to this establishing scene. Conservative: only enables flagging
        # scenes strictly AFTER the establishing scene, so it adds no false positives. See P10.
        if lower is None and predicate in EVENT_PREDICATES:
            lower = a.get("story_position")
        confidence = max(0.0, min(1.0, float(a.get("confidence") or 0.0)))
        status = status_for(a.get("confidence"), a.get("confirmed_by_human", False), conf_canon)

        sync_ok = True
        if predicate == "located_at":
            # Backdated continuation: a located_at with an OPEN lower bound but a CONCRETE
            # upper bound is a *duration* claim ("at L until P") — usually a restatement of
            # a stay established earlier (extraction marks the restatement starts_here=False).
            # Anchor its lower bound to the subject's most recent start at the SAME location,
            # so it becomes a bounded interval the presence_conflict scan can reason about
            # instead of an open-lower interval the scan must ignore (its lower_inf guard;
            # relaxing that guard instead would flag the backdated stay against EVERY prior
            # location). Without anchoring, 'locked in L until dawn' while impossibly
            # appearing elsewhere is structurally unflaggable. See answer-key P7.
            if lower is None and upper is not None and obj_id is not None:
                anchor = last_loc_start.get((subj[0], obj_id))
                if anchor is not None and anchor < upper:
                    lower = anchor
            if lower is not None and obj_id is not None:
                last_loc_start[(subj[0], obj_id)] = lower

            prev = open_located.get(subj[0])
            if prev is not None and obj_id is not None and obj_id == prev[2]:
                # re-assertion of the SAME location while its interval is still
                # open ("the ledger is still in the chapel") — a reaffirmation,
                # not a move; storing a duplicate open row would only feed the
                # presence_conflict scan false positives. Skip it.
                n_deduped += 1
                continue
            if prev is not None:
                prev_aid, prev_lower = prev[0], prev[1]
                # A backdated previous interval (open lower bound, prev_lower None)
                # closes like any other: (,) + move at P -> (,P).
                can_close = lower is not None and (prev_lower is None or lower > prev_lower)
                if can_close:
                    # close the previous open interval at the new position
                    cur.execute(
                        "UPDATE assertions SET valid_during = int4range(%s, %s) WHERE id = %s",
                        (prev_lower, lower, prev_aid),
                    )
                    cur.execute(
                        "UPDATE character_locations SET valid_during = int4range(%s, %s) "
                        "WHERE assertion_id = %s",
                        (prev_lower, lower, prev_aid),
                    )
                else:
                    # same position (or both backdated): genuine conflict — leave the
                    # scan check to flag it; don't fight the exclusion constraint
                    sync_ok = False

        cur.execute(
            "INSERT INTO assertions "
            "(world_id, subject_id, predicate, object_id, object_value, polarity, "
            " valid_during, established_in_scene, supporting_quote, confidence, status) "
            "VALUES (%s, %s, %s, %s, %s, %s, int4range(%s, %s), %s, %s, %s, %s) RETURNING id",
            (world_id, subj[0], predicate, obj_id, object_value, bool(a.get("polarity", True)),
             lower, upper, scene_id, a.get("supporting_quote"), confidence, status),
        )
        aid = cur.fetchone()[0]
        n_assertions += 1
        if predicate in EVENT_PREDICATES:
            seen_events.add((subj[0], predicate))

        if predicate == "located_at" and upper is None and sync_ok:
            # backdated rows (lower None) are tracked too, so (,) gets closed by
            # the next move; conflicting (sync_ok=False) rows are not tracked —
            # the previously synced interval must stay the one the next move closes
            open_located[subj[0]] = (aid, lower, obj_id)

        if (predicate == "located_at" and sync_ok and subj[1] == "character"
                and obj is not None and obj[1] == "location"):
            cur.execute(
                "INSERT INTO character_locations "
                "(assertion_id, character_id, location_id, valid_during) "
                "VALUES (%s, %s, %s, int4range(%s, %s))",
                (aid, subj[0], obj_id, lower, upper),
            )
            n_char_loc += 1

    conn.commit()
    return {
        "world_id": world_id, "entities": len(reg.entities), "aliases": n_aliases,
        "scene_presence": n_presence, "assertions": n_assertions,
        "character_locations": n_char_loc, "skipped": n_skipped, "deduped": n_deduped,
    }


def render_dry_run(state_dict: dict, world_name: str, conf_canon: float = CONF_CANON) -> str:
    """Offline summary of what would load (scene linkage/locations resolved live)."""
    state = resolve.state_from_dict(state_dict)
    n_prov = sum(1 for e in state.registry.entities if e.provisional)
    canon = sum(1 for a in state.assertions
                if status_for(a.get("confidence"), a.get("confirmed_by_human", False), conf_canon) == "canon")
    n_alias = sum(len(e.aliases) for e in state.registry.entities)
    char_presence = sum(len(p.get("entities") or []) for p in state.scene_presence)
    return "\n".join([
        f"world: {world_name}   (load requires its scenes from `canon ingest`)",
        f"entities: {len(state.registry.entities)} ({n_prov} provisional)  + {n_alias} alias(es)",
        f"assertions: {len(state.assertions)}  ({canon} canon / {len(state.assertions) - canon} draft "
        f"at conf>={conf_canon})",
        f"scene_presence (characters): {char_presence}  "
        f"(+ setting-location presence resolved against the scenes table at load)",
        "",
        "no database writes (dry run).",
    ])

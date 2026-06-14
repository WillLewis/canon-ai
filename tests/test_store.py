"""Store/load tests — all offline.

Pure transforms (valid_during ranges, object synthesis, status gate, slug ->
location) plus the full loader against a fake DB connection (records SQL +
params). Not run against a live Postgres in this environment.

    python -m pytest -q
    python tests/test_store.py
"""

import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from canon import store  # noqa: E402


# --- fake connection -------------------------------------------------------

class FakeCursor:
    def __init__(self, world_id, scenes):
        self.world_id = world_id          # None -> world not found
        self.scenes = scenes              # list of (id, story_position, slug)
        self.calls = []
        self._eid = 0
        self._aid = 0
        self._last = None

    def execute(self, sql, params=None):
        self.calls.append((sql, params))
        s = sql.lower()
        if s.startswith("select id from worlds"):
            self._last = "world"
        elif "from scenes s" in s and s.startswith("select"):
            self._last = "scenes"
        elif s.startswith("insert into entities") and "returning" in s:
            self._eid += 1
            self._last = ("eid", self._eid)
        elif s.startswith("insert into assertions") and "returning" in s:
            self._aid += 1
            self._last = ("aid", self._aid)
        else:
            self._last = None

    def fetchone(self):
        if self._last == "world":
            return (self.world_id,) if self.world_id is not None else None
        if isinstance(self._last, tuple):
            return (self._last[1],)
        return None

    def fetchall(self):
        return list(self.scenes) if self._last == "scenes" else []


class FakeConn:
    def __init__(self, world_id=7, scenes=None):
        self.cur = FakeCursor(world_id, scenes or [])
        self.committed = False

    def cursor(self):
        return self.cur

    def commit(self):
        self.committed = True

    def close(self):
        pass


# --- fixtures --------------------------------------------------------------

def _entity(lid, name, kind, aliases):
    return {"local_id": lid, "name": name, "kind": kind, "dossier": None,
            "provisional": False,
            "aliases": [{"alias": a, "kind": "name_variant"} for a in aliases]}


def _assn(subject, predicate, pos, *, object_entity=None, object_value=None,
          starts_here=True, ends_here=False, confidence=0.95):
    return {"subject": subject, "predicate": predicate, "object_entity": object_entity,
            "object_value": object_value, "polarity": True, "starts_here": starts_here,
            "ends_here": ends_here, "supporting_quote": "q", "confidence": confidence,
            "scene": f"E/sc{pos}", "story_position": pos}


def _state():
    return {
        "world": "greyharbor",
        "entities": [
            _entity(1, "Mara", "character", ["Mara"]),
            _entity(2, "Tobias", "character", ["Tobias"]),
            _entity(3, "Cole", "character", ["Cole"]),
            _entity(4, "Chapel on the Point", "location", ["Chapel on the Point", "chapel"]),
            _entity(5, "Leather Ledger", "object", ["Leather Ledger", "ledger"]),
            _entity(6, "Harbormaster's Office", "location", ["Harbormaster's Office"]),
        ],
        "assertions": [
            _assn("Mara", "cannot", 1, object_value="drive"),
            _assn("Tobias", "dies", 5),                                  # intransitive
            _assn("Chapel on the Point", "destroyed", 6),               # intransitive
            _assn("Leather Ledger", "located_at", 3, object_entity="chapel"),   # object @ location
            _assn("Mara", "located_at", 8, object_entity="chapel"),     # CHARACTER @ location
        ],
        "scene_presence": [
            {"scene": "E/sc1", "story_position": 1, "entities": ["Mara", "Tobias"]},
            {"scene": "E/sc9", "story_position": 9, "entities": ["Tobias"]},  # dead Tobias present
        ],
        "queue": [], "merges": [],
    }


_SCENES = [
    (101, 1, "INT. HARBORMASTER'S OFFICE - DAY"),
    (103, 3, "INT. CHAPEL ON THE POINT - NIGHT"),
    (105, 5, "INT. HARBORMASTER'S OFFICE - NIGHT"),
    (106, 6, "EXT. CHAPEL ON THE POINT - NIGHT"),
    (108, 8, "INT. CHAPEL ON THE POINT - DAY"),
    (109, 9, "INT. HARBORMASTER'S OFFICE - DAY"),
]


def _calls(cur, prefix):
    return [c for c in cur.calls if c[0].lower().startswith(prefix)]


# --- pure transforms -------------------------------------------------------

def test_valid_range():
    assert store.valid_range(5, True, False) == (5, None)     # open-ended [5,)
    assert store.valid_range(5, True, True) == (5, 6)          # [5,6) — just pos 5
    assert store.valid_range(5, False, False) == (None, None)  # backdated, open both ways


def test_synth_object_value():
    assert store.synth_object_value("dies", None, None) == "dies"
    assert store.synth_object_value("destroyed", None, "") == "destroyed"
    assert store.synth_object_value("located_at", 9, None) is None      # has object_id
    assert store.synth_object_value("cannot", None, "drive") == "drive"


def test_status_gate():
    assert store.status_for(0.95, False) == "canon"
    assert store.status_for(0.5, False) == "draft"
    assert store.status_for(0.1, True) == "canon"              # human-confirmed
    assert store.status_for(None, False) == "draft"


def test_slug_location():
    assert store.slug_location("INT. CHAPEL ON THE POINT - DAY") == "CHAPEL ON THE POINT"
    assert store.slug_location("EXT. GREYHARBOR DOCKS - NIGHT") == "GREYHARBOR DOCKS"
    assert store.slug_location("INT./EXT. CAR - MOVING") == "CAR"


# --- loader ----------------------------------------------------------------

def test_world_not_found_raises():
    conn = FakeConn(world_id=None, scenes=_SCENES)
    try:
        store.store_state(conn, "nope", _state())
    except RuntimeError as e:
        assert "ingest" in str(e)
    else:
        raise AssertionError("expected RuntimeError when world is missing")


def test_loader_inserts_entities_aliases_and_counts():
    conn = FakeConn(world_id=7, scenes=_SCENES)
    r = store.store_state(conn, "greyharbor", _state())
    assert conn.committed
    assert r["world_id"] == 7
    assert r["entities"] == 6
    assert len(_calls(conn.cur, "insert into entities")) == 6
    # Mara/Tobias/Cole/Office 1 alias each, Chapel/Ledger 2 each = 8
    assert r["aliases"] == 8 and len(_calls(conn.cur, "insert into aliases")) == 8
    assert r["assertions"] == 5 and r["skipped"] == 0


def test_scene_presence_includes_characters_and_setting_location():
    conn = FakeConn(world_id=7, scenes=_SCENES)
    r = store.store_state(conn, "greyharbor", _state())
    sp = _calls(conn.cur, "insert into scene_presence")
    pairs = {(p[1][0], p[1][1]) for p in sp}
    # characters: Mara(=eid1),Tobias(=eid2) at scene 101; Tobias at scene 109
    assert (101, 1) in pairs and (101, 2) in pairs and (109, 2) in pairs
    # setting locations resolved from slugs: chapel(=eid4) at 103/106/108,
    # office(=eid6) at 101/105/109
    assert (103, 4) in pairs and (106, 4) in pairs and (108, 4) in pairs
    assert (101, 6) in pairs and (105, 6) in pairs and (109, 6) in pairs
    assert r["scene_presence"] == 9


def test_assertion_ranges_objects_and_status():
    conn = FakeConn(world_id=7, scenes=_SCENES)
    store.store_state(conn, "greyharbor", _state())
    ins = _calls(conn.cur, "insert into assertions")
    # param order: world,subj,pred,obj_id,obj_val,polarity,lower,upper,scene,quote,conf,status
    by_pred = {c[1][2]: c[1] for c in ins}
    dies = by_pred["dies"]
    assert dies[6] == 5 and dies[7] is None          # [5,)
    assert dies[4] == "dies"                           # synthesized object_value
    assert dies[8] == 105                              # established_in_scene (pos5 -> scene 105)
    assert dies[11] == "canon"                         # conf 0.95 >= 0.85
    destroyed = by_pred["destroyed"]
    assert destroyed[6] == 6 and destroyed[4] == "destroyed"
    cannot = by_pred["cannot"]
    assert cannot[6] == 1 and cannot[4] == "drive"


def test_character_locations_synced_only_for_character_subjects():
    conn = FakeConn(world_id=7, scenes=_SCENES)
    r = store.store_state(conn, "greyharbor", _state())
    cl = _calls(conn.cur, "insert into character_locations")
    # Mara located_at chapel -> synced; ledger located_at chapel -> NOT (object subject)
    assert r["character_locations"] == 1 and len(cl) == 1
    assert cl[0][1][1] == 1                            # character_id = Mara (eid1)
    assert cl[0][1][2] == 4                            # location_id = chapel (eid4)


def test_reset_tears_down_world_graph_first():
    conn = FakeConn(world_id=7, scenes=_SCENES)
    store.store_state(conn, "greyharbor", _state(), reset=True)
    deletes = [c[0].lower() for c in conn.cur.calls if c[0].lower().startswith("delete")]
    assert deletes[0].startswith("delete from findings")
    assert any("from assertions" in d for d in deletes)
    assert any("from scene_presence" in d for d in deletes)
    assert any("from entities" in d for d in deletes)


def test_located_at_movement_closes_previous_open_interval():
    state = _state()
    state["assertions"] = [
        _assn("Mara", "located_at", 1, object_entity="Harbormaster's Office"),
        _assn("Mara", "located_at", 3, object_entity="Chapel on the Point"),   # move
        _assn("Mara", "located_at", 8, object_entity="Harbormaster's Office"),  # move back
    ]
    conn = FakeConn(world_id=7, scenes=_SCENES)
    r = store.store_state(conn, "greyharbor", state)
    updates = [c for c in conn.cur.calls if c[0].startswith("UPDATE assertions")]
    # each later location closed the previous open interval at its position
    assert [(u[1][0], u[1][1]) for u in updates] == [(1, 3), (3, 8)]
    cl_updates = [c for c in conn.cur.calls if c[0].startswith("UPDATE character_locations")]
    assert [(u[1][0], u[1][1]) for u in cl_updates] == [(1, 3), (3, 8)]
    assert r["character_locations"] == 3


def test_backdated_located_at_closed_by_next_move():
    state = _state()
    state["assertions"] = [
        # backdated: ledger has been in the chapel since before the story -> (,)
        _assn("Leather Ledger", "located_at", 3, object_entity="Chapel on the Point",
              starts_here=False),
        # taken to the office at pos 8 -> previous must close to (,8)
        _assn("Leather Ledger", "located_at", 8, object_entity="Harbormaster's Office"),
    ]
    conn = FakeConn(world_id=7, scenes=_SCENES)
    store.store_state(conn, "greyharbor", state)
    updates = [c for c in conn.cur.calls if c[0].startswith("UPDATE assertions")]
    assert [(u[1][0], u[1][1]) for u in updates] == [(None, 8)]   # (,) -> (,8)


def test_located_at_same_position_conflict_skips_sync_not_load():
    state = _state()
    state["assertions"] = [
        _assn("Mara", "located_at", 3, object_entity="Chapel on the Point"),
        _assn("Mara", "located_at", 3, object_entity="Harbormaster's Office"),  # two places at pos 3
        _assn("Mara", "located_at", 8, object_entity="Harbormaster's Office"),  # later move
    ]
    conn = FakeConn(world_id=7, scenes=_SCENES)
    r = store.store_state(conn, "greyharbor", state)
    assert r["assertions"] == 3                 # all loaded — scan check flags the conflict
    assert r["character_locations"] == 2        # conflicting row not synced
    updates = [c for c in conn.cur.calls if c[0].startswith("UPDATE assertions")]
    assert [(u[1][0], u[1][1]) for u in updates] == [(3, 8)]  # first row closed by the pos-8 move


def test_same_location_reaffirmation_dedupes():
    state = _state()
    state["assertions"] = [
        _assn("Leather Ledger", "located_at", 3, object_entity="Chapel on the Point",
              starts_here=False),                                              # (,)
        _assn("Leather Ledger", "located_at", 8, object_entity="Chapel on the Point",
              starts_here=False),                                              # "still there" — dedupe
        _assn("Leather Ledger", "located_at", 9, object_entity="Harbormaster's Office"),  # real move
    ]
    conn = FakeConn(world_id=7, scenes=_SCENES)
    r = store.store_state(conn, "greyharbor", state)
    assert r["deduped"] == 1
    assert r["assertions"] == 2                  # reaffirmation row never inserted
    updates = [c for c in conn.cur.calls if c[0].startswith("UPDATE assertions")]
    assert [(u[1][0], u[1][1]) for u in updates] == [(None, 9)]  # (,) closed by the move


def test_skips_assertion_with_unknown_subject():
    state = _state()
    state["assertions"].append(_assn("Ghost", "knows", 3, object_value="x"))
    conn = FakeConn(world_id=7, scenes=_SCENES)
    r = store.store_state(conn, "greyharbor", state)
    assert r["skipped"] == 1
    assert r["assertions"] == 5                        # the 5 known-subject ones


def test_backdated_bounded_continuation_anchors_to_prior_same_location_start():
    # 'locked in the chapel until pos 9', restated later as a backdated + bounded claim:
    # its open lower bound is anchored to the prior chapel stay's start (pos 3) so it
    # becomes a real interval the presence_conflict scan can use. (answer-key P7 shape)
    state = _state()
    state["assertions"] = [
        _assn("Mara", "located_at", 3, object_entity="Chapel on the Point"),        # [3,)
        _assn("Mara", "located_at", 5, object_entity="Harbormaster's Office"),       # move -> chapel [3,5)
        _assn("Mara", "located_at", 8, object_entity="Chapel on the Point",
              starts_here=False, ends_here=True),                                    # (,9) -> anchored [3,9)
    ]
    conn = FakeConn(world_id=7, scenes=_SCENES)
    store.store_state(conn, "greyharbor", state)
    backdated = _calls(conn.cur, "insert into assertions")[-1][1]   # the third located_at row
    assert backdated[6] == 3 and backdated[7] == 9                  # [3,9): lower anchored, not None


def test_backdated_bounded_without_prior_same_location_stays_open():
    # no prior stay at this location -> nothing to anchor to -> lower stays open (no over-reach).
    state = _state()
    state["assertions"] = [
        _assn("Mara", "located_at", 5, object_entity="Harbormaster's Office"),       # [5,)
        _assn("Mara", "located_at", 8, object_entity="Chapel on the Point",
              starts_here=False, ends_here=True),                                    # (,9): no prior chapel
    ]
    conn = FakeConn(world_id=7, scenes=_SCENES)
    store.store_state(conn, "greyharbor", state)
    backdated = _calls(conn.cur, "insert into assertions")[-1][1]
    assert backdated[6] is None and backdated[7] == 9              # (,9): unchanged


if __name__ == "__main__":
    import traceback

    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    failed = 0
    for fn in fns:
        try:
            fn()
            print(f"PASS {fn.__name__}")
        except Exception:
            failed += 1
            print(f"FAIL {fn.__name__}")
            traceback.print_exc()
    raise SystemExit(1 if failed else 0)

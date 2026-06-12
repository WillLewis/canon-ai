"""Golden resolution state for fixtures/greyharbor.

This is the expected output of `extract` + `resolve` for the two greyharbor
episodes — what a faithful pipeline run should produce, hand-authored so the
store + check steps can be verified end-to-end without a live LLM. It contains
all 15 graded assertions (A1–A15) plus the assertions that *trigger* the planted
errors (dead Tobias present in E102, Mara driving, Cole's unsourced knowledge,
the destroyed chapel reused) and the dangling Danny reference.

Used by tests/test_check.py (DB-gated integration) and the README demo.
"""


def _entity(lid, name, kind, aliases, provisional=False):
    return {"local_id": lid, "name": name, "kind": kind, "dossier": None,
            "provisional": provisional,
            "aliases": [{"alias": a, "kind": "name_variant"} for a in aliases]}


def _a(subject, predicate, pos, *, object_entity=None, object_value=None, polarity=True,
       starts_here=True, ends_here=False, quote="", conf=0.95):
    ep = "E101" if pos <= 6 else "E102"
    scn = pos if pos <= 6 else pos - 6
    return {"subject": subject, "predicate": predicate, "object_entity": object_entity,
            "object_value": object_value, "polarity": polarity, "starts_here": starts_here,
            "ends_here": ends_here, "supporting_quote": quote, "confidence": conf,
            "scene": f"{ep}/sc{scn}", "story_position": pos}


def golden_state() -> dict:
    return {
        "world": "greyharbor",
        "entities": [
            _entity(1, "Mara Voss", "character", ["Mara Voss", "Mara"]),
            _entity(2, "Tobias Voss", "character", ["Tobias Voss", "Tobias", "uncle"]),
            _entity(3, "Edda Quinn", "character", ["Edda Quinn", "Edda"]),
            _entity(4, "Cole Brannigan", "character", ["Cole Brannigan", "Cole", "the deputy"]),
            _entity(5, "Danny Voss", "character", ["Danny Voss", "Danny"], provisional=True),
            _entity(6, "Chapel on the Point", "location", ["Chapel on the Point", "chapel"]),
            _entity(7, "Leather Ledger", "object", ["Leather Ledger", "ledger"]),
        ],
        "assertions": [
            # E101
            _a("Mara Voss", "cannot", 1, object_value="drive", quote="License is gone for good."),
            _a("Tobias Voss", "occupation", 1, object_value="harbormaster", quote="harbormaster's office"),
            _a("Edda Quinn", "knows", 2, object_value="two sets of numbers",
               quote="Your uncle keeps two sets of numbers."),
            _a("Edda Quinn", "believes", 2, object_value="real ledger not in office",
               quote="The real one's not in that office."),
            _a("Edda Quinn", "occupation", 2, object_value="harbor cook", quote="harbor cook"),
            _a("Leather Ledger", "located_at", 3, object_entity="Chapel on the Point",
               quote="slides a LEATHER LEDGER beneath it"),
            _a("Tobias Voss", "knows", 3, object_value="ledger location",
               quote="slides a LEATHER LEDGER beneath it"),
            _a("Mara Voss", "promised", 4, object_value="find Danny",
               quote="I'll find you, Danny. Whatever it takes."),
            _a("Mara Voss", "sibling_of", 4, object_entity="Danny Voss",
               quote="Mara finds her brother's skiff"),               # N2: dangling Danny
            _a("Tobias Voss", "dies", 5, quote="A gunshot."),
            _a("Chapel on the Point", "destroyed", 6, quote="The chapel burns to its stones."),
            # E102
            _a("Cole Brannigan", "occupation", 7, object_value="deputy", quote="DEPUTY COLE BRANNIGAN"),
            _a("Cole Brannigan", "knows", 7, object_value="ledger location",
               quote="It's still under the chapel floor stone where your uncle hid it."),  # P2
            _a("Mara Voss", "knows", 8, object_value="ledger location",
               quote="Mara pries up the loose floor stone."),         # learned from Cole at sc1
            _a("Mara Voss", "possesses", 8, object_entity="Leather Ledger",
               quote="The LEATHER LEDGER, intact."),
            _a("Mara Voss", "fact", 10, object_value="drive",
               quote="Mara behind the wheel of Tobias's pickup, driving hard."),  # P4 trigger
            _a("Mara Voss", "knows", 11, object_value="C.B. payment",
               quote='"C.B. -- four hundred, the shoals run."'),
        ],
        "scene_presence": [
            {"story_position": 1, "entities": ["Mara Voss", "Tobias Voss"]},
            {"story_position": 2, "entities": ["Mara Voss", "Edda Quinn"]},
            {"story_position": 3, "entities": ["Tobias Voss"]},
            {"story_position": 4, "entities": ["Mara Voss"]},
            {"story_position": 5, "entities": ["Tobias Voss"]},
            {"story_position": 6, "entities": []},
            {"story_position": 7, "entities": ["Mara Voss", "Cole Brannigan"]},
            {"story_position": 8, "entities": ["Mara Voss"]},
            {"story_position": 9, "entities": ["Tobias Voss"]},   # P1: dead Tobias present
            {"story_position": 10, "entities": ["Mara Voss"]},
            {"story_position": 11, "entities": ["Mara Voss", "Edda Quinn"]},
        ],
        "queue": [], "merges": [],
    }

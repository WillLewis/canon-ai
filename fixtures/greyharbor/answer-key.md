# Greyharbor — Answer Key

Grading harness for Phase 0. Extraction recall is scored against the expected-assertions list; check precision/recall against the planted-errors table. The `[PLANTED: ...]` bracket lines in ep102 are test annotations — the ingester should strip them (they are not script content), and extraction must NOT need them to find the errors.

## Entities (expected after resolution)

| Canonical | Kind | Aliases seen in text |
|---|---|---|
| Mara Voss | character | MARA, Mara |
| Tobias Voss | character | TOBIAS, "your uncle", "harbormaster" (role-ref) |
| Edda Quinn | character | EDDA |
| Cole Brannigan | character | COLE, "the deputy", "C.B." |
| Danny Voss | character | Danny, "her brother" — provisional (never appears, only referenced) |
| Chapel on the Point | location | chapel, "the old chapel on the point" |
| Harbormaster's Office | location | |
| Greyharbor Docks | location | |
| Leather Ledger | object | "the real ledger", "Thursday numbers", "that book" |
| Danny-Lee | object | skiff |

## Expected core assertions (extraction recall ≥80% of these)

1. `cannot(Mara, "drive")` — established E101/sc1 ("you know I can't. License is gone for good."). Open-ended interval.
2. `dies(Tobias)` — E101/sc5 (gunshot). 
3. `located_at(Ledger, Chapel)` — E101/sc3 (under the floor stone).
4. `knows(Tobias, ledger location)` — E101/sc3 (he hid it).
5. `knows(Edda, "Tobias keeps two sets of numbers")` — E101/sc2.
6. `believes(Edda, "real ledger not in office")` — E101/sc2 (stated, uncorroborated at that point).
7. `destroyed(Chapel)` — E101/sc6 (burns to its stones). Open-ended interval.
8. `promised(Mara, "find Danny")` — E101/sc4. Open-ended.
9. `knows(Mara, ledger location)` — E102/sc1 (Cole tells her) / acted on in E102/sc2.
10. `knows(Cole, ledger location)` — E102/sc1. **Canon start should resolve to E102/sc1 with no earlier source — see planted error P2.**
11. `possesses(Mara, Ledger)` — from E102/sc2 onward.
12. `knows(Mara, "C.B. payment in ledger")` — E102/sc5.
13. `occupation(Cole, "deputy")`; `occupation(Tobias, "harbormaster")`; `occupation(Edda, "harbor cook")`.
14. Scene presence per scene (used by checks 1 & 4).

## Planted errors (checker must find ALL)

| # | Check | Where | Ground truth violated |
|---|---|---|---|
| P1 | `dead_speaker` | E102/sc3 (office) | Tobias speaks/acts; died E101/sc5; scene not marked flashback |
| P2 | `premature_knowledge` | E102/sc1 (docks) | Cole states the ledger's hiding place with no canon `knows` interval covering that position (his knowledge has no established source — the dramatic tell, but also a canon violation as written since the reveal of HOW he knows never lands in these two eps) |
| P3 | `destroyed_location_use` | E102/sc2 (chapel) | Chapel destroyed E101/sc6; scene set there afterward, not a flashback |
| P4 | `capability_violation` | E102/sc4 (coast road) | Mara drives; `cannot(Mara, drive)` open interval from E101/sc1 |

## Expected notes (not errors — should appear at `note` severity)

- `idle_setup`: `promised(Mara, "find Danny")` — never touched after E101/sc4.
- `dangling_reference`: Danny Voss — referenced, never established.

## False-positive budget

≤2 per episode. Known traps the checker must NOT flag:
- Mara repeating Tobias's line "Forgive me the arithmetic" (E102/sc2) is an echo, not a knowledge violation — she's reading the room he left, and she plausibly never heard him say it; if extraction emits `knows(Mara, Tobias's words)`, that's an extraction error, not a check error.
- Edda's E101 claims are `believes`, not `knows` — extracting them as world-facts will cascade into spurious flags.
- Tobias appearing in E101 scenes before his death is obviously fine; intervals must be position-correct.

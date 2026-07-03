# Greyharbor — Answer Key

Grading harness for Phase 0. Extraction recall is scored against the expected-assertions list; check precision/recall against the planted-errors table. The `[PLANTED: ...]` bracket lines in ep102 are legacy test annotations — the ingester should strip them (they are not script content), and extraction must NOT need them to find the errors. Held-out episodes E103-E108 are clean scripts with no inline annotations or error-disclosure credit; their locators live only in this answer key and `eval/expected_greyharbor_s1.json`.

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

| # | Check | Where | Line | Ground truth violated |
|---|---|---|---|---|
| P1 | `dead_speaker` | E102/sc3 (office) | `Now you've counted everything. Happy?` | Tobias speaks/acts; died E101/sc5; scene not marked flashback |
| P2 | `premature_knowledge` | E102/sc1 (docks) | `Whoever it was, they didn't find the real ledger. It's still under the chapel floor stone where your uncle hid it.` | Cole states the ledger's hiding place with no canon `knows` interval covering that position (his knowledge has no established source — the dramatic tell, but also a canon violation as written since the reveal of HOW he knows never lands in these two eps) |
| P3 | `destroyed_location_use` | E102/sc2 (chapel) | `Mara pries up the loose floor stone. The LEATHER LEDGER, intact. She flips pages: names, weights, payments.` | Chapel destroyed E101/sc6; scene set there afterward, not a flashback |
| P4 | `capability_violation` | E102/sc4 (coast road) | `Mara behind the wheel of Tobias's pickup, driving hard along the cliff road, ledger on the passenger seat.` | Mara drives; `cannot(Mara, drive)` open interval from E101/sc1 |

## Expected notes (not errors — should appear at `note` severity)

- `idle_setup`: `promised(Mara, "find Danny")` — E101/sc4 line: `I'll find you, Danny. Whatever it takes.` Never touched after E101/sc4 in the two-episode key.
- `dangling_reference`: Danny Voss — E101/sc4 line: `Mara finds her brother's skiff, the DANNY-LEE, tied off and empty.` Referenced, never established in the two-episode key.

## False-positive budget

≤2 per episode. Known traps the checker must NOT flag:
- T1: Mara repeating Tobias's line `Forgive me the arithmetic.` (E102/sc2) is an echo, not a knowledge violation — she's reading the room he left, and she plausibly never heard him say it; if extraction emits `knows(Mara, Tobias's words)`, that's an extraction error, not a check error.
- T2: Edda's E101/sc2 line `Your uncle keeps two sets of numbers. The real one's not in that office.` is `believes`, not `knows` — extracting it as a world-fact will cascade into spurious flags.
- Tobias appearing in E101 scenes before his death is obviously fine; intervals must be position-correct.

## Coverage notes

Reader's Report coverage scoring uses this table in addition to planted continuity errors.
Coverage notes are grounded-subjective: candidates must come from the graph, the LLM may
decline or phrase them, and the validator must drop notes with bad citations.

### Planted coverage items

| # | Family | Where | Line | Must mention |
|---|---|---|---|---|
| C1 | F1 open_question | E101/sc4 | `Mara finds her brother's skiff, the DANNY-LEE, tied off and empty.` | Danny Voss |
| C2 | F2 idle_setup | E101/sc4 | `I'll find you, Danny. Whatever it takes.` | find Danny |
| C3 | F3 dormant_knowledge | E102/sc1 | `It's still under the chapel floor stone where your uncle hid it.` | Cole, ledger location |
| C4 | F4 unmotivated_turn | E102/sc4 | `Mara behind the wheel of Tobias's pickup, driving hard along the cliff road, ledger on the passenger seat.` | Mara, drive |

### Coverage decoys

| # | Family | Where | Line | Must mention |
|---|---|---|---|---|
| D1 | F2 idle_setup | E103/sc4 | `Mara drops the pry bar and grabs him.` | find Danny |
| D2 | F3 dormant_knowledge | E102/sc2 | `Mara pries up the loose floor stone.` | Mara, ledger location |
| D3 | F4 unmotivated_turn | E102/sc2 | `Mara pries up the loose floor stone.` | Cole, ledger location |

## Season 1 extension note

`eval/expected_greyharbor_s1.json` is the held-out season key for E101-E108. It keeps original assertions A1-A15 and planted findings P1-P4. The two-episode notes N1/N2 are resolved in the season extension because Danny appears alive in E103; the season key replaces them with new note targets N3/N4. E103-E108 contain no inline planted/trap/note markers.

## Episode 103 — "Bottle Tide"

### Expected core assertions introduced

- `located_at(Leather Ledger, Edda's Stove Pipe)` — E103/sc2.
- `knows(Mara, "stove pipe cache")`; `knows(Edda, "stove pipe cache")` — E103/sc2.
- `occupation(Gideon, "ferryman")` — E103/sc3.
- `alive(Danny)` and `knows(Mara, "Danny alive")` — E103/sc4.
- `knows(Danny, "blue hook signal")`; `knows(Mara, "blue hook signal")` — E103/sc4.
- `cannot(Danny, "swim")` — E103/sc5.
- `occupation(Rafi, "radio man")` — E103/sc6.
- `destroyed(Fog Bell Buoy)` — E103/sc7.

### Notes and traps

- N3 `dangling_reference`: Nerida Glass — E103/sc6 line: `Nerida Glass wants the ledger by morning.` Named by the radio message, never established on screen.
- T3 trap: E103/sc2 line: `Edda sets Tobias's old cap on the empty chair.` This is grief ritual, not Tobias appearing alive.

## Episode 104 — "Hush Money"

### Expected core assertions introduced

- `occupation(Lila, "tide clerk")` — E104/sc1.
- `promised(Mara, "find Black Osprey")` — E104/sc2.
- `cannot(Edda, "right hand")` — E104/sc3.
- `destroyed(Salt House Cold Room)` — E104/sc6.
- `knows(Mara, "Tobias killer")` — E104/sc7.
- `dies(Cole)` — E104/sc8.

### Notes

- N4 `idle_setup`: E104/sc2 line: `The ledger names a boat called the Black Osprey. I find that boat, I find who bought Cole.` Mara promises to find the Black Osprey; the thread is not paid off by E108.

## Episode 105 — "The Third Tide"

### Planted errors

| # | Check | Where | Line | Ground truth violated |
|---|---|---|---|---|
| P5 | `dead_speaker` | E105/sc3 (harbormaster's office) | `Tobias stands at the desk, dry-eyed and solid, tapping the chart with one finger.` | Tobias speaks/acts; died E101/sc5; scene not marked flashback |
| P6 | `presence_conflict` | E105/sc5 (courthouse archive) | `Danny stands beside Mara at the courthouse map table, saltwater dripping from his cuffs.` | Danny appears in the courthouse archive while chained in the Blue Gull hold until the third tide turns from E105/sc4 |

### Trap

- T4 trap: E105/sc6 line: `ON THE RECORDER, COLE'S VOICE rasps through static.` This is recorder playback, not Cole appearing alive after E104/sc8.

## Episode 106 — "Number Station"

### Expected core assertions introduced

- `located_at(Rafi, Weather Station)` with an until-dawn lock interval — E106/sc1.
- `located_at(Rafi, Edda's Kitchen)` — E106/sc2.
- `occupation(Hale, "state investigator")` — E106/sc3.
- `knows(Hale, "blue hook signal")` — E106/sc3.

### Planted errors

| # | Check | Where | Line | Ground truth violated |
|---|---|---|---|---|
| P7 | `presence_conflict` | E106/sc2 (Edda's kitchen) | `Rafi sits at Edda's table, dripping rain onto the floorboards.` | Rafi appears in Edda's kitchen during the same night he is locked in the Weather Station until dawn in E106/sc1 |
| P8 | `premature_knowledge` | E106/sc3 (bus depot) | `State investigator. I hear a blue hook means your brother is alive.` | Hale knows the blue hook signal; source fact was private to Danny and Mara in E103/sc4, with no on-screen source for Hale |

## Episode 107 — "Bell Water"

### Expected core assertions introduced

- `knows(Jory, "stove pipe cache")` — E107/sc2.
- `occupation(Jory, "bait clerk")` — E107/sc2.

### Planted errors

| # | Check | Where | Line | Ground truth violated |
|---|---|---|---|---|
| P9 | `premature_knowledge` | E107/sc2 (bait shop) | `Tell Mara the stove pipe was clever.` | Jory knows the ledger is hidden in Edda's stove pipe; source fact was limited to Mara and Edda in E103/sc2 |
| P10 | `destroyed_location_use` | E107/sc3 (fog bell buoy) | `The Fog Bell Buoy rises whole from the water, cage slick with weed, as if it never burned.` | Fog Bell Buoy was destroyed and sunk in E103/sc7; scene uses it afterward, not a flashback |
| P11 | `capability_violation` | E107/sc4 (water below the buoy) | `Danny dives from the buoy and swims hard across the black water toward the skiff.` | Danny swims; `cannot(Danny, swim)` established E103/sc5 |

## Episode 108 — "King Tide"

### Expected core assertions introduced

- `believes(Edda, "Hale owns Black Osprey")` — E108/sc3.

### Planted errors

| # | Check | Where | Line | Ground truth violated |
|---|---|---|---|---|
| P12 | `destroyed_location_use` | E108/sc2 (Salt House cold room) | `The cold room hums intact, frost bright along the shelves.` | Salt House cold room was destroyed in E104/sc6; scene uses it afterward, not a flashback |
| P13 | `capability_violation` | E108/sc3 (Edda's kitchen) | `Edda kneads hard with her right hand, folding the dough until it shines.` | Edda uses her right hand; `cannot(Edda, right hand)` established E104/sc3 |
| P14 | `dead_speaker` | E108/sc4 (harbormaster's office) | `Cole steps from behind the file cabinets, badge on his belt.` | Cole speaks/acts; died E104/sc8; scene not marked flashback |

### Trap

- T5 trap: E108/sc6 line: `I died in that skiff, Mara. This is the part after.` This is metaphorical survivor language, not a literal death assertion.

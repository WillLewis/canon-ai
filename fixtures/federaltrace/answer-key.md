# Federal Trace - Answer Key

Grading harness for the second Phase 3 fixture. All script material in this fixture is original material written for this repo. The FBI/procedural texture, institutions, and case vocabulary are fictionalized for test coverage and do not rely on any third-party script.

This key follows the Greyharbor Markdown contract so `eval/run_eval.py --answer-key fixtures/federaltrace/answer-key.md` can parse it without eval-code changes. There are no inline planted annotations in the `.fountain` files; this answer key is the only disclosure layer.

## Entities (expected after resolution)

| Canonical | Kind | Aliases seen in text |
|---|---|---|
| Lena Ortiz | character | LENA, Lena |
| Malik Shaw | character | MALIK, Malik |
| Ruth Keene | character | KEENE, Keene, SSA Ruth Keene |
| Priya Sen | character | PRIYA, Priya |
| Darius Cho | character | DARIUS, Darius, Dr. Darius Cho |
| Nadia Vale | character | NADIA, Nadia, confidential source, source |
| Nora Venn | character | NORA, Nora, AUSA Nora Venn |
| Viktor Sloane | character | SLOANE, Sloane, Viktor |
| Calder | character | Calder - provisional (referenced, never appears) |
| FBI Rivergate Field Office | location | field office, bullpen |
| FBI Evidence Garage | location | evidence garage |
| East Terminal Copy Room | location | copy room |
| East Terminal Locker Bay | location | locker bay |
| Federal Courthouse Witness Room | location | witness room |
| Federal Courthouse Parking Deck | location | parking deck |
| JTTF War Room | location | war room |
| East Terminal Service Platform | location | service platform |
| Encrypted Drive | object | black encrypted drive, drive |
| Meridian Card | object | green transit card, Meridian card |
| Cantor-19 File | object | CANTOR-19, redacted folder |

## Expected core assertions (extraction recall >=80% of these)

1. `occupation(Ruth Keene, "SSA")` - E101/sc2 (`SSA RUTH KEENE, 50s, runs the room without raising her voice.`).
2. `occupation(Lena Ortiz, "special agent")` - E101/sc2 (`SPECIAL AGENT LENA ORTIZ, 30s, pins rail-yard photos beside a map.`).
3. `occupation(Malik Shaw, "special agent")` - E101/sc2 (`SPECIAL AGENT MALIK SHAW, 40s, drops a wet field jacket on a chair.`).
4. `occupation(Priya Sen, "cyber analyst")` - E101/sc2 (`PRIYA SEN, 20s, cyber analyst, wheels over a laptop cart.`).
5. `occupation(Darius Cho, "forensic analyst")` - E101/sc2 (`DR. DARIUS CHO, 40s, forensic analyst, bags a smear of black gel from Mart's sleeve.`).
6. `promised(Priya Sen, "rebuild van route from tower dump")` - E101/sc2 line: `Give me the tower dump and I'll rebuild the van's route before lunch.`
7. `promised(Lena Ortiz, "trace Meridian card")` - E101/sc3 line: `I'll trace the Meridian card before arraignment.`
8. `knows(Nadia Vale, "Locker 17 key under toner tray")` - E101/sc3 line: `Locker 17 at East Terminal. Spare key is taped under the copy-room toner tray.`
9. `knows(Lena Ortiz, "Locker 17 key under toner tray")` - E101/sc3/E101/sc4 (`A brass key is taped underneath.`).
10. `knows(Lena Ortiz, "Keene buried Malik OPR complaint")` - E101/sc3 line: `Keene buried an OPR complaint on Malik. If he learns that from anyone but you, he walks.`
11. `knows(Nadia Vale, "Project Sundial has ghost budget line")` - E101/sc3 line: `And Project Sundial has a ghost budget line inside the Bureau. I don't know who funds it.`
12. `knows(Nadia Vale, "Calder can prove route swap")` - E101/sc3 line: `Calder can prove the route was swapped.`
13. `knows(Priya Sen, "van route from tower dump")` - E101/sc6 line: `Tower dump is clean. The van paused at Canal Spur for four minutes before the hit, then the SUV ghosted toward East Terminal.`
14. `destroyed(FBI Evidence Garage)` - E101/sc7 line: `The FBI Evidence Garage burns through the rack doors until the ceiling drops and the room is destroyed.`
15. `dies(Nadia Vale)` - E101/sc8 line: `Nadia dies with her hand wrapped around Lena's sleeve.`
16. `knows(Priya Sen, "Locker 17 key under toner tray")` - E102/sc1 line: `The spare key was taped under the copy-room toner tray because Nadia hid it there before the takedown.`
17. `located_at(Malik Shaw, Federal Courthouse Witness Room)` - E102/sc2 line: `You stay in this witness room until nine tonight. No hallway, no calls, no exceptions.`
18. `located_at(Malik Shaw, FBI Evidence Garage)` - E102/sc3 line: `Malik steps between two evidence racks and signs a fresh chain-of-custody sheet.`
19. `possesses(Darius Cho, Encrypted Drive)` - E102/sc5 line: `Darius pockets the encrypted drive and hands it to a janitor wearing no badge.`
20. `possesses(Ruth Keene, Cantor-19 File)` - E102/sc6 line: `Keene tapes a redacted folder labeled CANTOR-19 to the inside of her case binder.`

## Planted errors (checker must find ALL)

`dangling_reference` is note-severity in the current checker, but it is included here as a planted continuity finding because this fixture is required to cover that check type.

| # | Check | Where | Line | Ground truth violated |
|---|---|---|---|---|
| P1 | `dead_speaker` | E102/sc4 (conference room) | `You always catalog after the lie. That is how they own the truth.` | Nadia speaks/acts; died E101/sc8; scene is not marked flashback or playback |
| P2 | `presence_conflict` | E102/sc3 (evidence garage) | `Malik steps between two evidence racks and signs a fresh chain-of-custody sheet.` | Malik is locked in the Federal Courthouse Witness Room until nine in E102/sc2, then appears in the FBI Evidence Garage at 3:15 P.M. |
| P3 | `premature_knowledge` | E102/sc1 (bullpen) | `The spare key was taped under the copy-room toner tray because Nadia hid it there before the takedown.` | Priya states the Locker 17 key location with no on-screen source; Nadia told Lena in E101/sc3 and Priya was not present |
| P4 | `destroyed_location_use` | E102/sc3 (evidence garage) | `The FBI Evidence Garage hums intact, fluorescent lights bright over clean evidence racks.` | The FBI Evidence Garage was destroyed in E101/sc7; scene uses it afterward, not as a flashback |
| P5 | `dangling_reference` | E101/sc3 (secure interview room) | `Ask Calder. Calder can prove the route was swapped.` | Calder is referenced as a person who can prove the route swap but never appears or receives an establishing canon row in these two episodes |

## Expected notes

No additional deterministic note expectations outside P5. Reader's Report coverage items are listed below.

## False-positive budget

<=2 per episode. Decoys the checker/report must NOT flag:
- Priya's E101/sc2 promise about the tower dump is paid off in E101/sc6; it is not an idle setup.
- Lena's E101/sc5 line `Nadia was right about the toner tray. Key works, locker opens.` means Lena's Locker 17 knowledge surfaces; it should not become dormant knowledge.
- Keene's E101/sc2 order to lock East Terminal is motivated by the SUV entering East Terminal; it should not become an unmotivated turn.
- Keene's E102/sc6 line `Cantor stays sealed by court order.` is a sealed-worthy intentional mystery, not an open-question defect.

## Coverage notes

Reader's Report coverage scoring uses this table in addition to planted continuity errors.

### Planted coverage items

| # | Family | Where | Line | Must mention |
|---|---|---|---|---|
| C1 | F1 open_question | E101/sc3 | `And Project Sundial has a ghost budget line inside the Bureau. I don't know who funds it.` | Project Sundial |
| C2 | F2 idle_setup | E101/sc3 | `I'll trace the Meridian card before arraignment.` | Meridian card |
| C3 | F3 dormant_knowledge | E101/sc3 | `Keene buried an OPR complaint on Malik. If he learns that from anyone but you, he walks.` | OPR complaint, Malik |
| C4 | F4 unmotivated_turn | E102/sc5 | `Darius pockets the encrypted drive and hands it to a janitor wearing no badge.` | Darius, encrypted drive |

### Coverage decoys

| # | Family | Where | Line | Must mention |
|---|---|---|---|---|
| D1 | F2 idle_setup | E101/sc2 | `Give me the tower dump and I'll rebuild the van's route before lunch.` | tower dump |
| D2 | F3 dormant_knowledge | E101/sc5 | `Nadia was right about the toner tray. Key works, locker opens.` | toner tray |
| D3 | F4 unmotivated_turn | E101/sc2 | `The SUV entered East Terminal six minutes after the hit. Lock the terminal and hold every camera card.` | lock the terminal |
| D4 | F1 open_question | E102/sc6 | `Cantor stays sealed by court order. We do not brief it, speculate on it, or make it a theory.` | Cantor |

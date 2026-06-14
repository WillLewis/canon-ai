"""Failure-mode attribution — Phase A item 1 + 1c (ARCHITECTURE_PLAN.md §6).

Turns "9/14" into a per-layer diagnosis: for every planted error the system MISSED,
which architectural layer dropped the signal? This is what drives the roadmap — if
most misses are extraction, schema work waits; if presence_conflict is store-
modeling, fix that directly; if knowledge misses are fact-handle drift, build facts
sooner.

It does NOT change scoring. Recall / FP / trap numbers come from the vendor-neutral
matcher (eval/run_eval.py, reused here exactly as bench/scorer.py reuses it); this
module adds an attribution pass that traces each missed error's required assertions
through the pipeline artifacts:

    candidates (Stage 2 extraction) --> resolved (Stage 3 / live DB) --> findings (Stage 5)

LAYERS (assigned per missed PLANTED error, earliest-stage cause wins):
  extraction_miss          required assertion never extracted (absent from candidates)
  extraction_proposition   the event WAS noticed, but encoded as a different/adjacent
                           proposition (low token overlap with the canonical fact) —
                           fact identity alone won't recover it; extraction must emit it
  resolution_miss          correct in candidates, lost/changed in entity resolution
  fact_canonicalization    the RIGHT fact, but the handle drifted (high token overlap) —
                           first-class fact identity recovers it
  store_range_modeling     data present & correct, but interval modeling erased the
                           overlap the check needs (presence_conflict)
  store_presence_modeling  data present, but the entity isn't in scene_presence at the
                           violating scene (objects/locations aren't tracked there yet)
  sql_check_miss           all data present & correct; the check query simply didn't fire

Plus, from the matcher: false_positives (grouped by check) and trap_violations.

The dependency map (which assertions each error needs) is DIAGNOSTIC metadata in
eval/failure_map_<world>.json — separate from the grading ground truth, so it can
never affect recall/FP/trap scoring. Without --candidates, resolution misses fold
into extraction_miss.

The value-drift split (fact_canonicalization vs extraction_proposition) uses token
overlap between the extracted object_value and the expected handle's synonyms.

`--db` does two things beyond reading the artifact file: it sources the resolved set
from the LIVE graph (authoritative, post-store), and it RE-GROUNDS every missed
planted error with a per-check PROBE that replicates that check's real matching
against the graph — not the eval's expected-handle proxy. The proxy can diverge from
the check: e.g. P9's three knowers share an identical handle (so the check links them)
yet the proxy reported fact-canon; the probe instead finds the true gate (co-presence
with a prior knower). Probed cases are tagged `[probed]`, proxy cases `[ proxy]`.

Usage:
  python -m bench.failure_modes --candidates out/s1.candidates.json \\
      --resolved out/s1.assertions.json --findings out/s1.findings.json
  # DB-backed (authoritative resolved set from the loaded world):
  python -m bench.failure_modes --candidates out/s1.candidates.json \\
      --db postgresql://postgres:postgres@127.0.0.1:5432/postgres --world greyharbor_s1 \\
      --findings out/s1.findings.json
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import pathlib

ROOT = pathlib.Path(__file__).resolve().parents[1]
_spec = importlib.util.spec_from_file_location("run_eval", ROOT / "eval" / "run_eval.py")
run_eval = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(run_eval)

EXTRACTION = "extraction_miss"
EXTRACTION_PROP = "extraction_proposition"
RESOLUTION = "resolution_miss"
FACT_CANON = "fact_canonicalization"
STORE_RANGE = "store_range_modeling"
STORE_PRESENCE = "store_presence_modeling"
SQL_CHECK = "sql_check_miss"

# render/histogram order = root-cause priority (earliest pipeline stage first)
LAYER_ORDER = [EXTRACTION, EXTRACTION_PROP, RESOLUTION, FACT_CANON,
               STORE_RANGE, STORE_PRESENCE, SQL_CHECK]
# upstream layers checked, in priority order, before any check-specific reasoning
_UPSTREAM = (EXTRACTION, EXTRACTION_PROP, RESOLUTION, FACT_CANON)

# A value-drift this close (token containment of the canonical handle) is the right
# fact with a drifted handle (fact-canon); below it, a different proposition entirely.
DRIFT_THRESHOLD = 0.5
_STOP = {"the", "a", "an", "of", "is", "in", "on", "at", "to", "and", "s", "it",
         "her", "his", "their", "that", "this", "with", "for", "was", "are"}


# ---------------------------------------------------------------------------
# Artifact loading
# ---------------------------------------------------------------------------

def flatten_assertions(obj) -> list:
    """Normalize a candidates file (nested by scene) or a resolved file (flat
    {"assertions": [...]}) into a flat list of assertion dicts."""
    if isinstance(obj, list):
        return obj
    if "assertions" in obj:
        return obj["assertions"]
    out: list = []
    for sc in obj.get("scenes", []):
        out.extend(sc.get("assertions", []))
    return out


def _load(path) -> list:
    return flatten_assertions(json.loads(pathlib.Path(path).read_text(encoding="utf-8")))


def load_resolved_from_db(url: str, world: str) -> list:
    """The DB-backed pass: pull the authoritative stored assertion set from the live
    world (post-store), in the same {subject, predicate, object_value, object_entity}
    shape as the resolved artifact. Lets attribution reflect the loaded graph rather
    than a possibly-stale file."""
    from canon import ingest  # lazy: keep psycopg out of the import path for file-only runs
    conn = ingest.connect(url)
    try:
        cur = conn.cursor()
        cur.execute("SELECT id FROM worlds WHERE name = %s", (world,))
        row = cur.fetchone()
        if not row:
            raise RuntimeError(f"world {world!r} not found in the database")
        cur.execute(
            "SELECT e.name, a.predicate, a.object_value, lo.name "
            "FROM assertions a JOIN entities e ON e.id = a.subject_id "
            "LEFT JOIN entities lo ON lo.id = a.object_id "
            "WHERE a.world_id = %s AND a.status NOT IN ('rejected','retconned')",
            (row[0],),
        )
        # store.py synthesizes object_value = predicate for intransitive predicates
        # (dies/destroyed/alive) to satisfy the schema CHECK; reverse that here so the
        # set matches the eval contract (intransitive -> null), or those rows stop matching.
        out = [{"subject": s, "predicate": p,
                "object_value": (None if ov == p else ov), "object_entity": oe}
               for s, p, ov, oe in cur.fetchall()]
        conn.rollback()
        return out
    finally:
        try:
            conn.close()
        except Exception:
            pass


def _alias_map(gt: dict) -> dict:
    return {run_eval.norm(k): v for k, v in gt["alias_map"].items()}


# ---------------------------------------------------------------------------
# Value-drift similarity (separates handle-drift from a different proposition)
# ---------------------------------------------------------------------------

def _tokens(s) -> set:
    return {t for t in run_eval.norm(s).split() if t and t not in _STOP}


def _value_overlap(extracted, expected_key, synonyms: dict) -> float:
    """Max token-containment of any expected synonym within the extracted value:
    how much of the canonical handle's content words appear in what was extracted."""
    if not expected_key:
        return 0.0
    et = _tokens(extracted)
    if not et:
        return 0.0
    best = 0.0
    for syn in synonyms.get(expected_key, [expected_key]):
        st = _tokens(syn)
        if st:
            best = max(best, len(et & st) / len(st))
    return best


def _best_drift(assertions, exp, alias_map, synonyms) -> tuple:
    """Over same-(subject,predicate) assertions: (best overlap, the value that scored it)."""
    best, val = 0.0, None
    for act in assertions:
        if run_eval.canon_entity(act.get("subject"), alias_map) != exp["subject"]:
            continue
        if run_eval.norm(act.get("predicate")) != run_eval.norm(exp["predicate"]):
            continue
        score = _value_overlap(act.get("object_value"), exp.get("object_value"), synonyms)
        if val is None or score > best:
            best, val = score, act.get("object_value")
    return best, val


def _sp_present(assertions: list, exp: dict, alias_map: dict) -> bool:
    """True if any assertion matches expected subject (via aliases) + predicate,
    regardless of object — i.e. the claim was made, even if its value drifted."""
    for act in assertions:
        if run_eval.canon_entity(act.get("subject"), alias_map) != exp["subject"]:
            continue
        if run_eval.norm(act.get("predicate")) == run_eval.norm(exp["predicate"]):
            return True
    return False


# ---------------------------------------------------------------------------
# Attribution
# ---------------------------------------------------------------------------

def _drift_layer_detail(a_id, exp, assertions, alias_map, synonyms, stage) -> tuple:
    """Classify a subject+predicate-present-but-value-drifted assertion. Entity-valued
    targets are handle-canon by nature; value-valued ones split on token overlap."""
    if exp.get("object_entity"):               # entity-valued (e.g. located_at): wrong entity
        return FACT_CANON, f"{a_id} ({exp['subject']}/{exp['predicate']}: entity drift, {stage})"
    score, val = _best_drift(assertions, exp, alias_map, synonyms)
    layer = FACT_CANON if score >= DRIFT_THRESHOLD else EXTRACTION_PROP
    return layer, (f"{a_id} ({exp['subject']}/{exp['predicate']}: got {val!r} vs "
                   f"{exp.get('object_value')!r}, overlap={score:.2f}, {stage})")


def classify_required(a_id, exp_by_id, cand_matched, res_matched,
                      candidates, resolved, alias_map, synonyms) -> tuple:
    """Where did one required assertion die? -> (state, detail).
    state in {"present", EXTRACTION, EXTRACTION_PROP, RESOLUTION, FACT_CANON}."""
    exp = exp_by_id.get(a_id)
    if exp is None:
        return "present", f"{a_id} (unknown id; skipped)"
    if a_id in res_matched:
        return "present", a_id
    # Intransitive event (dies/destroyed/alive — no object_value, no object_entity): the
    # "after the event" checks key on the PREDICATE alone, so a subject+predicate match IS
    # effectively present (a stray object_value that fooled the value-matcher is not drift),
    # and the real miss is downstream — not fact-canon / entity-drift.
    if exp.get("object_value") is None and not exp.get("object_entity") \
            and _sp_present(resolved, exp, alias_map):
        return "present", f"{a_id} ({exp['subject']}/{exp['predicate']} present; checks key on predicate)"
    # Correct in candidates but not in resolved -> resolution changed/dropped it. Check this
    # BEFORE the resolved-drift branch, or a resolution regression is mislabeled as drift.
    if cand_matched is not None and a_id in cand_matched:
        return RESOLUTION, f"{a_id} (correct in candidates, lost/changed in resolution)"
    if _sp_present(resolved, exp, alias_map):
        return _drift_layer_detail(a_id, exp, resolved, alias_map, synonyms, "resolved")
    if candidates is not None and _sp_present(candidates, exp, alias_map):
        return _drift_layer_detail(a_id, exp, candidates, alias_map, synonyms, "extraction")
    return EXTRACTION, f"{a_id} ({exp['subject']}/{exp['predicate']} never extracted)"


def _violation_present(resolved, subject_canon, keyword, alias_map) -> bool:
    """A positive (non-'cannot') assertion by this subject whose value carries the
    capability keyword — the act that should conflict with the 'cannot' rule."""
    kw = run_eval.norm(keyword)
    for act in resolved:
        if run_eval.canon_entity(act.get("subject"), alias_map) != subject_canon:
            continue
        if run_eval.norm(act.get("predicate")) == "cannot":
            continue
        if kw and kw in run_eval.norm(act.get("object_value")):
            return True
    return False


def _attribute_missed(entry, exp_by_id, cand_matched, res_matched,
                      candidates, resolved, alias_map, synonyms) -> tuple:
    """-> (layer, detail) for a missed planted error."""
    states = [classify_required(a, exp_by_id, cand_matched, res_matched,
                                candidates, resolved, alias_map, synonyms)
              for a in entry.get("requires_all", [])]
    for layer in _UPSTREAM:                       # earliest-stage upstream failure wins
        hit = [d for s, d in states if s == layer]
        if hit:
            return layer, "; ".join(hit)

    check = entry.get("check")

    if check == "premature_knowledge" and entry.get("requires_prior_any"):
        priors = [classify_required(a, exp_by_id, cand_matched, res_matched,
                                    candidates, resolved, alias_map, synonyms)
                  for a in entry["requires_prior_any"]]
        if not any(s == "present" for s, _ in priors):
            return EXTRACTION, ("prior knower absent — asymmetry undetectable ("
                                + "; ".join(d for _, d in priors) + ")")
        return entry.get("downstream", SQL_CHECK), "knower + prior knower present; check did not fire"

    if check == "capability_violation" and entry.get("violation_keyword"):
        req = entry.get("requires_all") or []
        exp0 = exp_by_id.get(req[0]) if req else None
        if exp0 and not _violation_present(resolved, exp0["subject"], entry["violation_keyword"], alias_map):
            return EXTRACTION, f"violating act ('{entry['violation_keyword']}') never extracted"
        return entry.get("downstream", SQL_CHECK), "cannot-rule + violating act present; check did not fire"

    return entry.get("downstream", SQL_CHECK), "required data present; check did not fire"


def attribute(findings, candidates, resolved, gt, fmap) -> dict:
    """Score with the shared matcher, then attribute every missed planted error to a
    layer. Returns {"summary": {...}, "cases": [...]}."""
    run_eval.GT = gt
    found, _missed, fps, trap_hits = run_eval.score_findings(findings)
    found = set(found)
    alias_map = _alias_map(gt)
    synonyms = gt.get("object_value_synonyms", {})
    exp_by_id = {e["id"]: e for e in gt["expected_assertions"]}

    if candidates is not None:
        _, cm, _ = run_eval.score_extraction(candidates)
        cand_matched = set(cm)
    else:
        cand_matched = None
    _, rm, _ = run_eval.score_extraction(resolved)
    res_matched = set(rm)

    planted_ids = {e["id"] for e in gt["expected_findings"]}
    cases: list = []
    for fid, entry in fmap.items():
        if fid.startswith("_"):
            continue
        common = {"id": fid, "check": entry.get("check"), "planted": fid in planted_ids}
        if fid in found:
            cases.append({**common, "status": "caught", "layer": None, "detail": None})
        else:
            layer, detail = _attribute_missed(entry, exp_by_id, cand_matched, res_matched,
                                              candidates, resolved, alias_map, synonyms)
            cases.append({**common, "status": "missed", "layer": layer, "detail": detail,
                          "grounded": False})

    hist: dict = {}
    for c in cases:
        if c["status"] == "missed" and c["planted"]:
            hist.setdefault(c["layer"], []).append(c["id"])

    fp_by_check: dict = {}
    for f in fps:
        fp_by_check.setdefault(f.get("check", "?"), []).append(f.get("explanation", "")[:90])

    summary = {
        "planted_caught": sum(1 for c in cases if c["planted"] and c["status"] == "caught"),
        "planted_total": len(planted_ids),
        "miss_histogram": {k: hist[k] for k in LAYER_ORDER if k in hist},
        "false_positives": len(fps),
        "fp_by_check": fp_by_check,
        "trap_violations": sorted({t[0] for t in trap_hits}),
    }
    return {"summary": summary, "cases": cases}


# ---------------------------------------------------------------------------
# DB-grounded probes — attribute a missed planted error against each CHECK's REAL
# matching (live graph), not the eval's expected-handle proxy. This is what tells us
# whether fact identity would actually help (a prior knower exists but the handle
# drifted) vs. whether the gate is elsewhere (co-presence, intervals, presence).
# ---------------------------------------------------------------------------

_NORM_SQL = r"regexp_replace(lower(a.object_value),'[^a-z0-9 ]','','g')"


def _eid(cur, wid, name):
    cur.execute("SELECT id FROM entities WHERE world_id=%s AND lower(name)=lower(%s)", (wid, name))
    r = cur.fetchone()
    if r:
        return r[0]
    cur.execute("SELECT al.entity_id FROM aliases al JOIN entities e ON e.id=al.entity_id "
                "WHERE e.world_id=%s AND lower(al.alias)=lower(%s)", (wid, name))
    r = cur.fetchone()
    return r[0] if r else None


def _fact_match(handle, expected_key, synonyms) -> bool:
    """Does `handle` refer to the intended fact (expected_key)? Conservative: requires
    >= DRIFT_THRESHOLD containment AND >= 2 shared content tokens (or, for a 1-token
    expected handle, that token) — so a single shared word (e.g. a name) can't trigger a
    phantom match. This is the same prefer-unlinked-over-wrongly-linked bias as fact resolution."""
    et = _tokens(handle)
    if not et or not expected_key:
        return False
    for syn in synonyms.get(expected_key, [expected_key]):
        st = _tokens(syn)
        if st and len(et & st) >= min(2, len(st)) and len(et & st) / len(st) >= DRIFT_THRESHOLD:
            return True
    return False


def _probe_premature(cur, wid, knower, eid, exp_handle, synonyms):
    cur.execute(
        f"SELECT {_NORM_SQL} AS h, coalesce(lower(a.valid_during), s.story_position) AS pos "
        "FROM assertions a JOIN scenes s ON s.id=a.established_in_scene "
        "WHERE a.world_id=%s AND a.subject_id=%s AND a.predicate='knows' "
        "AND a.object_value IS NOT NULL AND a.status NOT IN ('rejected','retconned')", (wid, eid))
    mine = cur.fetchall()
    # the knower's handle(s) that actually refer to the INTENDED fact (not incidental knows)
    matched = [(h, pos) for h, pos in mine if pos is not None and _fact_match(h, exp_handle, synonyms)]
    if not matched:
        best = max((round(_value_overlap(h, exp_handle, synonyms), 2) for h, _ in mine), default=0.0)
        return EXTRACTION, (f"{knower}: no `knows` matching the intended fact {exp_handle!r} extracted "
                            f"({len(mine)} other knows; best overlap {best}) — extraction/proposition gap")
    h, pos = min(matched, key=lambda x: x[1])               # earliest the knower holds the fact
    # exact-normalized prior knower — the check's actual linkage
    cur.execute(
        "SELECT e.name, min(coalesce(lower(a.valid_during), s.story_position)) "
        "FROM assertions a JOIN entities e ON e.id=a.subject_id JOIN scenes s ON s.id=a.established_in_scene "
        f"WHERE a.world_id=%s AND a.predicate='knows' AND a.subject_id<>%s AND {_NORM_SQL}=%s "
        "AND a.status NOT IN ('rejected','retconned') GROUP BY e.name", (wid, eid, h))
    priors = [(n, p) for n, p in cur.fetchall() if p is not None and p < pos]
    if priors:                                              # late knower -> why no flag? co-presence.
        cur.execute(
            "SELECT sc.story_position, oe.name FROM scene_presence sp "
            "JOIN scenes sc ON sc.id=sp.scene_id JOIN scene_presence spo ON spo.scene_id=sp.scene_id "
            "JOIN assertions a ON a.subject_id=spo.entity_id JOIN entities oe ON oe.id=spo.entity_id "
            f"WHERE sp.entity_id=%s AND a.predicate='knows' AND {_NORM_SQL}=%s AND a.subject_id<>%s "
            "AND sc.story_position<=%s AND a.valid_during @> sc.story_position "
            "AND a.status NOT IN ('rejected','retconned') LIMIT 1", (eid, h, eid, pos))
        cop = cur.fetchone()
        if cop:
            return SQL_CHECK, (f"co-presence suppression: {knower} shares scene pos{cop[0]} with prior "
                               f"knower {cop[1]} of {h!r}; the check excuses it (heuristic too permissive)")
        return SQL_CHECK, (f"{knower} is a late knower of {h!r} (prior {priors[0][0]}@{priors[0][1]}), "
                           "not co-present — check should fire; inspect anchor/status")
    # no exact prior — does a prior knower hold the SAME intended fact under a different handle?
    cur.execute(
        f"SELECT e.name, {_NORM_SQL} AS oh, coalesce(lower(a.valid_during), s.story_position) AS opos "
        "FROM assertions a JOIN entities e ON e.id=a.subject_id JOIN scenes s ON s.id=a.established_in_scene "
        "WHERE a.world_id=%s AND a.predicate='knows' AND a.subject_id<>%s AND a.object_value IS NOT NULL "
        "AND a.status NOT IN ('rejected','retconned')", (wid, eid))
    for name, oh, opos in cur.fetchall():
        if opos is None or opos >= pos or oh == h:
            continue
        if _fact_match(oh, exp_handle, synonyms):
            return FACT_CANON, (f"prior knower {name} knows {oh!r} — the intended fact under a different handle "
                                f"than {h!r}; the check's exact match can't link them. Fact identity would.")
    return EXTRACTION, f"{knower}: no prior knower of {exp_handle!r} extracted (treated as originator)"


def _probe_presence_conflict(cur, wid, subject, eid):
    cur.execute("SELECT count(*) FROM assertions WHERE world_id=%s AND subject_id=%s "
                "AND predicate='located_at' AND status NOT IN ('rejected','retconned')", (wid, eid))
    if cur.fetchone()[0] < 2:
        return EXTRACTION, f"{subject}: fewer than two located_at assertions extracted"
    cur.execute(
        "SELECT count(*) FROM assertions a JOIN assertions b ON b.subject_id=a.subject_id AND b.id>a.id "
        "WHERE a.world_id=%s AND a.subject_id=%s AND a.predicate='located_at' AND b.predicate='located_at' "
        "AND a.object_id<>b.object_id AND a.valid_during && b.valid_during "
        # mirror presence_conflict exactly (db/checks.sql): the check excludes open-lower
        # (backdated, confirm-queue) intervals and rejected/retconned rows; without these the
        # probe counts overlaps the check correctly suppresses and falsely blames the scan.
        "AND NOT lower_inf(a.valid_during) AND NOT lower_inf(b.valid_during) "
        "AND a.status NOT IN ('rejected','retconned') AND b.status NOT IN ('rejected','retconned')",
        (wid, eid))
    overlaps = cur.fetchone()[0]
    if overlaps == 0:
        return STORE_RANGE, (f"{subject}: located_at present but NO two different-location intervals overlap "
                             "(serialized by movement-closing / confinement duration not modeled)")
    return SQL_CHECK, (f"{subject}: {overlaps} overlapping different-location pair(s) exist but weren't "
                       "flagged (likely the lower_inf guard) — inspect the scan")


def _probe_presence_event(cur, wid, entity, eid, predicate):
    # Mirror the two checks faithfully (db/checks.sql): destroyed_location_use filters
    # `d.polarity` and has NO resurrection hatch; dead_speaker has NO polarity filter but
    # DOES exempt resurrection (a later 'alive' interval covering the scene, starting after
    # the death). Diverging here would falsely report SQL_CHECK for a correctly-suppressed case.
    polarity = "AND d.polarity " if predicate == "destroyed" else ""
    hatch = ("AND NOT EXISTS (SELECT 1 FROM assertions r WHERE r.subject_id=d.subject_id "
             "AND r.predicate='alive' AND r.polarity AND r.valid_during @> s.story_position "
             "AND lower(r.valid_during) > lower(d.valid_during)) ") if predicate == "dies" else ""
    exist_pol = "AND polarity " if predicate == "destroyed" else ""

    cur.execute("SELECT count(*), bool_or(lower(valid_during) IS NULL) FROM assertions "
                "WHERE world_id=%s AND subject_id=%s AND predicate=%s " + exist_pol +
                "AND status NOT IN ('rejected','retconned')", (wid, eid, predicate))
    n, any_unanchored = cur.fetchone()
    if not n:
        return EXTRACTION, f"{entity}: no `{predicate}` assertion extracted"
    # the check's ACTUAL join: present at a non-flashback scene strictly after the event's lower bound
    cur.execute(
        "SELECT count(*) FROM assertions d JOIN scene_presence sp ON sp.entity_id=d.subject_id "
        "JOIN scenes s ON s.id=sp.scene_id WHERE d.world_id=%s AND d.subject_id=%s AND d.predicate=%s "
        + polarity +
        "AND d.status NOT IN ('rejected','retconned') AND NOT s.is_flashback "
        "AND s.story_position > lower(d.valid_during) " + hatch, (wid, eid, predicate))
    if cur.fetchone()[0]:
        return SQL_CHECK, f"{entity}: present after `{predicate}` yet unflagged — inspect the scan (flashback/escape-hatch)"
    # join empty. Present LATER than the event's establishing scene, but the event interval is
    # unanchored ('(,)', lower NULL) so '> lower' can't compare? -> store/range (anchor the event).
    cur.execute(
        "SELECT count(*) FROM assertions d JOIN scenes es ON es.id=d.established_in_scene "
        "JOIN scene_presence sp ON sp.entity_id=d.subject_id JOIN scenes s ON s.id=sp.scene_id "
        "WHERE d.world_id=%s AND d.subject_id=%s AND d.predicate=%s " + polarity +
        "AND d.status NOT IN ('rejected','retconned') AND NOT s.is_flashback "
        "AND s.story_position > es.story_position", (wid, eid, predicate))
    if any_unanchored and cur.fetchone()[0]:
        return STORE_RANGE, (f"{entity}: `{predicate}` stored unanchored ('(,)', lower NULL) so '> destruction' "
                             f"can't compare, yet {entity} appears later — anchor the event to its scene (cf. P7)")
    return STORE_PRESENCE, (f"{entity}: `{predicate}` present, but {entity} is in no post-event scene_presence "
                            "row (objects/locations aren't tracked there) — check can't fire")


def _probe_capability(cur, wid, subject, eid, keyword):
    cur.execute("SELECT count(*) FROM assertions WHERE world_id=%s AND subject_id=%s AND predicate='cannot' "
                "AND polarity AND status NOT IN ('rejected','retconned')", (wid, eid))
    if not cur.fetchone()[0]:
        return EXTRACTION, f"{subject}: no `cannot` rule extracted"
    # mirror BOTH arms of capability_violation (db/checks.sql): arm A = a positive (polarity)
    # non-cannot act whose value contains the capability handle; arm B = an explicit
    # contradiction cannot(X, v, polarity=false). The prior probe dropped the polarity filter
    # (arm A) and couldn't see arm B at all.
    kw = run_eval.norm(keyword)
    cur.execute(
        "SELECT count(*) FROM assertions a WHERE a.world_id=%s AND a.subject_id=%s "
        "AND a.status NOT IN ('rejected','retconned') AND ("
        f"  (a.polarity AND a.predicate<>'cannot' AND position(%s in {_NORM_SQL})>0) "
        f"  OR (NOT a.polarity AND a.predicate='cannot' AND {_NORM_SQL}=%s))",
        (wid, eid, kw, kw))
    if not cur.fetchone()[0]:
        return EXTRACTION, f"{subject}: cannot-rule present but no violating act ('{keyword}') extracted"
    return SQL_CHECK, f"{subject}: cannot-rule + violating act present, yet unflagged — inspect the scan"


def reground(conn, world, report, gt, fmap, synonyms) -> dict:
    """Re-attribute each missed planted error against the live graph with check-specific
    probes (authoritative), replacing the file proxy and marking the case `grounded`."""
    cur = conn.cursor()
    cur.execute("SELECT id FROM worlds WHERE name=%s", (world,))
    row = cur.fetchone()
    if not row:
        return report
    wid = row[0]
    exp_by_id = {e["id"]: e for e in gt["expected_assertions"]}
    for c in report["cases"]:
        if c["status"] != "missed" or not c["planted"]:
            continue
        entry = fmap.get(c["id"]) or {}
        req = entry.get("requires_all") or []
        if not req or req[0] not in exp_by_id:
            continue
        exp = exp_by_id[req[0]]
        eid = _eid(cur, wid, exp["subject"])
        if eid is None:
            continue
        check = entry.get("check")
        if check == "premature_knowledge":
            res = _probe_premature(cur, wid, exp["subject"], eid, exp.get("object_value"), synonyms)
        elif check == "presence_conflict":
            res = _probe_presence_conflict(cur, wid, exp["subject"], eid)
        elif check in ("destroyed_location_use", "dead_speaker"):
            res = _probe_presence_event(cur, wid, exp["subject"], eid,
                                        "destroyed" if check == "destroyed_location_use" else "dies")
        elif check == "capability_violation":
            res = _probe_capability(cur, wid, exp["subject"], eid, entry.get("violation_keyword", ""))
        else:
            continue
        c["layer"], c["detail"], c["grounded"] = res[0], res[1], True
    conn.rollback()
    hist: dict = {}
    for c in report["cases"]:
        if c["status"] == "missed" and c["planted"]:
            hist.setdefault(c["layer"], []).append(c["id"])
    report["summary"]["miss_histogram"] = {k: hist[k] for k in LAYER_ORDER if k in hist}
    return report


# ---------------------------------------------------------------------------
# Rendering / CLI
# ---------------------------------------------------------------------------

def render(report: dict) -> str:
    s = report["summary"]
    lines = ["=" * 66, "CANON AI — FAILURE-MODE ATTRIBUTION", "=" * 66,
             f"Planted errors caught : {s['planted_caught']}/{s['planted_total']}", ""]
    missed = [c for c in report["cases"] if c["planted"] and c["status"] == "missed"]
    if missed:
        lines.append(f"MISSED ({len(missed)}) — attributed layer:")
        for c in missed:
            tag = "probed" if c.get("grounded") else " proxy"
            check, layer = c.get("check") or "?", c.get("layer") or "?"
            lines.append(f"  {c['id']:<4} {check:<22} {layer:<22} [{tag}] {c.get('detail') or ''}")
        lines.append("")
    lines.append("MISS HISTOGRAM (planted only):")
    if s["miss_histogram"]:
        for layer, ids in s["miss_histogram"].items():
            lines.append(f"  {layer:<24} {len(ids)}   ({', '.join(ids)})")
    else:
        lines.append("  (none — all planted errors caught)")
    lines += ["", f"FALSE POSITIVES: {s['false_positives']}"]
    for check, samples in s["fp_by_check"].items():
        for ex in samples:
            lines.append(f"  [{check}] {ex}")
    tv = s["trap_violations"]
    lines += ["", f"TRAP VIOLATIONS: {len(tv)}"
              + (f"  ({', '.join(tv)})" if tv else "  (none — traps respected)"), "=" * 66]
    return "\n".join(lines)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(
        description="Attribute each missed planted error to an architectural layer.")
    ap.add_argument("--gt", type=pathlib.Path,
                    default=ROOT / "eval" / "expected_greyharbor_s1.json")
    ap.add_argument("--map", type=pathlib.Path,
                    default=ROOT / "eval" / "failure_map_greyharbor_s1.json")
    ap.add_argument("--candidates", type=pathlib.Path, default=None,
                    help="Stage-2 extraction JSON (enables the extraction-vs-resolution split)")
    ap.add_argument("--resolved", type=pathlib.Path, default=None,
                    help="Stage-3 resolved assertions JSON (or use --db)")
    ap.add_argument("--db", default=None,
                    help="Postgres URL: attribute against the live stored graph instead of --resolved")
    ap.add_argument("--world", default="greyharbor_s1", help="world name (with --db)")
    ap.add_argument("--findings", type=pathlib.Path, required=True,
                    help="Stage-5 findings JSON")
    ap.add_argument("--json", action="store_true", help="emit the raw report as JSON")
    args = ap.parse_args(argv)

    gt = json.loads(args.gt.read_text(encoding="utf-8"))
    fmap = json.loads(args.map.read_text(encoding="utf-8"))
    findings = json.loads(args.findings.read_text(encoding="utf-8"))["findings"]
    if args.db:
        resolved = load_resolved_from_db(args.db, args.world)
    elif args.resolved:
        resolved = _load(args.resolved)
    else:
        ap.error("provide --resolved <file> or --db <url> --world <name>")
    candidates = _load(args.candidates) if args.candidates else None

    report = attribute(findings, candidates, resolved, gt, fmap)
    if args.db:
        from canon import ingest
        conn = ingest.connect(args.db)
        try:
            reground(conn, args.world, report, gt, fmap, gt.get("object_value_synonyms", {}))
        finally:
            try:
                conn.close()
            except Exception:
                pass
    print(json.dumps(report, indent=2) if args.json else render(report))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

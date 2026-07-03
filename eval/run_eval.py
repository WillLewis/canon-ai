#!/usr/bin/env python3
"""
Canon AI Phase 0 evaluation gate.

I/O CONTRACT

The pipeline hands this script two JSON files. Unknown fields are ignored, so
other workstreams can add metadata without breaking the gate.

Assertions input:

    {
      "assertions": [
        {
          "subject": "Mara Voss",
          "predicate": "cannot",
          "object_value": "drive",
          "object_entity": null,
          "scene": "E101/sc1",
          "confidence": 0.95
        }
      ]
    }

Required assertion fields: subject, predicate. One of object_value or
object_entity is required when the predicate has an object. Names should be
canonical after resolution, but the scorer also accepts fixture aliases.

Findings input:

    {
      "findings": [
        {
          "check": "dead_speaker",
          "severity": "critical",
          "explanation": "Tobias appears after dying in E101/sc5.",
          "sealed": false,
          "scene": "E102/sc3",
          "line": "Now you've counted everything. Happy?"
        }
      ]
    }

Required finding fields: check, severity, explanation. `sealed: true` suppresses
the finding from recall and false-positive scoring. Use `scene`, `where`,
`line`, `quote`, or `supporting_quote` when available; the scorer falls back to
the explanation text when locator metadata is absent.

Ground truth:

By default this file parses fixtures/greyharbor/answer-key.md for the Phase 0
two-episode gate: expected assertions, planted errors, expected notes, traps,
and thresholds. Extended fixture sets can pass a machine-readable key via
`--expected`, for example eval/expected_greyharbor_s1.json.

Metrics:

* extraction recall = matched expected assertions / expected assertions
* planted recall = matched planted continuity errors / planted errors
* check precision = matched planted errors / (matched planted errors + false
  positives)
* false positives per episode = unmatched live critical/warning findings /
  episode count

Exit code 0 means all Phase 0 bars pass: extraction recall >= 80%, all planted
errors found, false positives <= 2 per episode, and no trap fired.

Usage:

    python eval/run_eval.py --assertions out/assertions.json --findings out/findings.json
    python eval/run_eval.py --demo
    python eval/run_eval.py --dump-ground-truth
    python eval/run_eval.py --expected eval/expected_greyharbor_s1.json \\
        --assertions out/assertions.json --findings out/findings.json
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Any

EVAL_DIR = Path(__file__).resolve().parent
REPO_ROOT = EVAL_DIR.parent
DEFAULT_ANSWER_KEY = REPO_ROOT / "fixtures" / "greyharbor" / "answer-key.md"
SAMPLE_ASSERTIONS = EVAL_DIR / "sample_output" / "assertions.json"
SAMPLE_FINDINGS = EVAL_DIR / "sample_output" / "findings.json"

FINDING_SEVERITIES = {
    "dead_speaker": "critical",
    "presence_conflict": "critical",
    "premature_knowledge": "critical",
    "destroyed_location_use": "critical",
    "capability_violation": "warning",
    "idle_setup": "note",
    "dangling_reference": "note",
}

ENTITY_OBJECT_PREDICATES = {
    "located_at",
    "present_in_scene",
    "member_of",
    "possesses",
    "married_to",
    "parent_of",
    "sibling_of",
    "romantic_with",
    "allied_with",
    "enemy_of",
    "secret_of",
}

CRITICAL_OR_WARNING = {"critical", "warning"}

DEFAULT_OBJECT_SYNONYMS = {
    "drive": ["drive", "driving", "drive a truck", "operate a vehicle"],
    "ledger location": [
        "ledger location",
        "where the ledger is",
        "ledger under the chapel floor stone",
        "ledger hidden in chapel",
        "hiding place of the ledger",
        "where tobias hid the ledger",
    ],
    "two sets of numbers": [
        "two sets of numbers",
        "tobias keeps two sets of numbers",
        "tobias keeps two ledgers",
        "two ledgers",
        "keeps two sets of books",
    ],
    "Tobias keeps two sets of numbers": [
        "two sets of numbers",
        "tobias keeps two sets of numbers",
        "tobias keeps two ledgers",
        "two ledgers",
        "keeps two sets of books",
    ],
    "real ledger not in office": [
        "real ledger not in office",
        "the real one's not in that office",
        "real ledger is elsewhere",
    ],
    "find danny": ["find danny", "find her brother", "find him", "i'll find you danny"],
    "find Danny": ["find danny", "find her brother", "find him", "i'll find you danny"],
    "c.b. payment": [
        "c.b. payment",
        "c.b. payment in ledger",
        "cole's payment in ledger",
        "c.b. four hundred",
        "cole's name in the book",
        "deputy in the ledger",
    ],
    "c.b. payment in ledger": [
        "c.b. payment",
        "c.b. payment in ledger",
        "cole's payment in ledger",
        "c.b. four hundred",
        "cole's name in the book",
        "deputy in the ledger",
    ],
    "deputy": ["deputy"],
    "harbormaster": ["harbormaster"],
    "harbor cook": ["harbor cook", "cook"],
    "stove pipe cache": [
        "stove pipe cache",
        "ledger in edda's stove pipe",
        "behind the stove pipe",
        "pipe sleeve behind the stove",
        "where mara hid the ledger",
    ],
    "ferryman": ["ferryman", "ferry man", "runs the ferry", "blue gull's engine"],
    "danny alive": ["danny alive", "danny is alive", "i'm breathing", "her brother is alive"],
    "Danny alive": ["danny alive", "danny is alive", "i'm breathing", "her brother is alive"],
    "blue hook signal": [
        "blue hook signal",
        "blue fishhook",
        "blue hook means breathing",
        "blue hook means your brother is alive",
        "fishhook means breathing",
    ],
    "swim": ["swim", "swimming", "can't swim anymore", "cannot swim", "if i go under i stay under"],
    "radio man": ["radio man", "harbor's radio man", "radio mast", "receiver"],
    "tide clerk": ["tide clerk", "permit window", "tide permits"],
    "find black osprey": ["find the black osprey", "black osprey", "find that boat", "trace the black osprey"],
    "find Black Osprey": ["find the black osprey", "black osprey", "find that boat", "trace the black osprey"],
    "right hand": ["right hand", "can't use your right hand", "cannot use right hand", "bandaged hand", "burn dressing"],
    "tobias killer": ["cole shot him", "cole killed tobias", "tobias's killer", "outside the chapel"],
    "Tobias killer": ["cole shot him", "cole killed tobias", "tobias's killer", "outside the chapel"],
    "state investigator": ["state investigator", "state police", "agent hale"],
    "bait clerk": ["bait clerk", "sorts hooks behind the counter", "bait shop"],
    "hale owns black osprey": ["hale's shell company", "hale owns black osprey", "black osprey is hale's"],
    "Hale owns Black Osprey": ["hale's shell company", "hale owns black osprey", "black osprey is hale's"],
}

STOP_WORDS = {
    "a",
    "an",
    "and",
    "as",
    "at",
    "by",
    "for",
    "from",
    "he",
    "her",
    "his",
    "in",
    "is",
    "it",
    "of",
    "on",
    "or",
    "she",
    "that",
    "the",
    "their",
    "there",
    "they",
    "this",
    "to",
    "was",
    "with",
    "you",
}


def norm(s: Any) -> str:
    """Normalize text for deterministic fixture matching."""
    return re.sub(r"\s+", " ", re.sub(r"[^a-z0-9 ]", " ", str(s or "").lower())).strip()


def _strip_md(s: Any) -> str:
    s = str(s or "").strip()
    s = re.sub(r"^\s*`|`\s*$", "", s)
    s = s.replace("**", "")
    return s.strip()


def _split_section(text: str, start: str, end: str | None = None) -> str:
    start_i = text.find(start)
    if start_i < 0:
        return ""
    start_i += len(start)
    end_i = text.find(end, start_i) if end else -1
    return text[start_i:] if end_i < 0 else text[start_i:end_i]


def _table_rows(section: str) -> list[list[str]]:
    rows: list[list[str]] = []
    for line in section.splitlines():
        raw = line.strip()
        if not raw.startswith("|") or not raw.endswith("|"):
            continue
        cells = [_strip_md(c.strip()) for c in raw.strip("|").split("|")]
        if not cells or all(re.fullmatch(r"-+", c.replace(" ", "")) for c in cells):
            continue
        if cells[0].lower() in {"canonical", "#"}:
            continue
        rows.append(cells)
    return rows


def _split_aliases(cell: str) -> list[str]:
    cell = cell.split("\u2014")[0]
    cell = re.sub(r"\([^)]*\)", "", cell)
    cell = cell.replace('"', "")
    return [p.strip() for p in cell.split(",") if p.strip()]


def _parse_entities(text: str) -> tuple[list[dict[str, Any]], dict[str, str]]:
    section = _split_section(text, "## Entities", "## Expected core assertions")
    entities: list[dict[str, Any]] = []
    alias_map: dict[str, str] = {}
    for canonical, kind, aliases in _table_rows(section):
        ent = {
            "canonical": canonical,
            "kind": kind,
            "aliases": _split_aliases(aliases),
        }
        entities.append(ent)
        alias_map[norm(canonical)] = canonical
        for alias in ent["aliases"]:
            alias_map[norm(alias)] = canonical
            stripped = re.sub(r"^(the|a|an|your|her|his)\s+", "", alias, flags=re.IGNORECASE).strip()
            if stripped and stripped != alias:
                alias_map.setdefault(norm(stripped), canonical)
        if kind in {"location", "object"}:
            words = [w for w in re.split(r"\s+", canonical) if w.lower() not in {"on", "the", "of"}]
            if words:
                alias_map.setdefault(norm(words[-1]), canonical)
    return entities, alias_map


def canon_entity(name: Any, alias_map: dict[str, str]) -> Any:
    if name is None:
        return None
    s = str(name).strip()
    return alias_map.get(norm(s), s)


def _split_args(arg_text: str) -> list[tuple[str, bool]]:
    args: list[tuple[str, bool]] = []
    buf: list[str] = []
    quoted = False
    current_quoted = False
    for ch in arg_text:
        if ch == '"':
            quoted = not quoted
            current_quoted = True
            continue
        if ch == "," and not quoted:
            args.append(("".join(buf).strip(), current_quoted))
            buf = []
            current_quoted = False
            continue
        buf.append(ch)
    if buf or current_quoted:
        args.append(("".join(buf).strip(), current_quoted))
    return args


def _assertion_exprs(line: str) -> list[tuple[str, str]]:
    exprs: list[tuple[str, str]] = []
    for m in re.finditer(r"`([a-z_]+)\(([^`]+)\)`", line, flags=re.IGNORECASE):
        exprs.append((m.group(1), m.group(2)))
    return exprs


def _where(text: str) -> str | None:
    m = re.search(r"\b(E\d{3})\s*/\s*sc(\d+)\b", text, flags=re.IGNORECASE)
    return f"{m.group(1).upper()}/sc{int(m.group(2))}" if m else None


def _line_quote(text: str) -> str | None:
    m = re.search(r"line:\s*`([^`]+)`", text, flags=re.IGNORECASE)
    if m:
        return m.group(1).strip()
    m = re.search(r'\("([^"]{8,})"\)', text)
    return m.group(1).strip() if m else None


def _parse_assertion_expr(
    predicate: str,
    arg_text: str,
    alias_map: dict[str, str],
    assertion_id: str,
    source_line: str,
) -> dict[str, Any] | None:
    args = _split_args(arg_text)
    if not args:
        return None
    subject = canon_entity(args[0][0], alias_map)
    item: dict[str, Any] = {
        "id": assertion_id,
        "subject": subject,
        "predicate": predicate,
        "object_value": None,
    }
    if len(args) >= 2:
        obj, was_quoted = args[1]
        if predicate.lower() in ENTITY_OBJECT_PREDICATES and not was_quoted:
            item["object_entity"] = canon_entity(obj, alias_map)
            item.pop("object_value", None)
        else:
            item["object_value"] = obj
    where = _where(source_line)
    quote = _line_quote(source_line)
    if where:
        item["where"] = where
    if quote:
        item["line"] = quote
    return item


def _parse_expected_assertions(text: str, alias_map: dict[str, str]) -> list[dict[str, Any]]:
    section = _split_section(text, "## Expected core assertions", "## Planted errors")
    expected: list[dict[str, Any]] = []
    next_id = 1
    for raw in section.splitlines():
        if not re.match(r"\s*\d+\.\s+", raw):
            continue
        for predicate, arg_text in _assertion_exprs(raw):
            assertion = _parse_assertion_expr(
                predicate,
                arg_text,
                alias_map,
                f"A{next_id}",
                raw,
            )
            if assertion:
                expected.append(assertion)
                next_id += 1
    return expected


def _short_name(text: str) -> str:
    cleaned = _strip_md(text)
    if not cleaned:
        return cleaned
    words = cleaned.split()
    if len(words) >= 2 and words[1][0:1].isupper() and words[0] not in {"Fog", "Salt", "Blue"}:
        return words[0]
    return cleaned


def _cannot_terms(text: str) -> list[str]:
    m = re.search(r"`cannot\(([^,]+),\s*\"?([^\"`)]+)\"?\)`", text, flags=re.IGNORECASE)
    if not m:
        return []
    return [_short_name(m.group(1).strip()), m.group(2).strip()]


def _finding_mentions(check: str, line: str, truth: str, alias_map: dict[str, str]) -> list[str]:
    combined = f"{line} {truth}"
    cannot = _cannot_terms(combined)
    if cannot:
        return cannot

    if check == "dead_speaker":
        m = re.search(r"\b([A-Z][A-Za-z]+)\s+(?:speaks|steps|stands|appears|taps)\b", truth)
        if m:
            return [m.group(1)]
        m = re.match(r"\s*([A-Z][A-Za-z]+)\b", line)
        return [m.group(1)] if m else []

    if check == "premature_knowledge":
        terms: list[str] = []
        m = re.search(r"\b([A-Z][A-Za-z]+)\s+(?:states|knows|uses|appears)\b", truth)
        if m:
            terms.append(m.group(1))
        elif re.search(r"\bState investigator\b", line):
            terms.append("Hale")
        for marker in ("blue hook", "stove", "ledger"):
            if marker in norm(combined):
                terms.append(marker)
                break
        return terms

    if check == "destroyed_location_use":
        for marker in ("Fog Bell Buoy", "Salt House", "Chapel"):
            if norm(marker) in norm(combined):
                return [marker]

    if check == "presence_conflict":
        terms = []
        for marker in ("Danny", "Rafi", "Blue Gull", "Courthouse", "Weather", "Kitchen"):
            if norm(marker) in norm(combined):
                terms.append(marker)
        return terms

    caps = re.findall(r"\b[A-Z][A-Za-z]+(?:\s+[A-Z][A-Za-z]+)?\b", combined)
    return [_short_name(canon_entity(caps[0], alias_map))] if caps else []


def _parse_planted_errors(text: str, alias_map: dict[str, str]) -> list[dict[str, Any]]:
    section = _split_section(text, "## Planted errors", "## Expected notes")
    findings: list[dict[str, Any]] = []
    for cells in _table_rows(section):
        if len(cells) < 5 or not re.fullmatch(r"P\d+", cells[0]):
            continue
        fid, check, where, line, truth = cells[:5]
        findings.append({
            "id": fid,
            "check": check,
            "severity": FINDING_SEVERITIES.get(check, "warning"),
            "where": _where(where) or where,
            "line": line,
            "ground_truth": truth,
            "must_mention": _finding_mentions(check, line, truth, alias_map),
        })
    return findings


def _parse_expected_notes(text: str, alias_map: dict[str, str]) -> list[dict[str, Any]]:
    section = _split_section(text, "## Expected notes", "## False-positive budget")
    notes: list[dict[str, Any]] = []
    next_id = 1
    for raw in section.splitlines():
        if not raw.lstrip().startswith("-"):
            continue
        m = re.search(r"`([^`]+)`\s*:", raw)
        if not m:
            continue
        check = m.group(1).strip()
        line = _line_quote(raw)
        mentions: list[str] = []
        exprs = _assertion_exprs(raw)
        if exprs:
            parsed = _parse_assertion_expr(exprs[0][0], exprs[0][1], alias_map, "tmp", raw)
            obj = parsed.get("object_value") if parsed else None
            if obj:
                caps = re.findall(r"\b[A-Z][A-Za-z]+\b", str(obj))
                mentions.append(caps[-1] if caps else str(obj))
        else:
            tail = raw[m.end():]
            caps = re.findall(r"\b[A-Z][A-Za-z]+(?:\s+[A-Z][A-Za-z]+)?\b", tail)
            if caps:
                mentions.append(_short_name(caps[0]))
        notes.append({
            "id": f"N{next_id}",
            "check": check,
            "severity": "note",
            "where": _where(raw),
            "line": line,
            "must_mention": mentions,
        })
        next_id += 1
    return notes


def _parse_traps(text: str) -> list[dict[str, Any]]:
    section = _split_section(text, "## False-positive budget", "## Season 1 extension note")
    traps: list[dict[str, Any]] = []
    for raw in section.splitlines():
        m = re.match(r"\s*-\s*(T\d+):\s*(.+)", raw)
        if not m:
            continue
        tid, desc = m.groups()
        check = "premature_knowledge" if tid == "T1" or "premature_knowledge" in desc else "*"
        if tid == "T1":
            must_not = ["Mara", "arithmetic"]
        elif tid == "T2":
            must_not = ["Edda", "office"]
        else:
            must_not = []
        traps.append({
            "id": tid,
            "description": _strip_md(desc),
            "check": check,
            "must_not_mention_all": must_not,
            "where": _where(raw),
            "line": _line_quote(raw),
        })
    return traps


def _object_synonyms(expected_assertions: list[dict[str, Any]]) -> dict[str, list[str]]:
    synonyms = {k: list(v) for k, v in DEFAULT_OBJECT_SYNONYMS.items()}
    for assertion in expected_assertions:
        value = assertion.get("object_value")
        if value:
            synonyms.setdefault(value, [value])
    return synonyms


def _episode_count(expected_assertions: list[dict[str, Any]], expected_findings: list[dict[str, Any]]) -> int:
    episodes = set()
    for item in expected_assertions + expected_findings:
        where = item.get("where")
        if where:
            episodes.add(where.split("/")[0].upper())
    return max(1, len(episodes))


def parse_answer_key(path: Path = DEFAULT_ANSWER_KEY) -> dict[str, Any]:
    """Parse the Phase 0 two-episode answer key from markdown."""
    text = path.read_text()
    entities, alias_map = _parse_entities(text)
    expected_assertions = _parse_expected_assertions(text, alias_map)
    expected_findings = _parse_planted_errors(text, alias_map)
    expected_notes = _parse_expected_notes(text, alias_map)
    traps = _parse_traps(text)
    return {
        "_comment": f"Parsed from {path}",
        "entities": entities,
        "expected_assertions": expected_assertions,
        "alias_map": alias_map,
        "object_value_synonyms": _object_synonyms(expected_assertions),
        "expected_findings": expected_findings,
        "expected_notes": expected_notes,
        "trap_findings_must_not_flag": traps,
        "thresholds": {
            "extraction_recall_min": 0.80,
            "planted_findings_recall_min": 1.0,
            "false_positives_per_episode_max": 2,
            "episode_count": _episode_count(expected_assertions, expected_findings),
        },
    }


def _synonym_lookup(synonyms: dict[str, list[str]]) -> dict[str, list[str]]:
    out: dict[str, list[str]] = {}
    for key, vals in synonyms.items():
        merged = list(dict.fromkeys([key] + list(vals or [])))
        norm_key = norm(key)
        existing = out.get(norm_key, [])
        out[norm_key] = list(dict.fromkeys(existing + merged))
    return out


def value_matches(expected_key: Any, actual_value: Any, synonyms: dict[str, list[str]]) -> bool:
    if expected_key is None:
        return actual_value in (None, "")
    actual_norm = norm(actual_value)
    if not actual_norm:
        return False
    syns = _synonym_lookup(synonyms)
    for syn in syns.get(norm(expected_key), [str(expected_key)]):
        syn_norm = norm(syn)
        if syn_norm and (syn_norm in actual_norm or actual_norm in syn_norm):
            return True
    return False


def _entity_value_matches(expected_entity: str, actual: dict[str, Any], alias_map: dict[str, str]) -> bool:
    for field in ("object_entity", "object_value"):
        value = actual.get(field)
        if value and canon_entity(value, alias_map) == expected_entity:
            return True
    return False


def _assertion_matches(exp: dict[str, Any], actual: dict[str, Any], alias_map: dict[str, str], synonyms: dict[str, list[str]]) -> bool:
    if canon_entity(actual.get("subject"), alias_map) != exp["subject"]:
        return False
    if norm(actual.get("predicate")) != norm(exp["predicate"]):
        return False
    if exp.get("object_entity"):
        return _entity_value_matches(exp["object_entity"], actual, alias_map)
    if "object_value" in exp:
        return value_matches(exp.get("object_value"), actual.get("object_value"), synonyms)
    return not actual.get("object_value") and not actual.get("object_entity")


def score_extraction(actual_assertions: list[dict[str, Any]]) -> tuple[float, list[str], list[str]]:
    alias_map = {norm(k): v for k, v in GT["alias_map"].items()}
    synonyms = GT.get("object_value_synonyms", {})
    matched: list[str] = []
    missed: list[str] = []
    consumed: set[int] = set()
    for exp in GT["expected_assertions"]:
        hit = None
        for i, act in enumerate(actual_assertions):
            if i in consumed:
                continue
            if _assertion_matches(exp, act, alias_map, synonyms):
                hit = i
                break
        if hit is None:
            missed.append(exp["id"])
        else:
            consumed.add(hit)
            matched.append(exp["id"])
    total = len(GT["expected_assertions"])
    recall = len(matched) / total if total else 1.0
    return recall, matched, missed


def _truthy(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return False
    return norm(value) in {"1", "true", "yes", "sealed"}


def _finding_text(finding: dict[str, Any]) -> str:
    fields = [
        "check",
        "severity",
        "explanation",
        "where",
        "scene",
        "line",
        "quote",
        "supporting_quote",
        "quoted_line",
        "citation",
        "source",
    ]
    return " ".join(str(finding.get(k, "")) for k in fields if finding.get(k) is not None)


def _mentions(finding: dict[str, Any], terms: list[str]) -> bool:
    text = norm(_finding_text(finding))
    return all(norm(t) in text for t in terms if norm(t))


def _finding_scene_refs(finding: dict[str, Any]) -> set[str]:
    refs: set[str] = set()
    for key in ("where", "scene", "explanation", "citation", "source"):
        val = finding.get(key)
        if not val:
            continue
        for m in re.finditer(r"\b(E\d{3})\s*/\s*sc(\d+)\b", str(val), flags=re.IGNORECASE):
            refs.add(f"{m.group(1).upper()}/sc{int(m.group(2))}")
    return refs


def _tokens(text: Any) -> set[str]:
    return {t for t in norm(text).split() if t and t not in STOP_WORDS}


def _line_overlap(expected_line: str | None, finding: dict[str, Any]) -> bool:
    if not expected_line:
        return False
    expected = _tokens(expected_line)
    if len(expected) < 3:
        return False
    actual = _tokens(_finding_text(finding))
    return len(expected & actual) / len(expected) >= 0.55


def _check_matches(actual_check: Any, expected_check: str) -> bool:
    return norm(actual_check) == norm(expected_check)


def _finding_matches_expected(finding: dict[str, Any], expected: dict[str, Any]) -> bool:
    if not _check_matches(finding.get("check"), expected["check"]):
        return False
    expected_where = expected.get("where")
    expected_where = _where(str(expected_where)) if expected_where else None
    if expected_where and expected_where in _finding_scene_refs(finding):
        return True
    if _line_overlap(expected.get("line"), finding):
        return True
    return _mentions(finding, expected.get("must_mention") or [])


def _trap_matches(finding: dict[str, Any], trap: dict[str, Any]) -> bool:
    check = trap.get("check")
    if check and check != "*" and not _check_matches(finding.get("check"), check):
        return False
    return _mentions(finding, trap.get("must_not_mention_all") or [])


def score_findings(actual_findings: list[dict[str, Any]]) -> tuple[list[str], list[str], list[dict[str, Any]], list[tuple[str, str]]]:
    live = [f for f in actual_findings if not _truthy(f.get("sealed"))]
    found: list[str] = []
    missed: list[str] = []
    consumed: set[int] = set()

    for exp in GT.get("expected_findings", []):
        hit = next(
            (i for i, f in enumerate(live) if i not in consumed and _finding_matches_expected(f, exp)),
            None,
        )
        if hit is None:
            missed.append(exp["id"])
        else:
            consumed.add(hit)
            found.append(exp["id"])

    for exp in GT.get("expected_notes", []):
        hit = next(
            (
                i
                for i, f in enumerate(live)
                if i not in consumed
                and norm(f.get("severity")) == "note"
                and _finding_matches_expected(f, exp)
            ),
            None,
        )
        if hit is None:
            missed.append(exp["id"])
        else:
            consumed.add(hit)
            found.append(exp["id"])

    fps = [
        f
        for i, f in enumerate(live)
        if i not in consumed and norm(f.get("severity")) in CRITICAL_OR_WARNING
    ]

    trap_hits: list[tuple[str, str]] = []
    for trap in GT.get("trap_findings_must_not_flag", []):
        for f in live:
            if _trap_matches(f, trap):
                trap_hits.append((trap["id"], str(f.get("explanation", ""))[:120]))

    return found, missed, fps, trap_hits


def _fmt_assertion(a: dict[str, Any]) -> str:
    obj = ""
    if a.get("object_entity"):
        obj = f", {a['object_entity']}"
    elif "object_value" in a and a.get("object_value") is not None:
        obj = f", \"{a['object_value']}\""
    where = f" @ {a['where']}" if a.get("where") else ""
    return f"{a['id']} {a['predicate']}({a['subject']}{obj}){where}"


def _fmt_expected_finding(f: dict[str, Any]) -> str:
    where = f" @ {f['where']}" if f.get("where") else ""
    line = f" | {f['line']}" if f.get("line") else ""
    return f"{f['id']} {f['check']}{where}{line}"


def _fmt_actual_finding(f: dict[str, Any]) -> str:
    scene = next(iter(_finding_scene_refs(f)), None)
    where = f" @ {scene}" if scene else ""
    explanation = str(f.get("explanation", "")).strip()
    if len(explanation) > 160:
        explanation = explanation[:157] + "..."
    return f"{norm(f.get('severity')) or '?'} {f.get('check')}{where}: {explanation}"


def _load_json(path: Path) -> dict[str, Any]:
    with path.open() as f:
        return json.load(f)


def _load_ground_truth(expected: Path | None, answer_key: Path) -> dict[str, Any]:
    if expected:
        return _load_json(expected)
    return parse_answer_key(answer_key)


def _print_report(
    *,
    recall: float,
    matched: list[str],
    missed_assertions: list[str],
    found: list[str],
    missed_findings: list[str],
    fps: list[dict[str, Any]],
    trap_hits: list[tuple[str, str]],
) -> bool:
    thresholds = GT["thresholds"]
    planted_ids = {e["id"] for e in GT.get("expected_findings", [])}
    planted_found = [fid for fid in found if fid in planted_ids]
    planted_total = len(planted_ids)
    planted_recall = len(planted_found) / planted_total if planted_total else 1.0
    precision_denom = len(planted_found) + len(fps)
    check_precision = len(planted_found) / precision_denom if precision_denom else 1.0
    episode_count = thresholds["episode_count"]
    fp_per_episode = len(fps) / episode_count if episode_count else 0.0

    missed_assertion_objs = [
        a for a in GT.get("expected_assertions", []) if a["id"] in set(missed_assertions)
    ]
    missed_finding_objs = [
        f for f in GT.get("expected_findings", []) if f["id"] in set(missed_findings)
    ]

    gates = [
        (
            f"extraction recall >= {thresholds['extraction_recall_min']:.0%}",
            recall >= thresholds["extraction_recall_min"],
        ),
        (
            "all planted errors found",
            planted_recall >= thresholds["planted_findings_recall_min"],
        ),
        (
            f"false positives <= {thresholds['false_positives_per_episode_max']}/episode",
            fp_per_episode <= thresholds["false_positives_per_episode_max"],
        ),
        ("no trap flags fired", not trap_hits),
    ]

    print("=" * 72)
    print("CANON AI PHASE 0 EVAL")
    print("=" * 72)
    print(
        f"Extraction recall : {recall:.1%} "
        f"({len(matched)}/{len(GT.get('expected_assertions', []))})"
    )
    print(
        f"Planted recall    : {planted_recall:.1%} "
        f"({len(planted_found)}/{planted_total})"
    )
    print(f"Check precision   : {check_precision:.1%} ({len(planted_found)} TP / {len(fps)} FP)")
    print(f"False positives   : {len(fps)} total ({fp_per_episode:.2f}/episode)")

    if missed_assertion_objs:
        print("")
        print("Missed assertions:")
        for a in missed_assertion_objs:
            print(f"  - {_fmt_assertion(a)}")

    if missed_finding_objs:
        print("")
        print("Missed planted errors:")
        for f in missed_finding_objs:
            print(f"  - {_fmt_expected_finding(f)}")

    if fps:
        print("")
        print("Spurious critical/warning findings:")
        for f in fps:
            print(f"  - {_fmt_actual_finding(f)}")

    if trap_hits:
        print("")
        print("Trap violations:")
        for tid, text in trap_hits:
            print(f"  - {tid}: {text}")

    print("")
    print("Gates:")
    ok = True
    for label, passed in gates:
        ok = ok and passed
        print(f"  [{'PASS' if passed else 'FAIL'}] {label}")
    print("=" * 72)
    print("PHASE 0 GATES: " + ("ALL PASS" if ok else "NOT YET"))
    return ok


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Score Canon AI pipeline output against Greyharbor ground truth.")
    parser.add_argument("--assertions", type=Path, help="Pipeline assertion JSON.")
    parser.add_argument("--findings", type=Path, help="Pipeline check findings JSON.")
    parser.add_argument("--demo", action="store_true", help="Score bundled deliberately degraded sample output.")
    parser.add_argument("--answer-key", type=Path, default=DEFAULT_ANSWER_KEY, help="Markdown answer key to parse.")
    parser.add_argument("--expected", type=Path, help="Machine-readable ground-truth JSON. Overrides --answer-key.")
    parser.add_argument("--dump-ground-truth", action="store_true", help="Print parsed/loaded ground truth JSON and exit.")
    args = parser.parse_args(argv)

    global GT
    GT = _load_ground_truth(args.expected, args.answer_key)

    if args.dump_ground_truth:
        print(json.dumps(GT, indent=2, sort_keys=True))
        return 0

    if args.demo:
        args.assertions = SAMPLE_ASSERTIONS
        args.findings = SAMPLE_FINDINGS

    if not args.assertions or not args.findings:
        parser.error("provide --assertions and --findings, or use --demo")

    assertions_doc = _load_json(args.assertions)
    findings_doc = _load_json(args.findings)
    actual_assertions = assertions_doc.get("assertions")
    actual_findings = findings_doc.get("findings")
    if not isinstance(actual_assertions, list):
        raise SystemExit(f"{args.assertions} must contain an 'assertions' array")
    if not isinstance(actual_findings, list):
        raise SystemExit(f"{args.findings} must contain a 'findings' array")

    recall, matched, missed_assertions = score_extraction(actual_assertions)
    found, missed_findings, fps, trap_hits = score_findings(actual_findings)
    ok = _print_report(
        recall=recall,
        matched=matched,
        missed_assertions=missed_assertions,
        found=found,
        missed_findings=missed_findings,
        fps=fps,
        trap_hits=trap_hits,
    )
    return 0 if ok else 1


try:
    GT = parse_answer_key(DEFAULT_ANSWER_KEY)
except FileNotFoundError:
    GT = {
        "expected_assertions": [],
        "alias_map": {},
        "object_value_synonyms": {},
        "expected_findings": [],
        "expected_notes": [],
        "trap_findings_must_not_flag": [],
        "thresholds": {
            "extraction_recall_min": 0.80,
            "planted_findings_recall_min": 1.0,
            "false_positives_per_episode_max": 2,
            "episode_count": 1,
        },
    }


if __name__ == "__main__":
    sys.exit(main())

"""Per-world structured rules (P3-RULES, Wave 4) — model, compiler, runner.

Exactly three rule kinds, all fully structured (the optional writer label is
the only free text anywhere):

    CANNOT     "{entity | any entity of trait T} cannot {predicate} [{object}]"
               -> a violation exists when a matching assertion exists; the
               finding cites the assertion's establishing scene.
    ONLY       "only {entity} may {predicate} [{object}]"
               -> a violation exists when anyone else does it.
    EXCEPTION  suppress one universal check family for {entity}, optionally
               inside a story-position range — the surgical version of the
               per-world check toggle (dead_speaker off for the ghost, only
               for Marcus). Exceptions never query; they filter findings.

Doctrine: this is NOT a free-form constraint language. Every rule validates
against the closed predicate vocabulary (extract/schema.py) at construction —
an unknown predicate, an ambiguous subject scope, or an inverted range raises
ValueError, never a warning. A rule that can't compile unambiguously is
rejected at creation, never guessed at.

CANNOT/ONLY compile to parameterized SQL over the assertions table in the
exact finding shape db/checks.sql produces (check_name, severity, explanation,
scene_id, assertion_a, assertion_b). Severity defaults to 'warning'. Sealed
things never flag twice over: the violating assertion's own status is checked,
and the seals table is anti-joined on the emitted check name — a writer's
"intentional" ruling suppresses the rule exactly as it suppresses a check.

Zero LLM calls anywhere in this module (enforced by a grep test in
tests/test_rules.py, same as canon/export). SQL judges; nothing here phrases
beyond deterministic format() templates naming the rule.
"""

from __future__ import annotations

import argparse
import sys
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import ClassVar

from extract.schema import INTRANSITIVE, PREDICATES

from . import rules_store
from .check import _COLUMNS as _FINDING_COLUMNS
from .check import split_checks

RULE_KINDS = ("cannot", "only", "exception")
SEVERITIES = ("critical", "warning", "note")
DEFAULT_SEVERITY = "warning"

# Assertion statuses a rule never flags: 'sealed' is writer-marked intentional
# (the db/checks.sql convention), 'rejected' is a fact the writer already
# ruled wrong in the confirm queue, 'retconned' is superseded. Flagging any of
# them would be a false positive by definition (CLAUDE.md doctrine).
_MUTED = "('sealed', 'rejected', 'retconned')"

_CHECKS_SQL_PATH = Path(__file__).resolve().parents[1] / "db" / "checks.sql"


@lru_cache(maxsize=1)
def check_families() -> tuple:
    """The closed set of check families an EXCEPTION may suppress: every named
    check in db/checks.sql plus the two rule-generated families."""
    names = [n for n, _ in split_checks(_CHECKS_SQL_PATH.read_text(encoding="utf-8")) if n]
    return tuple(names) + (CannotRule.check_name, OnlyRule.check_name)


# ---------------------------------------------------------------------------
# Validation helpers — ValueError, never a warning.
# ---------------------------------------------------------------------------

def _require_predicate(predicate) -> None:
    if predicate not in PREDICATES:
        raise ValueError(
            f"unknown predicate {predicate!r}: rules may only reference the closed "
            f"vocabulary ({', '.join(PREDICATES)})")


def _require_severity(severity) -> None:
    if severity not in SEVERITIES:
        raise ValueError(f"severity must be one of {SEVERITIES}, got {severity!r}")


def _opt_int(value, field: str):
    if value is None or value == "":
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        raise ValueError(f"{field} must be an integer, got {value!r}") from None


def _req_int(value, field: str) -> int:
    out = _opt_int(value, field)
    if out is None:
        raise ValueError(f"{field} is required")
    return out


def _clean_label(label):
    label = (label or "").strip()
    return label or None


# The SQL tail shared by CANNOT and ONLY: anti-join the seals table on the
# emitted check name so a sealed rule finding never flags again (same key
# canon/check.py and ui/db.py::seal_finding use; rule findings carry no
# assertion_b, so the seal row's coalesce_b is 0).
_SEALS_ANTI_JOIN = """
  and not exists (
    select 1 from seals se
    where se.world_id = a.world_id
      and se.check_name = %(check_name)s
      and se.assertion_a = a.id
      and se.coalesce_b = 0
  )"""


@dataclass(frozen=True)
class CannotRule:
    """{entity | any entity of trait T} cannot {predicate} [{object}]."""

    kind: ClassVar[str] = "cannot"
    check_name: ClassVar[str] = "rule_cannot"

    predicate: str
    subject_entity_id: int | None = None   # exactly one of these two scopes
    subject_trait: str | None = None       # matches entities holding an active 'trait' assertion
    object_entity_id: int | None = None    # optional: narrow to one object entity
    severity: str = DEFAULT_SEVERITY
    label: str | None = None
    rule_id: str | None = None

    def __post_init__(self):
        _require_predicate(self.predicate)
        _require_severity(self.severity)
        has_entity = self.subject_entity_id is not None
        has_trait = self.subject_trait is not None and str(self.subject_trait).strip() != ""
        if has_entity == has_trait:  # both or neither: ambiguous, reject
            raise ValueError(
                "a cannot-rule needs exactly one subject scope: an entity or a trait")
        if self.object_entity_id is not None and self.predicate in INTRANSITIVE:
            raise ValueError(
                f"predicate {self.predicate!r} takes no object; drop the object "
                "or pick a transitive predicate")

    @property
    def name(self) -> str:
        if self.label:
            return self.label
        scope = (f"any entity of trait {self.subject_trait!r}" if self.subject_trait
                 else f"entity #{self.subject_entity_id}")
        tail = f" entity #{self.object_entity_id}" if self.object_entity_id else ""
        return f"{scope} cannot {self.predicate}{tail}"

    def params(self) -> dict:
        return {
            "predicate": self.predicate,
            "subject_entity_id": self.subject_entity_id,
            "subject_trait": self.subject_trait,
            "object_entity_id": self.object_entity_id,
            "severity": self.severity,
        }

    def compile(self) -> tuple:
        """(sql, params) — parameterized psycopg SQL; caller adds world_id."""
        params = {
            "check_name": self.check_name,
            "severity": self.severity,
            "rule_name": self.name,
            "predicate": self.predicate,
        }
        if self.subject_trait is not None:
            subject_clause = f"""
  and exists (
    select 1 from assertions t
    where t.world_id = a.world_id
      and t.subject_id = a.subject_id
      and t.predicate = 'trait'
      and t.object_value = %(subject_trait)s
      and t.polarity
      and t.status not in {_MUTED}
  )"""
            params["subject_trait"] = self.subject_trait
        else:
            subject_clause = "\n  and a.subject_id = %(subject_entity_id)s"
            params["subject_entity_id"] = self.subject_entity_id
        object_clause = ""
        if self.object_entity_id is not None:
            object_clause = "\n  and a.object_id = %(object_entity_id)s"
            params["object_entity_id"] = self.object_entity_id
        sql = f"""
select
  %(check_name)s as check_name,
  %(severity)s as severity,
  format('%%s %%s in %%s (pos %%s), which the rule "%%s" forbids: "%%s".',
         subj.name,
         %(predicate)s || coalesce(' ' || obj.name,
                                   coalesce(' "' || nullif(a.object_value, '') || '"', '')),
         coalesce(s.slug, 'scene ' || s.id::text),
         s.story_position,
         %(rule_name)s,
         coalesce(nullif(a.supporting_quote, ''), 'no quote')) as explanation,
  s.id as scene_id,
  a.id as assertion_a,
  null::bigint as assertion_b
from assertions a
join entities subj on subj.id = a.subject_id
join scenes s on s.id = a.established_in_scene
left join entities obj on obj.id = a.object_id
where a.world_id = %(world_id)s
  and a.predicate = %(predicate)s
  and a.polarity
  and a.status not in {_MUTED}{subject_clause}{object_clause}{_SEALS_ANTI_JOIN}
"""
        return sql, params


@dataclass(frozen=True)
class OnlyRule:
    """only {entity} may {predicate} [{object}] — anyone else doing it flags."""

    kind: ClassVar[str] = "only"
    check_name: ClassVar[str] = "rule_only"

    entity_id: int
    predicate: str
    object_entity_id: int | None = None
    severity: str = DEFAULT_SEVERITY
    label: str | None = None
    rule_id: str | None = None

    def __post_init__(self):
        _require_predicate(self.predicate)
        _require_severity(self.severity)
        if self.entity_id is None:
            raise ValueError("an only-rule needs the one permitted entity")
        if self.object_entity_id is not None and self.predicate in INTRANSITIVE:
            raise ValueError(
                f"predicate {self.predicate!r} takes no object; drop the object "
                "or pick a transitive predicate")

    @property
    def name(self) -> str:
        if self.label:
            return self.label
        tail = f" entity #{self.object_entity_id}" if self.object_entity_id else ""
        return f"only entity #{self.entity_id} may {self.predicate}{tail}"

    def params(self) -> dict:
        return {
            "entity_id": self.entity_id,
            "predicate": self.predicate,
            "object_entity_id": self.object_entity_id,
            "severity": self.severity,
        }

    def compile(self) -> tuple:
        """(sql, params) — parameterized psycopg SQL; caller adds world_id."""
        params = {
            "check_name": self.check_name,
            "severity": self.severity,
            "rule_name": self.name,
            "predicate": self.predicate,
            "entity_id": self.entity_id,
        }
        object_clause = ""
        if self.object_entity_id is not None:
            object_clause = "\n  and a.object_id = %(object_entity_id)s"
            params["object_entity_id"] = self.object_entity_id
        sql = f"""
select
  %(check_name)s as check_name,
  %(severity)s as severity,
  format('%%s %%s in %%s (pos %%s), but the rule "%%s" allows only %%s: "%%s".',
         subj.name,
         %(predicate)s || coalesce(' ' || obj.name,
                                   coalesce(' "' || nullif(a.object_value, '') || '"', '')),
         coalesce(s.slug, 'scene ' || s.id::text),
         s.story_position,
         %(rule_name)s,
         holder.name,
         coalesce(nullif(a.supporting_quote, ''), 'no quote')) as explanation,
  s.id as scene_id,
  a.id as assertion_a,
  null::bigint as assertion_b
from assertions a
join entities subj on subj.id = a.subject_id
join entities holder on holder.id = %(entity_id)s
join scenes s on s.id = a.established_in_scene
left join entities obj on obj.id = a.object_id
where a.world_id = %(world_id)s
  and a.predicate = %(predicate)s
  and a.subject_id <> %(entity_id)s
  and a.polarity
  and a.status not in {_MUTED}{object_clause}{_SEALS_ANTI_JOIN}
"""
        return sql, params


@dataclass(frozen=True)
class ExceptionRule:
    """Suppress one universal check family for one entity [in a position range].

    Never queries — it filters an already-listed findings set (see
    apply_exceptions). The range is inclusive on both ends; either end open.
    """

    kind: ClassVar[str] = "exception"

    check_name: str          # a member of check_families()
    entity_id: int
    pos_from: int | None = None
    pos_to: int | None = None
    label: str | None = None
    rule_id: str | None = None

    def __post_init__(self):
        families = check_families()
        if self.check_name not in families:
            raise ValueError(
                f"unknown check family {self.check_name!r}: exceptions may only "
                f"suppress {', '.join(families)}")
        if self.entity_id is None:
            raise ValueError("an exception needs the one entity it covers")
        if (self.pos_from is not None and self.pos_to is not None
                and self.pos_from > self.pos_to):
            raise ValueError(
                f"empty story-position range: from {self.pos_from} to {self.pos_to}")

    @property
    def name(self) -> str:
        if self.label:
            return self.label
        rng = ""
        if self.pos_from is not None or self.pos_to is not None:
            rng = f" in positions {self.pos_from if self.pos_from is not None else '*'}" \
                  f"..{self.pos_to if self.pos_to is not None else '*'}"
        return f"skip {self.check_name} for entity #{self.entity_id}{rng}"

    def params(self) -> dict:
        return {
            "check_name": self.check_name,
            "entity_id": self.entity_id,
            "pos_from": self.pos_from,
            "pos_to": self.pos_to,
        }

    def suppresses(self, check, subject_id, position) -> bool:
        """Does this exception cover one finding? Exact family + exact entity;
        with a range set, an unknown position stays flagged (the writer asked
        for a surgical window, so we never widen it by guessing)."""
        if check != self.check_name or subject_id != self.entity_id:
            return False
        if self.pos_from is None and self.pos_to is None:
            return True
        if position is None:
            return False
        if self.pos_from is not None and position < self.pos_from:
            return False
        if self.pos_to is not None and position > self.pos_to:
            return False
        return True


# ---------------------------------------------------------------------------
# Construction from stored params (the one validated path in and out of the
# world_rules table — ui/rules_ui.py builds forms into this too).
# ---------------------------------------------------------------------------

def from_params(kind: str, params: dict, label=None, rule_id=None,
                severity=None):
    """Build a typed rule from a params dict; every field validated."""
    params = params or {}
    if kind == "cannot":
        return CannotRule(
            predicate=params.get("predicate"),
            subject_entity_id=_opt_int(params.get("subject_entity_id"), "subject entity"),
            subject_trait=(params.get("subject_trait") or None),
            object_entity_id=_opt_int(params.get("object_entity_id"), "object entity"),
            severity=severity or params.get("severity") or DEFAULT_SEVERITY,
            label=_clean_label(label), rule_id=rule_id)
    if kind == "only":
        return OnlyRule(
            entity_id=_req_int(params.get("entity_id"), "entity"),
            predicate=params.get("predicate"),
            object_entity_id=_opt_int(params.get("object_entity_id"), "object entity"),
            severity=severity or params.get("severity") or DEFAULT_SEVERITY,
            label=_clean_label(label), rule_id=rule_id)
    if kind == "exception":
        return ExceptionRule(
            check_name=params.get("check_name"),
            entity_id=_req_int(params.get("entity_id"), "entity"),
            pos_from=_opt_int(params.get("pos_from"), "from position"),
            pos_to=_opt_int(params.get("pos_to"), "to position"),
            label=_clean_label(label), rule_id=rule_id)
    raise ValueError(f"unknown rule kind {kind!r} (expected one of {RULE_KINDS})")


def rule_from_row(row: dict):
    """Typed rule from a world_rules row (rules_store dict shape)."""
    return from_params(row["kind"], row.get("params") or {},
                       label=row.get("label"), rule_id=row.get("id"))


def save_rule(cur, world_id: int, rule, created_by=None) -> str:
    """Persist a *typed* rule (already validated by construction) and return
    its id. This is the only write path the UI uses — an invalid rule can't
    reach the table because it can't be constructed."""
    return rules_store.create_rule(cur, world_id, rule.kind, rule.params(),
                                   label=rule.label, created_by=created_by)


# ---------------------------------------------------------------------------
# Runner + compose — the check runner's and report's entry points.
# ---------------------------------------------------------------------------

def load_rules(cur, world_id: int, enabled_only: bool = True) -> list:
    """Typed rules for a world, disabled ones excluded by default."""
    rows = rules_store.list_rules(cur, world_id, enabled_only=enabled_only)
    return [rule_from_row(r) for r in rows]


def run_rules(cur, world_id: int) -> list[dict]:
    """Execute every enabled CANNOT/ONLY rule; return canon/check.py-shaped
    findings ({check, severity, explanation, scene_id, assertion_a,
    assertion_b, sealed}). Sealed violations are excluded in SQL, so every
    returned finding is live. No LLM anywhere on this path."""
    findings: list[dict] = []
    for rule in load_rules(cur, world_id, enabled_only=True):
        if isinstance(rule, ExceptionRule):
            continue  # exceptions filter; they never query
        sql, params = rule.compile()
        cur.execute(sql, {"world_id": world_id, **params})
        for row in cur.fetchall():
            findings.append(dict(zip(_FINDING_COLUMNS, row)) | {"sealed": False})
    return findings


def apply_exceptions(cur, world_id: int, findings: list[dict],
                     exceptions: list | None = None) -> list[dict]:
    """Filter a canon/check.py-style findings list through the world's enabled
    EXCEPTION rules. Accepts findings keyed 'check' (canon/check.py) or
    'check_name' (ui/db.py rows). A finding is suppressed only when an
    exception matches its family AND the subject of its cited assertion_a AND
    its story position (the cited scene's, else the assertion's establishing
    scene's) falls inside the rule's window."""
    if exceptions is None:
        exceptions = [r for r in load_rules(cur, world_id, enabled_only=True)
                      if isinstance(r, ExceptionRule)]
    if not exceptions or not findings:
        return list(findings)

    subject_of: dict[int, int] = {}
    assertion_pos: dict[int, int] = {}
    aids = sorted({f.get("assertion_a") for f in findings if f.get("assertion_a")})
    if aids:
        cur.execute(
            "select a.id, a.subject_id, s.story_position"
            " from assertions a"
            " left join scenes s on s.id = a.established_in_scene"
            " where a.world_id = %(world_id)s and a.id = any(%(ids)s)",
            {"world_id": world_id, "ids": aids})
        for aid, subject_id, pos in cur.fetchall():
            subject_of[aid] = subject_id
            assertion_pos[aid] = pos
    scene_pos: dict[int, int] = {}
    sids = sorted({f.get("scene_id") for f in findings if f.get("scene_id")})
    if sids:
        cur.execute("select id, story_position from scenes where id = any(%(ids)s)",
                    {"ids": sids})
        for sid, pos in cur.fetchall():
            scene_pos[sid] = pos

    kept: list[dict] = []
    for f in findings:
        check = f.get("check") or f.get("check_name")
        subject_id = subject_of.get(f.get("assertion_a"))
        position = scene_pos.get(f.get("scene_id"))
        if position is None:
            position = assertion_pos.get(f.get("assertion_a"))
        if any(e.suppresses(check, subject_id, position) for e in exceptions):
            continue
        kept.append(f)
    return kept


def compose_findings(cur, world_id: int, check_findings=()) -> list[dict]:
    """The one-call composition for the check runner and the report: universal
    check findings + this world's rule findings, exception filters applied to
    both. Everything stays in the db/checks.sql finding shape."""
    combined = list(check_findings) + run_rules(cur, world_id)
    return apply_exceptions(cur, world_id, combined)


# ---------------------------------------------------------------------------
# CLI — `canon rules list|run --world W` (registered from canon/cli.py).
# ---------------------------------------------------------------------------

def render_rules_list(rows: list[dict]) -> str:
    if not rows:
        return "no rules defined for this world."
    lines = []
    for r in rows:
        rule = rule_from_row(r)
        state = "on " if r.get("enabled", r.get("disabled_at") is None) else "off"
        lines.append(f"  [{state}] {r['kind']:<9} {rule.name}  ({r['id']})")
    return f"{len(rows)} rule(s):\n" + "\n".join(lines)


def cmd_rules(args: argparse.Namespace) -> int:
    from . import check as check_mod
    from . import ingest as ingest_mod

    db_url = ingest_mod.resolve_db_url(args.db_url)
    if db_url is None:
        print("error: rules requires a database (set CANON_DB_URL / DATABASE_URL / --db-url)",
              file=sys.stderr)
        return 2
    try:
        conn = ingest_mod.connect(db_url)
    except RuntimeError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    try:
        cur = conn.cursor()
        cur.execute("SELECT id FROM worlds WHERE name = %s", (args.world,))
        row = cur.fetchone()
        if not row:
            print(f"error: world '{args.world}' not found — load it first.", file=sys.stderr)
            return 1
        world_id = row[0]
        if args.action == "list":
            print(render_rules_list(rules_store.list_rules(cur, world_id)))
        else:  # run
            findings = apply_exceptions(cur, world_id, run_rules(cur, world_id))
            print(check_mod.render_report(findings))
    finally:
        try:
            conn.close()
        except Exception:
            pass
    return 0


def register_cli(sub) -> None:
    """Attach the `rules` subcommand to the canon CLI's subparsers."""
    ru = sub.add_parser(
        "rules", help="per-world structured rules: list them, or run them as cited checks")
    ru.add_argument("action", choices=["list", "run"],
                    help="list this world's rules, or run them -> findings report")
    ru.add_argument("--world", required=True, help="world name (must be loaded)")
    ru.add_argument("--db-url", default=None,
                    help="Postgres URL (else CANON_DB_URL / DATABASE_URL)")
    ru.set_defaults(func=cmd_rules)

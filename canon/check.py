"""Continuity checks — Phase 0 step 5 (PLAN.md item 5; SPEC R6).

Runs db/checks.sql against a world's loaded graph and emits findings: each carries
a check name, severity, plain-English explanation, citation (scene/assertions),
and a `sealed` flag (writer marked intentional → suppressed from the report and
the eval's gates). Doctrine D7: SQL judges; this runner only executes the queries,
applies seals, and serializes — it never originates a finding.

Output is the eval I/O contract `{"findings": [{check, severity, explanation,
sealed, ...}]}` (eval/run_eval.py) and, optionally, rows persisted to the
`findings` table.

psycopg note: checks.sql uses SQL's `format('%s', ...)`; those literal `%` must be
doubled before psycopg sees them as a parameterized query, and `:world_id` becomes
a `%(world_id)s` named placeholder.
"""

from __future__ import annotations

import re

# columns each check SELECTs, in order (see db/checks.sql header)
_COLUMNS = ("check", "severity", "explanation", "scene_id", "assertion_a", "assertion_b")
_NAME_RE = re.compile(r"(?is)^\s*select\s+'([a-z_]+)'")


def split_checks(sql_text: str) -> list[tuple]:
    """Split checks.sql into (check_name, statement) pairs (comments stripped)."""
    body = "\n".join(ln for ln in sql_text.splitlines() if not ln.lstrip().startswith("--"))
    out: list[tuple] = []
    for chunk in body.split(";"):
        stmt = chunk.strip()
        if not stmt:
            continue
        m = _NAME_RE.match(stmt)
        out.append((m.group(1) if m else None, stmt))
    return out


def to_psycopg(stmt: str) -> str:
    """Escape literal % (from SQL format()) then bind :world_id as a named param."""
    return stmt.replace("%", "%%").replace(":world_id", "%(world_id)s")


def load_seals(cur, world_id: int) -> set:
    cur.execute(
        "SELECT check_name, assertion_a, coalesce_b FROM seals WHERE world_id = %s",
        (world_id,),
    )
    return {(r[0], r[1], r[2]) for r in cur.fetchall()}


def run_checks(conn, world_id: int, checks_sql: str) -> tuple:
    """Execute every check; return (findings, errors). Findings carry `sealed`."""
    cur = conn.cursor()
    findings: list[dict] = []
    errors: list[tuple] = []
    for name, stmt in split_checks(checks_sql):
        try:
            cur.execute(to_psycopg(stmt), {"world_id": world_id})
            rows = cur.fetchall()
        except Exception as e:  # one bad check shouldn't abort the rest
            conn.rollback()
            errors.append((name, str(e).splitlines()[0]))
            continue
        for r in rows:
            findings.append(dict(zip(_COLUMNS, r)) | {"sealed": False})

    seals = load_seals(cur, world_id)
    for f in findings:
        if (f["check"], f["assertion_a"], f["assertion_b"] or 0) in seals:
            f["sealed"] = True
    return findings, errors


def persist_findings(conn, world_id: int, findings: list[dict], clear: bool = True) -> None:
    cur = conn.cursor()
    if clear:
        cur.execute("DELETE FROM findings WHERE world_id = %s", (world_id,))
    for f in findings:
        cur.execute(
            "INSERT INTO findings "
            "(world_id, check_name, severity, explanation, scene_id, assertion_a, assertion_b, sealed) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s, %s)",
            (world_id, f["check"], f["severity"], f["explanation"],
             f["scene_id"], f["assertion_a"], f["assertion_b"], f["sealed"]),
        )
    conn.commit()


def to_findings_json(findings: list[dict]) -> dict:
    """The eval/run_eval.py I/O contract."""
    return {"findings": [
        {"check": f["check"], "severity": f["severity"], "explanation": f["explanation"],
         "sealed": f["sealed"], "scene_id": f["scene_id"],
         "assertion_a": f["assertion_a"], "assertion_b": f["assertion_b"]}
        for f in findings
    ]}


_SEV_ORDER = {"critical": 0, "warning": 1, "note": 2}


def render_report(findings: list[dict]) -> str:
    live = [f for f in findings if not f["sealed"]]
    sealed = [f for f in findings if f["sealed"]]
    by_sev: dict[str, int] = {}
    for f in live:
        by_sev[f["severity"]] = by_sev.get(f["severity"], 0) + 1
    summary = ", ".join(f"{n} {s}" for s, n in sorted(by_sev.items(), key=lambda kv: _SEV_ORDER.get(kv[0], 9))) or "none"
    lines = [f"findings: {len(live)} ({summary})" + (f"; {len(sealed)} sealed" if sealed else ""), ""]
    for f in sorted(live, key=lambda x: _SEV_ORDER.get(x["severity"], 9)):
        lines.append(f"  [{f['severity']}] {f['check']}: {f['explanation']}")
    for f in sealed:
        lines.append(f"  [sealed] {f['check']}: {f['explanation']}")
    return "\n".join(lines)

"""Batch ingestion: parse a world's works, assign global story positions, load.

Story position is a single integer axis *per world* (docs/architecture.md, D6):
default = reading order across the whole ingested corpus. So positions are
assigned here, across works, not inside the per-file parser. The cross-work
ordering is what lets continuity checks compare e.g. "died in E101" against
"appears in E102".

The loader speaks the common psycopg2/psycopg3 cursor subset
(cursor/execute/fetchone/commit) so it works with either driver and is unit
testable against a fake connection without a live database.
"""

from __future__ import annotations

import glob
import os
from pathlib import Path

from .fountain import ParsedWork, parse_fountain

DB_URL_ENV_VARS = ("CANON_DB_URL", "DATABASE_URL")
# `supabase start` default; documented fallback, never assumed silently.
SUPABASE_LOCAL_URL = "postgresql://postgres:postgres@127.0.0.1:54322/postgres"


def expand_inputs(paths: list[str]) -> list[str]:
    """Expand a mix of files and directories into an ordered, de-duped file list."""
    files: list[str] = []
    for p in paths:
        if os.path.isdir(p):
            files.extend(sorted(glob.glob(os.path.join(p, "*.fountain"))))
        else:
            files.append(p)
    seen: set[str] = set()
    out: list[str] = []
    for f in files:
        if f not in seen:
            seen.add(f)
            out.append(f)
    return out


def parse_works(files: list[str]) -> list[ParsedWork]:
    works = [parse_fountain(Path(f).read_text(encoding="utf-8"), source_file=f) for f in files]
    return order_works(works)


def order_works(works: list[ParsedWork]) -> list[ParsedWork]:
    """Order works by episode number when known, else by filename, and stamp a
    stable sort_order so story positions are deterministic on every (re)ingest."""

    def key(w: ParsedWork) -> tuple:
        return (0, w.episode) if w.episode is not None else (1, w.source_file)

    ordered = sorted(works, key=key)
    for i, w in enumerate(ordered, start=1):
        if w.sort_order is None:
            w.sort_order = i
    return ordered


def assign_story_positions(
    works: list[ParsedWork], start: int = 1
) -> list[tuple[ParsedWork, list[tuple]]]:
    """Stamp each scene with a global (per-world) story_position in reading order.

    Returns a plan: [(work, [(scene, position), ...]), ...]. Positions are kept
    out of the Scene object because they belong to the world, not the file.
    """
    pos = start
    plan: list[tuple[ParsedWork, list[tuple]]] = []
    for w in works:
        rows = []
        for sc in w.scenes:
            rows.append((sc, pos))
            pos += 1
        plan.append((w, rows))
    return plan


# ---------------------------------------------------------------------------
# Database loading
# ---------------------------------------------------------------------------

def resolve_db_url(explicit: str | None) -> str | None:
    if explicit:
        return explicit
    for env in DB_URL_ENV_VARS:
        if os.environ.get(env):
            return os.environ[env]
    return None


def connect(db_url: str):
    try:
        import psycopg  # psycopg3
    except ModuleNotFoundError:
        try:
            import psycopg2 as psycopg  # fallback
        except ModuleNotFoundError as e:
            raise RuntimeError(
                "psycopg is not installed. Run `pip install -r requirements.txt`, "
                "or pass --dry-run to parse without a database."
            ) from e
    return psycopg.connect(db_url)


def load_into(conn, world_name: str, works: list[ParsedWork], reset: bool = False) -> dict:
    """Load works/scenes for one world into Postgres. Idempotent only with reset.

    Populates worlds, works, scenes (per db/schema.sql). scene_presence and
    entities are deliberately untouched — they are produced by the *extraction*
    step, not ingestion.
    """
    plan = assign_story_positions(works, start=1)
    cur = conn.cursor()

    if reset:
        # FK-safe teardown for a clean reload. A plain `DELETE FROM worlds` cascade
        # is NOT reliable here: scene_presence.entity_id / character_locations are
        # NO ACTION, and Postgres checks them mid-cascade (before the scenes branch
        # clears those rows), so the delete can fail once a graph is loaded. Delete
        # the dependent rows first, in dependency order, then the world.
        _w = "(SELECT id FROM worlds WHERE name = %s)"
        cur.execute(f"DELETE FROM findings WHERE world_id IN {_w}", (world_name,))
        cur.execute(f"DELETE FROM assertions WHERE world_id IN {_w}", (world_name,))
        cur.execute(
            "DELETE FROM scene_presence WHERE scene_id IN "
            "(SELECT s.id FROM scenes s JOIN works w ON w.id = s.work_id "
            " JOIN worlds wd ON wd.id = w.world_id WHERE wd.name = %s)",
            (world_name,),
        )
        cur.execute(f"DELETE FROM entities WHERE world_id IN {_w}", (world_name,))
        cur.execute("DELETE FROM worlds WHERE name = %s", (world_name,))  # cascades works/scenes

    cur.execute("SELECT id FROM worlds WHERE name = %s", (world_name,))
    row = cur.fetchone()
    if row:
        world_id = row[0]
    else:
        cur.execute("INSERT INTO worlds (name) VALUES (%s) RETURNING id", (world_name,))
        world_id = cur.fetchone()[0]

    cur.execute("SELECT count(*) FROM works WHERE world_id = %s", (world_id,))
    if cur.fetchone()[0] and not reset:
        raise RuntimeError(
            f"world '{world_name}' already has works; re-run with --reset-world to replace it."
        )

    n_scenes = 0
    for w, rows in plan:
        cur.execute(
            "INSERT INTO works (world_id, title, source_file, sort_order) "
            "VALUES (%s, %s, %s, %s) RETURNING id",
            (world_id, w.title, w.source_file, w.sort_order),
        )
        work_id = cur.fetchone()[0]
        for sc, pos in rows:
            cur.execute(
                "INSERT INTO scenes (work_id, slug, story_position, is_flashback, raw_text) "
                "VALUES (%s, %s, %s, %s, %s)",
                (work_id, sc.slug, pos, sc.is_flashback, sc.raw_text),
            )
            n_scenes += 1

    conn.commit()
    return {"world_id": world_id, "works": len(plan), "scenes": n_scenes}


def render_dry_run(world_name: str, works: list[ParsedWork]) -> str:
    """Human-readable preview of exactly what load_into would insert."""
    plan = assign_story_positions(works, start=1)
    lines = [f"world: {world_name}", ""]
    total = 0
    for w, rows in plan:
        lines.append(
            f"  {w.title}  [{w.source_file}]  "
            f"(sort_order={w.sort_order}, {len(rows)} scene(s))"
        )
        for sc, pos in rows:
            fb = "  (FLASHBACK)" if sc.is_flashback else ""
            lines.append(f"    pos {pos:>3}  sc{sc.scene_index}  {sc.slug}{fb}")
            total += 1
        if w.stripped_planted:
            lines.append(f"    (stripped {w.stripped_planted} planted annotation(s))")
        lines.append("")
    lines.append(f"total: {len(plan)} work(s), {total} scene(s)")
    return "\n".join(lines)

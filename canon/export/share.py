"""Share-link plumbing — pure functions over a DB cursor.

Backed by the share_links table (supabase/migrations/..._identity_and_access.sql):
token (unique), world_id, kind ('report'|'bible'), created_by, revoked_at
(null = live). No web routes here — the surface workstream wires HTTP later.

Rights hygiene: nothing is shareable by default (docs/readers-report.md §6);
a link exists only when the writer explicitly creates one, and revocation is
permanent (a revoked token never resolves again — mint a new link instead).
"""

from __future__ import annotations

import secrets
from typing import Any

KINDS = ("report", "bible")
_TOKEN_BYTES = 32  # secrets.token_urlsafe(32) -> 43-char URL-safe token


def create_share_link(cur, world_id: int, kind: str, created_by: str | None = None) -> dict[str, Any]:
    """Insert a live share link; returns {token, world_id, kind, created_by}."""

    if kind not in KINDS:
        raise ValueError(f"share link kind must be one of {KINDS}, got {kind!r}")
    token = secrets.token_urlsafe(_TOKEN_BYTES)
    cur.execute(
        "INSERT INTO share_links (token, world_id, kind, created_by) "
        "VALUES (%s, %s, %s, %s)",
        (token, world_id, kind, created_by),
    )
    return {"token": token, "world_id": world_id, "kind": kind, "created_by": created_by}


def resolve_share_link(cur, token: str) -> dict[str, Any] | None:
    """{world_id, kind} for a live (not revoked) token; None otherwise."""

    if not token:
        return None
    cur.execute(
        "SELECT world_id, kind FROM share_links "
        "WHERE token = %s AND revoked_at IS NULL",
        (token,),
    )
    row = cur.fetchone()
    if not row:
        return None
    return {"world_id": row[0], "kind": row[1]}


def revoke_share_link(cur, token: str) -> bool:
    """Revoke a live token. True if a live link was revoked; False when the
    token is unknown or already revoked (idempotent, never raises for those)."""

    cur.execute(
        "UPDATE share_links SET revoked_at = now() "
        "WHERE token = %s AND revoked_at IS NULL",
        (token,),
    )
    return bool(getattr(cur, "rowcount", 0))

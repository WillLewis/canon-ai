"""Supabase JWT auth for the workbench (P3-IDENTITY).

Verification only — Supabase mints the tokens (email and Google sign-ins both
arrive as the same Supabase access token, HS256-signed with the project's JWT
secret); this module never creates identities. Two FastAPI dependencies:

    get_current_user     Authorization: Bearer <jwt> or the sb-access-token
                         cookie -> User. 401 when absent/invalid/expired.
    require_role(min)    get_current_user + a world_members role check against
                         the world the request addresses (same ?world= picking
                         rule as the routes). Hierarchy: owner > editor > viewer.

Dev bypass: AUTH_DISABLED=1 short-circuits both to a fake local owner so the
single-user workbench workflow keeps working. Default OFF — with the env unset
and no token, every guarded route fails closed with 401.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, replace

from fastapi import Depends, HTTPException, Request

try:
    import jwt  # PyJWT — see ui/requirements.txt
except ModuleNotFoundError as e:  # pragma: no cover - listed in ui/requirements.txt
    raise RuntimeError(
        "PyJWT is required for the UI auth layer. Run `pip install -r ui/requirements.txt`."
    ) from e

from . import db

JWT_SECRET_ENV = "SUPABASE_JWT_SECRET"
JWT_ALGORITHM = "HS256"                 # Supabase access tokens are HS256
JWT_AUDIENCE = "authenticated"          # aud claim Supabase sets on user tokens
COOKIE_NAME = "sb-access-token"

ROLE_RANK = {"viewer": 1, "editor": 2, "owner": 3}


@dataclass
class User:
    id: str                    # auth.users.id (the JWT `sub`)
    email: str | None = None
    role: str | None = None    # filled in by require_role for the active world


# Stable fake identity for AUTH_DISABLED=1 local development.
DEV_USER = User(id="00000000-0000-0000-0000-000000000001",
                email="dev@localhost", role="owner")


def auth_disabled() -> bool:
    return os.environ.get("AUTH_DISABLED", "") == "1"


def has_role(role: str | None, min_role: str) -> bool:
    """True when `role` meets `min_role` in the owner > editor > viewer hierarchy."""
    return ROLE_RANK.get(role or "", 0) >= ROLE_RANK[min_role]


def _token_from_request(request: Request) -> str | None:
    header = request.headers.get("authorization") or ""
    if header.lower().startswith("bearer "):
        return header[7:].strip() or None
    return request.cookies.get(COOKIE_NAME)


def decode_token(token: str) -> dict:
    """Verify a Supabase JWT and return its claims. Raises HTTPException, never mints."""
    secret = os.environ.get(JWT_SECRET_ENV)
    if not secret:
        # Fail closed: a token we cannot verify is a token we reject.
        raise HTTPException(status_code=503, detail=f"auth is not configured ({JWT_SECRET_ENV} unset)")
    try:
        return jwt.decode(token, secret, algorithms=[JWT_ALGORITHM], audience=JWT_AUDIENCE)
    except jwt.ExpiredSignatureError:
        raise HTTPException(status_code=401, detail="token expired",
                            headers={"WWW-Authenticate": "Bearer"})
    except jwt.InvalidTokenError:
        raise HTTPException(status_code=401, detail="invalid token",
                            headers={"WWW-Authenticate": "Bearer"})


def get_current_user(request: Request) -> User:
    """FastAPI dependency: the verified Supabase user for this request."""
    if auth_disabled():
        return replace(DEV_USER)
    token = _token_from_request(request)
    if not token:
        raise HTTPException(status_code=401, detail="not authenticated",
                            headers={"WWW-Authenticate": "Bearer"})
    claims = decode_token(token)
    sub = claims.get("sub")
    if not sub:
        raise HTTPException(status_code=401, detail="token has no subject",
                            headers={"WWW-Authenticate": "Bearer"})
    return User(id=str(sub), email=claims.get("email"))


def _resolve_world_id(request: Request) -> int | None:
    """The world this request addresses — same picking rule as ui.app._pick_world
    (?world= by name, else the loaded dev world, else the first), duplicated here
    because dependencies resolve before the route body runs."""
    worlds = db.list_worlds()
    by_name = {w["name"]: w for w in worlds}
    name = request.query_params.get("world")
    if name and name in by_name:
        return by_name[name]["id"]
    if "greyharbor_s1" in by_name:
        return by_name["greyharbor_s1"]["id"]
    return worlds[0]["id"] if worlds else None


def require_role(min_role: str):
    """Dependency factory: verified user who holds >= min_role on the active world."""
    if min_role not in ROLE_RANK:
        raise ValueError(f"unknown role {min_role!r} (expected one of {sorted(ROLE_RANK)})")

    def dependency(request: Request, user: User = Depends(get_current_user)) -> User:
        if auth_disabled():
            return user                      # DEV_USER, already role='owner'
        world_id = _resolve_world_id(request)
        if world_id is None:
            raise HTTPException(status_code=404, detail="no world to authorize against")
        role = db.member_role(world_id, user.id)
        if not has_role(role, min_role):
            raise HTTPException(status_code=403,
                                detail=f"requires the {min_role} role on this world")
        user.role = role
        return user

    return dependency

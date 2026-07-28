"""Auth layer tests (P3-IDENTITY) — all offline.

Supabase JWT verification (accept / reject / expiry), the owner > editor >
viewer hierarchy, the wired seal/unseal routes (viewer cannot seal; editor can,
with ruled_by attribution), and the AUTH_DISABLED dev bypass. The database is
faked by patching ui.db's module-level functions — no live Postgres, matching
the repo's fake-connection test pattern.

    python -m pytest -q tests/test_auth.py
    python tests/test_auth.py
"""

import contextlib
import os
import pathlib
import sys
import time

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

import jwt  # PyJWT — ui/requirements.txt  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from ui import auth, db  # noqa: E402
from ui.app import app  # noqa: E402

SECRET = "test-secret-0123456789abcdef0123456789abcdef"  # >= 32 bytes (RFC 7518)
WORLD = {"id": 1, "name": "greyharbor_s1"}
EDITOR = "11111111-1111-1111-1111-111111111111"
VIEWER = "22222222-2222-2222-2222-222222222222"
OWNER = "33333333-3333-3333-3333-333333333333"
STRANGER = "44444444-4444-4444-4444-444444444444"
ROLES = {EDITOR: "editor", VIEWER: "viewer", OWNER: "owner"}


def make_token(sub=EDITOR, secret=SECRET, aud=auth.JWT_AUDIENCE, expires_in=3600,
               **extra):
    now = int(time.time())
    claims = {"sub": sub, "aud": aud, "email": "writer@example.com",
              "iat": now, "exp": now + expires_in, **extra}
    return jwt.encode(claims, secret, algorithm="HS256")


# --- harness: env + fake db wiring, restored on exit -------------------------

@contextlib.contextmanager
def wired(roles=ROLES, auth_disabled=False, secret=SECRET):
    """TestClient over the real app with ui.db faked and auth env controlled.

    Yields (client, seal_calls) where seal_calls records every
    db.seal_finding(world_id, finding_id, reason, ruled_by) call.
    """
    seal_calls = []
    saved_env = {k: os.environ.get(k) for k in ("SUPABASE_JWT_SECRET", "AUTH_DISABLED")}
    saved_fns = (db.list_worlds, db.member_role, db.seal_finding, db.unseal_finding)
    try:
        if secret is None:
            os.environ.pop("SUPABASE_JWT_SECRET", None)
        else:
            os.environ["SUPABASE_JWT_SECRET"] = secret
        if auth_disabled:
            os.environ["AUTH_DISABLED"] = "1"
        else:
            os.environ.pop("AUTH_DISABLED", None)

        db.list_worlds = lambda: [dict(WORLD)]
        db.member_role = lambda world_id, user_id: (
            roles.get(user_id) if world_id == WORLD["id"] else None)
        db.seal_finding = lambda world_id, finding_id, reason, ruled_by=None: (
            seal_calls.append({"world_id": world_id, "finding_id": finding_id,
                               "reason": reason, "ruled_by": ruled_by}) or True)
        db.unseal_finding = lambda world_id, finding_id: True

        yield TestClient(app, follow_redirects=False), seal_calls
    finally:
        for k, v in saved_env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        db.list_worlds, db.member_role, db.seal_finding, db.unseal_finding = saved_fns


def _seal(client, token=None, cookie=None, finding=5):
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    if cookie:
        client.cookies.set(auth.COOKIE_NAME, cookie)
    return client.post(f"/findings/{finding}/seal", data={"reason": "intentional"},
                       headers=headers)


# --- JWT verification --------------------------------------------------------

def test_valid_token_is_accepted_and_decoded():
    with wired():
        claims = auth.decode_token(make_token())
        assert claims["sub"] == EDITOR and claims["aud"] == auth.JWT_AUDIENCE


def test_no_token_is_401_fail_closed():
    with wired() as (client, calls):
        r = _seal(client)
        assert r.status_code == 401
        assert calls == []


def test_garbage_token_is_401():
    with wired() as (client, calls):
        r = _seal(client, token="not-a-jwt")
        assert r.status_code == 401 and calls == []


def test_wrong_signature_is_401():
    with wired() as (client, calls):
        r = _seal(client, token=make_token(secret="some-other-secret"))
        assert r.status_code == 401 and calls == []


def test_expired_token_is_401():
    with wired() as (client, calls):
        r = _seal(client, token=make_token(expires_in=-60))
        assert r.status_code == 401 and calls == []


def test_wrong_audience_is_401():
    with wired() as (client, calls):
        r = _seal(client, token=make_token(aud="service_role"))
        assert r.status_code == 401 and calls == []


def test_unconfigured_secret_fails_closed_not_open():
    # A token arrives but SUPABASE_JWT_SECRET is unset: reject, never accept.
    with wired(secret=None) as (client, calls):
        r = _seal(client, token=make_token())
        assert r.status_code == 503 and calls == []


def test_cookie_token_works_like_bearer():
    with wired() as (client, calls):
        r = _seal(client, cookie=make_token(sub=EDITOR))
        assert r.status_code == 303
        assert calls and calls[0]["ruled_by"] == EDITOR


# --- role hierarchy ----------------------------------------------------------

def test_role_hierarchy_owner_gt_editor_gt_viewer():
    assert auth.has_role("owner", "viewer")
    assert auth.has_role("owner", "editor")
    assert auth.has_role("owner", "owner")
    assert auth.has_role("editor", "viewer")
    assert auth.has_role("editor", "editor")
    assert not auth.has_role("editor", "owner")
    assert auth.has_role("viewer", "viewer")
    assert not auth.has_role("viewer", "editor")
    assert not auth.has_role(None, "viewer")          # non-member holds nothing
    assert not auth.has_role("stranger", "viewer")    # unknown role holds nothing


def test_require_role_rejects_unknown_min_role():
    try:
        auth.require_role("superadmin")
    except ValueError:
        pass
    else:
        raise AssertionError("expected ValueError for an unknown role name")


# --- seal/unseal wiring ------------------------------------------------------

def test_viewer_cannot_seal():
    with wired() as (client, calls):
        r = _seal(client, token=make_token(sub=VIEWER))
        assert r.status_code == 403 and calls == []


def test_non_member_cannot_seal():
    with wired() as (client, calls):
        r = _seal(client, token=make_token(sub=STRANGER))
        assert r.status_code == 403 and calls == []


def test_editor_can_seal_with_attribution():
    with wired() as (client, calls):
        r = _seal(client, token=make_token(sub=EDITOR))
        assert r.status_code == 303
        assert r.headers["location"] == f"/findings/5?world={WORLD['name']}"
        assert calls == [{"world_id": 1, "finding_id": 5,
                          "reason": "intentional", "ruled_by": EDITOR}]


def test_owner_outranks_editor_requirement():
    with wired() as (client, calls):
        r = _seal(client, token=make_token(sub=OWNER))
        assert r.status_code == 303
        assert calls and calls[0]["ruled_by"] == OWNER


def test_unseal_requires_editor_too():
    with wired() as (client, _):
        no_auth = client.post("/findings/5/unseal")
        assert no_auth.status_code == 401
        viewer = client.post("/findings/5/unseal",
                             headers={"Authorization": f"Bearer {make_token(sub=VIEWER)}"})
        assert viewer.status_code == 403
        editor = client.post("/findings/5/unseal",
                             headers={"Authorization": f"Bearer {make_token(sub=EDITOR)}"})
        assert editor.status_code == 303


# --- AUTH_DISABLED dev bypass ------------------------------------------------

def test_auth_disabled_bypass_seals_as_dev_user():
    with wired(auth_disabled=True, secret=None) as (client, calls):
        r = _seal(client)                      # no token at all
        assert r.status_code == 303
        assert calls and calls[0]["ruled_by"] == auth.DEV_USER.id


def test_auth_disabled_defaults_off():
    # Belt and braces for the acceptance bar: with the env unset the guarded
    # route fails closed (same as test_no_token_401, asserted via the env).
    with wired() as (client, _):
        assert os.environ.get("AUTH_DISABLED") is None
        assert _seal(client).status_code == 401


if __name__ == "__main__":
    import traceback

    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    failed = 0
    for fn in fns:
        try:
            fn()
            print(f"PASS {fn.__name__}")
        except Exception:
            failed += 1
            print(f"FAIL {fn.__name__}")
            traceback.print_exc()
    raise SystemExit(1 if failed else 0)

"""Shared pytest gate for integration tests that require a migrated Canon DB."""

from __future__ import annotations

import os

import pytest


LOCAL_POSTGRES = "postgresql://postgres:postgres@127.0.0.1:5432/postgres"
LOCAL_SUPABASE = "postgresql://postgres:postgres@127.0.0.1:54322/postgres"


def _candidate_urls(*, include_database_url: bool,
                    include_supabase_default: bool) -> list[str]:
    urls = []
    for value in (
        os.environ.get("CANON_DB_URL"),
        os.environ.get("DATABASE_URL") if include_database_url else None,
        LOCAL_SUPABASE if include_supabase_default else None,
        LOCAL_POSTGRES,
    ):
        if value and value not in urls:
            urls.append(value)
    return urls


def require_migrated_db(*, include_database_url: bool = False,
                        include_supabase_default: bool = False):
    """Return a psycopg connection only when the Canon schema is present.

    Connectivity alone is not enough: a plain or unmigrated Postgres instance
    on the default port must skip these integration tests, not fail later on
    "relation worlds does not exist".
    """
    try:
        import psycopg
    except Exception as exc:
        pytest.skip(f"psycopg unavailable: {exc}")

    attempted = 0
    for url in _candidate_urls(
        include_database_url=include_database_url,
        include_supabase_default=include_supabase_default,
    ):
        attempted += 1
        try:
            conn = psycopg.connect(url, connect_timeout=2)
        except Exception:
            continue

        try:
            with conn.cursor() as cur:
                cur.execute("SELECT to_regclass('public.worlds')")
                row = cur.fetchone()
            if row and row[0] is not None:
                return conn
        except Exception:
            pass

        conn.close()

    pytest.skip(
        "no migrated Canon database reachable "
        f"(checked {attempted} URL(s); worlds table missing or connection failed)"
    )

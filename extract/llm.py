"""Anthropic client helpers for structured-output calls."""

from __future__ import annotations

import os
from typing import Any

DEFAULT_MODEL = "claude-opus-4-8"
DEFAULT_EFFORT = "high"
MAX_EXTRACTION_TOKENS = 16000
MAX_RESOLUTION_TOKENS = 4000


def has_credentials() -> bool:
    return bool(os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_AUTH_TOKEN"))


def _metered(create):
    """COGS metering (ops.meter -> usage_events, docs/ops.md), opt-in via
    CANON_METERING_DB_URL. Fail-silent: without the env, or on any setup failure,
    the original callable returns untouched — no-DB runs behave exactly as before.
    user_id stays NULL for pipeline runs; CANON_METERING_WORLD_ID attributes the
    world being processed when the caller exports it."""
    dsn = os.environ.get("CANON_METERING_DB_URL")
    if not dsn:
        return create
    try:
        import psycopg
        from ops import meter
        cursor = psycopg.connect(dsn, autocommit=True).cursor()
        wid = os.environ.get("CANON_METERING_WORLD_ID")
        return meter("extraction", cursor=cursor,
                     user_id=os.environ.get("CANON_METERING_USER_ID") or None,
                     world_id=int(wid) if wid else None)(create)
    except Exception:
        return create  # metering must never block extraction


def make_client():
    try:
        import anthropic
    except ModuleNotFoundError as exc:
        raise RuntimeError(
            "anthropic is not installed. Install requirements or run with --dry-run/--no-llm."
        ) from exc
    # No per-request training flag exists in the SDK. The configured key must
    # belong to a no-training / zero-data-retention workspace.
    client = anthropic.Anthropic()
    client.messages.create = _metered(client.messages.create)
    return client


def first_text(response: Any) -> str | None:
    for block in getattr(response, "content", None) or []:
        if getattr(block, "type", None) == "text":
            return block.text
    return None


def structured_request(
    *,
    model: str,
    system: str,
    user: str,
    schema: dict,
    max_tokens: int,
    effort: str = DEFAULT_EFFORT,
    thinking: bool = True,
) -> dict:
    """Build a native Claude Messages API request.

    Intentionally omits sampling params such as `temperature`; current Opus
    models reject non-default sampling parameters, and the extraction contract is
    enforced by schema plus conservative gates.
    """

    output_config: dict[str, Any] = {"format": {"type": "json_schema", "schema": schema}}
    if effort:
        output_config["effort"] = effort
    request: dict[str, Any] = {
        "model": model,
        "max_tokens": max_tokens,
        "system": system,
        "messages": [{"role": "user", "content": user}],
        "output_config": output_config,
    }
    if thinking:
        request["thinking"] = {"type": "adaptive"}
    return request

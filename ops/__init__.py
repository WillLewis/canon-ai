"""Ops floor (P3-OPS): COGS metering, per-account rate limits, alerting.

Standalone package other streams wire in — see docs/ops.md for the wiring
guide. Imports nothing from canon/, extract/, ingest/, or ask/; the FastAPI
integration lives in ops.middleware (imported separately so this package
stays usable from the pipeline CLIs without fastapi installed).

    from ops import record_usage, meter, per_world_cogs, per_user_cogs
    from ops import check_limit, record_action, get_policy, Decision
    from ops import alerts
    from ops.middleware import rate_limited          # FastAPI routes only
"""

from . import alerts  # noqa: F401
from .metering import PRICING, meter, per_user_cogs, per_world_cogs, record_usage  # noqa: F401
from .ratelimit import (  # noqa: F401
    Decision,
    Limit,
    Policy,
    check_limit,
    check_script_pages,
    get_policy,
    record_action,
)

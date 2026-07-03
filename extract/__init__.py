"""P0-EXTRACT workstream package.

This package is intentionally JSON-in / JSON-out. It does not parse scripts and
does not write to Postgres; ingestion and storage remain separate workstreams.
"""

from .schema import AUTO_ACCEPT_CONFIDENCE, PREDICATES

__all__ = ["AUTO_ACCEPT_CONFIDENCE", "PREDICATES"]

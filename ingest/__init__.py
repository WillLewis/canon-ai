"""Scene-record ingestion for Canon AI Phase 0.

This package produces the P0-INGEST handoff contract only: scene records with
stable scene ids, slugs, global story positions, flashback flags, and raw scene
text. It deliberately does not write to Postgres or extract assertions.
"""

from .pipeline import SceneRecord, ingest_files, records_to_json

__all__ = ["SceneRecord", "ingest_files", "records_to_json"]

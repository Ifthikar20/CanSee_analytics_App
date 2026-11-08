"""Source attribution: where an AI answer most likely got its information.

See docs/attribution_contract.md and ``service`` for the pipeline.
"""
from apps.citations.services.attribution.service import (
    compute,
    get_payload,
    snapshot_payload,
    start,
)

__all__ = ["compute", "get_payload", "snapshot_payload", "start"]

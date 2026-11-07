"""Public service API for the brand_vault app."""
from apps.brand_vault.services.embeddings import embed_text
from apps.brand_vault.services.fact_extractor import (
    extract_facts_for_chunk,
    extract_facts_for_website,
)
from apps.brand_vault.services.fact_versioning import (
    approve_fact,
    record_creation,
    reject_fact,
    supersede_fact,
)

__all__ = [
    "embed_text",
    "extract_facts_for_chunk",
    "extract_facts_for_website",
    "approve_fact",
    "record_creation",
    "reject_fact",
    "supersede_fact",
]

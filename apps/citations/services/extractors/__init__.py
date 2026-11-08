"""Citation extractor registry."""
from apps.citations.services.extractors.base import BaseExtractor, CitationCandidate
from apps.citations.services.extractors.gemini import GeminiGroundingExtractor
from apps.citations.services.extractors.perplexity import PerplexityNativeExtractor
from apps.citations.services.extractors.regex import RegexExtractor

__all__ = [
    "BaseExtractor",
    "CitationCandidate",
    "GeminiGroundingExtractor",
    "PerplexityNativeExtractor",
    "RegexExtractor",
]

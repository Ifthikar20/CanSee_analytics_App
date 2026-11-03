"""Text matching rules shared by the insights services (contract 1d).

``fold`` and ``contains`` are the verbatim-quote rule: a quote matches when
it appears in the source after collapsing whitespace and ignoring case.
``idea_key`` is the agent's ``research.query_key`` rule, copied so insights
does not import agent tools: prompt ideas, fan-out queries and tracked
prompts compare with it, so the agent and these pages agree on "the same
search".
"""

from __future__ import annotations

import hashlib
import re

# Copied verbatim from apps.agent.tools.research._QUERY_NOISE: punctuation
# Google ignores in a query.
_QUERY_NOISE = re.compile(r"[?!,;:\"'“”‘’«»()\[\]{}]|\.(?=\s|$)")
_CLAIM_EDGE = " .,;:!?\"'"


def fold(text) -> str:
    """Case folded, whitespace collapsed to single spaces."""
    return " ".join(str(text or "").casefold().split())


def contains(haystack, needle) -> bool:
    """Whether ``needle`` (not empty) appears in ``haystack``, case and
    spacing ignored."""
    wanted = fold(needle)
    return bool(wanted) and wanted in fold(haystack)


def idea_key(text) -> str:
    """``text`` as a search: case, spacing and ignorable punctuation dropped."""
    return " ".join(_QUERY_NOISE.sub(" ", str(text or "").lower()).split())


def claim_key(text) -> str:
    """The key that groups the same claim across answers."""
    folded = fold(text).strip(_CLAIM_EDGE)
    return hashlib.sha1(folded.encode()).hexdigest()[:16]


def label_key(label) -> str:
    """The key that groups the same theme label across answers."""
    return fold(label)[:60]


def collapse(text) -> str:
    """Whitespace collapsed to single spaces, case kept."""
    return " ".join(str(text or "").split())

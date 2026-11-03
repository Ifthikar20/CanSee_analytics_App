"""Post lengths as the networks count them (design/p2_contract.md 12f,
verified 2026-11-03).

X (docs.x.com "Counting characters", twitter-text config v3): text is
NFC-normalized; code points 0-4351, 8192-8205, 8208-8223 and 8242-8247
count 1 and every other code point 2; each http(s) URL counts 23 whatever
its length; the limit is 280. LinkedIn: a post holds up to 3,000
characters. Posts carry no emojis (house rule), so emoji sequences need
no counting of their own.
"""
from __future__ import annotations

import re
import unicodedata

X_LIMIT = 280
LINKEDIN_LIMIT = 3000
X_URL_LENGTH = 23
# Inclusive code point ranges that count as one character on X.
X_LIGHT_RANGES = ((0, 4351), (8192, 8205), (8208, 8223), (8242, 8247))
_URL = re.compile(r"https?://[^\s<>\"]+", re.IGNORECASE)
# Punctuation that ends a sentence after a link is not part of the link.
_URL_TAIL = ".,;:!?)]}'\""


def _weight(char: str) -> int:
    point = ord(char)
    return 1 if any(low <= point <= high for low, high in X_LIGHT_RANGES) else 2


def links(text: str) -> list[str]:
    """The http(s) URLs of ``text``, trailing punctuation left out."""
    return [match.group(0).rstrip(_URL_TAIL) for match in _URL.finditer(text or "")]


def x_length(text: str) -> int:
    """The length X counts for ``text``."""
    text = unicodedata.normalize("NFC", text or "")
    total, pos = 0, 0
    for match in _URL.finditer(text):
        url = match.group(0).rstrip(_URL_TAIL)
        total += sum(_weight(c) for c in text[pos:match.start()]) + X_URL_LENGTH
        pos = match.start() + len(url)
    return total + sum(_weight(c) for c in text[pos:])


def linkedin_length(text: str) -> int:
    """Characters of a LinkedIn post (code points)."""
    return len(unicodedata.normalize("NFC", text or ""))

"""Search score, AI score, and the gap between them.

Pure functions, no Django. These tables are the product's definition of
"the gap", so they live here once and the tests pin them.

Google position -> search score::

    #1-3: 100   #4-10: 80   #11-20: 55   #21-50: 30   #51-100: 15   not found: 0

AI engine -> AI score, over its most recent answers::

    share of answers that mention the brand
      x mean rank weight of those mentions
        (rank 1-3: 100, 4-5: 80, 6-10: 55, 11+: 30, mentioned but unranked: 55)

gap = AI score - search score. Example: Google #94 scores 15, Claude
listing the brand #3 scores 100, so the gap is +85: AI ahead.
"""

from __future__ import annotations

from dataclasses import dataclass

AI_AHEAD = "ai_ahead"
GOOGLE_AHEAD = "google_ahead"
STRONG_BOTH = "strong_both"
INVISIBLE_BOTH = "invisible_both"
EVEN = "even"
NO_DATA = "no_data"

GAP_THRESHOLD = 30
STRONG = 55
INVISIBLE = 15
# How many of an engine's latest answers feed its score. Answers vary run
# to run; a small window smooths that without hiding a real change.
ANSWER_WINDOW = 5

_SEARCH_BANDS = ((3, 100), (10, 80), (20, 55), (50, 30), (100, 15))
_RANK_WEIGHTS = ((3, 100), (5, 80), (10, 55))
_LONG_TAIL_WEIGHT = 30
_UNRANKED_WEIGHT = 55


@dataclass(frozen=True)
class Answer:
    mentioned: bool
    rank: int | None = None


def search_score(position: int | None) -> int:
    if not position or position < 1:
        return 0
    for limit, score in _SEARCH_BANDS:
        if position <= limit:
            return score
    return 0


def google_page(position: int | None) -> int | None:
    if not position or position < 1:
        return None
    return (position - 1) // 10 + 1


def rank_weight(rank: int | None) -> int:
    if rank is None or rank < 1:
        return _UNRANKED_WEIGHT
    for limit, weight in _RANK_WEIGHTS:
        if rank <= limit:
            return weight
    return _LONG_TAIL_WEIGHT


def ai_score(answers) -> int | None:
    """None when the engine has not answered yet (nothing to compare)."""
    answers = list(answers)
    if not answers:
        return None
    mentions = [a for a in answers if a.mentioned]
    if not mentions:
        return 0
    share = len(mentions) / len(answers)
    weight = sum(rank_weight(a.rank) for a in mentions) / len(mentions)
    return round(share * weight)


def classify(ai: int | None, search: int | None) -> str:
    """Gap class for one engine, or for the mean over engines.

    ``search`` is None until Google has been checked, which is no data
    rather than "not ranking": an unchecked prompt must not read as AI
    ahead.
    """
    if ai is None or search is None:
        return NO_DATA
    gap = ai - search
    if gap >= GAP_THRESHOLD:
        return AI_AHEAD
    if gap <= -GAP_THRESHOLD:
        return GOOGLE_AHEAD
    if ai >= STRONG and search >= STRONG:
        return STRONG_BOTH
    if ai <= INVISIBLE and search <= INVISIBLE:
        return INVISIBLE_BOTH
    return EVEN


def overall(ai_scores, search: int | None) -> dict:
    """Mean AI score over engines that answered, against Google."""
    present = [s for s in ai_scores if s is not None]
    ai = round(sum(present) / len(present)) if present else None
    value = ai - search if ai is not None and search is not None else None
    return {
        "ai_score": ai,
        "search_score": search,
        "value": value,
        "class": classify(ai, search),
    }

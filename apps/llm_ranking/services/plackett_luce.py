"""
Plackett-Luce model for ranking brands across LLM responses.

Each LLM response gives a ranking like [brand_a, brand_b, brand_c, ...].
Plackett-Luce fits a strength s_i per brand such that

    P(item i ranked first among S) = w_i / sum_{j in S} w_j

and a full ranking factors as a product of such top-1 choices.

We fit via the MM (minorisation-maximisation) iteration from
Hunter (2004) "MM algorithms for generalized Bradley-Terry models",
which is a closed-form update with guaranteed monotone log-likelihood
improvement and no learning rate to tune.

Implementation note: pure Python (no NumPy dependency) since the brand
counts per audit are small (typically < 50) and iteration counts are
modest (< 200). Avoids pulling NumPy/SciPy into the runtime.
"""
from __future__ import annotations


def fit_plackett_luce(
    rankings: list[list[str]],
    max_iter: int = 200,
    tol: float = 1e-6,
) -> dict[str, float]:
    """
    Fit Plackett-Luce strengths from a list of rankings.

    Each ranking is an ordered list of brand names (best first). A ranking
    with fewer than two brands compares nothing, so it is ignored, and a
    brand that appears in no comparison is excluded from the result
    (rather than keeping its untouched starting strength of 1.0).

    Returns ``{brand: strength}`` normalised so the maximum strength is 1.0.
    Returns an empty dict when no ranking compares two brands.
    """
    rankings = [r for r in rankings if r and len(r) >= 2]
    brands = sorted({b for r in rankings for b in r})
    if not brands:
        return {}
    # Defensive cap — a hostile / malformed extraction could produce an
    # arbitrarily long brand list. The MM iteration is O(n*k) per pass
    # so we cut off any rankings beyond a reasonable horizon. 200 is far
    # above any real audit and keeps the JSONField bounded.
    if len(brands) > 200:
        brands = brands[:200]
        keep = set(brands)
        rankings = [[b for b in r if b in keep] for r in rankings]

    idx = {b: i for i, b in enumerate(brands)}
    n = len(brands)
    w = [1.0] * n

    for _ in range(max_iter):
        num = [0.0] * n
        denom = [0.0] * n

        for ranking in rankings:
            if not ranking or len(ranking) < 2:
                continue
            ids = [idx[b] for b in ranking if b in idx]
            if len(ids) < 2:
                continue

            tail_sum = sum(w[i] for i in ids)
            for k, i in enumerate(ids[:-1]):
                num[i] += 1.0
                if tail_sum > 0:
                    inv = 1.0 / tail_sum
                    for j in ids[k:]:
                        denom[j] += inv
                tail_sum -= w[i]

        new_w = [
            (num[i] / denom[i]) if denom[i] > 0 else w[i]
            for i in range(n)
        ]
        max_w = max(new_w) or 1.0
        new_w = [v / max_w for v in new_w]

        delta = sum((new_w[i] - w[i]) ** 2 for i in range(n)) ** 0.5
        w = new_w
        if delta < tol:
            break

    return {b: w[idx[b]] for b in brands}


def rankings_from_results(results, target_name: str) -> list[list[str]]:
    """
    Build Plackett-Luce-ready rankings from a list of LLMRankingResult rows.

    For each successful response that produced a position for the target
    and/or competitors, emit a ranking ordered by ascending position.
    """
    rankings: list[list[str]] = []
    for r in results:
        if not r.query_succeeded:
            continue
        items: list[tuple[int, str]] = []
        if r.is_mentioned and r.mention_rank:
            items.append((int(r.mention_rank), target_name))
        for c in (r.competitors_mentioned or []):
            if not isinstance(c, dict):
                continue
            name = (c.get("name") or "").strip()
            pos = c.get("position")
            if name and pos:
                try:
                    items.append((int(pos), name))
                except (TypeError, ValueError):
                    continue
        if len(items) < 2:
            continue
        items.sort(key=lambda t: t[0])
        # Deduplicate while preserving order (in case the same brand was
        # extracted twice with different positions).
        seen = set()
        ordered = []
        for _, name in items:
            if name in seen:
                continue
            seen.add(name)
            ordered.append(name)
        if len(ordered) >= 2:
            rankings.append(ordered)
    return rankings

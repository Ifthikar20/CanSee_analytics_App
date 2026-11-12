"""What changed per tracked prompt and engine, for the agent's visibility
history tool and the browser pane's Keywords tab.

Both read answer_changes: each tracked prompt and engine's newest answer in
the window against the one before it. The agent tool also gets the 90-day
weekly visibility series from the dashboard; the pane does not need it. The
window is 1 to 90 days and never longer, so a change is always between the
newest two answers of at most 90 days.
"""
from __future__ import annotations

from apps.llm_ranking.services.answer_changes import WINDOW_DAYS, answer_changes
from core.exceptions import CanseeException

NOTHING_TO_COMPARE = (
    "No engine has answered a tracked prompt in the last {days} days, so nothing can be "
    "compared."
)


def parse_days(raw) -> int:
    """The window from a query value: WINDOW_DAYS when absent, else a whole
    number of days from 1 to WINDOW_DAYS (a 400 otherwise)."""
    if raw is None or raw == "":
        return WINDOW_DAYS
    try:
        days = int(raw)
    except (TypeError, ValueError):
        days = 0
    if not 1 <= days <= WINDOW_DAYS:
        message = f"Choose a window of 1 to {WINDOW_DAYS} days."
        raise CanseeException(
            message, code="invalid", status_code=400,
            details={"field": "days", "message": message},
        )
    return days


def build_visibility_history(website, *, days: int = WINDOW_DAYS, weekly: bool = True,
                             now=None) -> dict:
    """``{window_days, counts, latest_check_at, previous_check_at, changes,
    changes_total}``, plus ``note`` when nothing could be compared.

    ``changes`` holds every answer_changes row (one per tracked prompt and
    engine, keyed by ``prompt_id`` like the search-gap overview's prompts[]),
    uncapped. With ``weekly`` the result also carries the dashboard's 90-day
    ``weekly`` series and ``engines``; that series only covers 90 days.
    """
    if weekly and days != WINDOW_DAYS:
        raise ValueError(f"The weekly series covers {WINDOW_DAYS} days only.")
    changes = answer_changes(website, days=days, now=now)
    rows = changes["rows"]
    result: dict = {"window_days": days}
    if weekly:
        from apps.llm_ranking.services.visibility_dashboard import build_dashboard

        board = build_dashboard(website, period="90d", interval="week")
        visibility = board["visibility"]
        result["weekly"] = {
            "weeks": board["window"]["buckets"],
            "visibility_pct": visibility["all"]["visibility_pct"],
            "answers": visibility["all"]["answers"],
            "by_engine": [{"engine": e["key"], "visibility_pct": e["visibility_pct"]}
                          for e in visibility["by_engine"]],
        }
        result["engines"] = board["engines"]
    result.update({
        "counts": changes["counts"],
        "latest_check_at": changes["latest_check_at"],
        "previous_check_at": changes["previous_check_at"],
        "changes": rows,
        "changes_total": len(rows),
    })
    if not rows:
        result["note"] = NOTHING_TO_COMPARE.format(days=days)
    return result

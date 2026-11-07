"""Typed view of the AGENT_* settings (config/settings/base.py).

Read through agent_conf() at the point of use, never at import time, so
override_settings and per-process settings modules take effect.
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from django.conf import settings
from django.core import checks

RUN_MODES = ("thread", "inline")
EFFORT_LEVELS = ("low", "medium", "high", "xhigh", "max")
# The worker requests turn thinking off, which the API accepts only at
# these levels (xhigh and max return a 400).
WORKER_EFFORT_LEVELS = ("low", "medium", "high")
CACHE_TTLS = ("5m", "1h")

# Settings of the workspace half of wave 2, with the defaults of
# config/settings/base.py: read with these defaults, so the agent app
# works the same before and after the settings block is there.
DEFAULTS = {
    "AGENT_OPTIMIZER_MAX_TOKENS": 24000,
    "AGENT_COMPARISON_MAX_TOKENS": 24000,
    "AGENT_SOCIAL_MAX_TOKENS": 8000,
    "AGENT_PR_MAX_TOKENS": 16000,
    "AGENT_LLMS_TXT_MAX_TOKENS": 8000,
    "AGENT_MESSAGING_MAX_TOKENS": 16000,
    "AGENT_WRITER_MAX_PAGES": 5,
    "AGENT_CORRECTION_MAX_PAGES": 5,
    "AGENT_REVIEW_EMAILS": True,
    "CONTEXT_BUILD_MAX_PAGES": 8,
    "CONTEXT_CHECK_MAX_PAGES": 20,
    "CONTEXT_BUILD_DAILY_RUNS": 5,
    "CONTEXT_BUILD_USD_PER_REQUEST": "0.0100",
    "CONTEXT_BUILD_MAX_TOKENS": 4000,
}


def _setting(name: str):
    if name in DEFAULTS:
        return getattr(settings, name, DEFAULTS[name])
    return getattr(settings, name)


_POSITIVE_INT_SETTINGS = (
    "AGENT_MAX_TOKENS",
    "AGENT_LANDING_MAX_TOKENS",
    "AGENT_ARTICLE_MAX_TOKENS",
    "AGENT_SHEET_MAX_TOKENS",
    "AGENT_MAX_REQUESTS_PER_RUN",
    "AGENT_RUN_MAX_SECONDS",
    "AGENT_MAX_CONCURRENT_RUNS",
    "AGENT_USER_ACTIVE_RUNS",
    "AGENT_WEB_SEARCH_MAX_USES",
    "AGENT_WEB_FETCH_MAX_USES",
    "AGENT_SERP_MAX_PAGES",
    "AGENT_BROWSE_DAILY_LIMIT",
    "AGENT_STREAM_MAX_SECONDS",
    "AGENT_HEARTBEAT_SECONDS",
    "AGENT_STALE_SECONDS",
    "AGENT_SWARM_MAX_TASKS",
    "AGENT_SWARM_WORKERS",
    "AGENT_SWARM_PROCESS_WORKERS",
    "AGENT_SWARM_SERP_CONCURRENCY",
    "AGENT_SWARM_BROWSE_CONCURRENCY",
    "AGENT_SWARM_ENGINE_CONCURRENCY",
    "AGENT_SWARM_HAIKU_CONCURRENCY",
    "AGENT_SWARM_MAX_SECONDS",
    "AGENT_WORKER_MAX_TOKENS",
    "AGENT_OPTIMIZER_MAX_TOKENS",
    "AGENT_COMPARISON_MAX_TOKENS",
    "AGENT_SOCIAL_MAX_TOKENS",
    "AGENT_PR_MAX_TOKENS",
    "AGENT_LLMS_TXT_MAX_TOKENS",
    "AGENT_MESSAGING_MAX_TOKENS",
    "AGENT_WRITER_MAX_PAGES",
    "AGENT_CORRECTION_MAX_PAGES",
    "CONTEXT_BUILD_MAX_PAGES",
    "CONTEXT_BUILD_DAILY_RUNS",
    "CONTEXT_BUILD_MAX_TOKENS",
)


@dataclass(frozen=True)
class AgentConf:
    enabled: bool
    process: bool
    run_mode: str
    model: str
    effort: str
    subagent_effort: str
    title_effort: str
    max_tokens: int
    landing_max_tokens: int
    article_max_tokens: int
    sheet_max_tokens: int
    max_requests_per_run: int
    run_max_seconds: int
    run_max_cost_usd: float
    max_concurrent_runs: int
    user_active_runs: int
    cache_ttl: str
    fallbacks_enabled: bool
    web_tools_enabled: bool
    web_search_max_uses: int
    web_fetch_max_uses: int
    serp_max_pages: int
    browse_daily_limit: int
    stream_max_seconds: int
    heartbeat_seconds: int
    stale_seconds: int
    chat_retention_days: int
    swarm_enabled: bool
    swarm_max_tasks: int
    swarm_workers: int
    swarm_process_workers: int
    swarm_serp_concurrency: int
    swarm_browse_concurrency: int
    swarm_engine_concurrency: int
    swarm_haiku_concurrency: int
    swarm_max_seconds: int
    worker_model: str
    worker_effort: str
    worker_max_tokens: int
    optimizer_max_tokens: int
    comparison_max_tokens: int
    social_max_tokens: int
    pr_max_tokens: int
    llms_txt_max_tokens: int
    messaging_max_tokens: int
    writer_max_pages: int
    correction_max_pages: int
    review_emails: bool
    context_build_max_pages: int
    context_check_max_pages: int
    context_build_daily_runs: int
    context_build_usd_per_request: Decimal
    context_build_max_tokens: int


def agent_conf() -> AgentConf:
    """The current AGENT_* settings."""
    s = settings
    return AgentConf(
        enabled=bool(s.AGENT_ENABLED),
        process=bool(s.AGENT_PROCESS),
        run_mode=s.AGENT_RUN_MODE,
        model=s.AGENT_MODEL,
        effort=s.AGENT_EFFORT,
        subagent_effort=s.AGENT_SUBAGENT_EFFORT,
        title_effort=s.AGENT_TITLE_EFFORT,
        max_tokens=int(s.AGENT_MAX_TOKENS),
        landing_max_tokens=int(s.AGENT_LANDING_MAX_TOKENS),
        article_max_tokens=int(s.AGENT_ARTICLE_MAX_TOKENS),
        sheet_max_tokens=int(s.AGENT_SHEET_MAX_TOKENS),
        max_requests_per_run=int(s.AGENT_MAX_REQUESTS_PER_RUN),
        run_max_seconds=int(s.AGENT_RUN_MAX_SECONDS),
        run_max_cost_usd=float(s.AGENT_RUN_MAX_COST_USD),
        max_concurrent_runs=int(s.AGENT_MAX_CONCURRENT_RUNS),
        user_active_runs=int(s.AGENT_USER_ACTIVE_RUNS),
        cache_ttl=s.AGENT_CACHE_TTL,
        fallbacks_enabled=bool(s.AGENT_FALLBACKS_ENABLED),
        web_tools_enabled=bool(s.AGENT_WEB_TOOLS_ENABLED),
        web_search_max_uses=int(s.AGENT_WEB_SEARCH_MAX_USES),
        web_fetch_max_uses=int(s.AGENT_WEB_FETCH_MAX_USES),
        serp_max_pages=int(s.AGENT_SERP_MAX_PAGES),
        browse_daily_limit=int(s.AGENT_BROWSE_DAILY_LIMIT),
        stream_max_seconds=int(s.AGENT_STREAM_MAX_SECONDS),
        heartbeat_seconds=int(s.AGENT_HEARTBEAT_SECONDS),
        stale_seconds=int(s.AGENT_STALE_SECONDS),
        chat_retention_days=int(s.AGENT_CHAT_RETENTION_DAYS),
        swarm_enabled=bool(s.AGENT_SWARM_ENABLED),
        swarm_max_tasks=int(s.AGENT_SWARM_MAX_TASKS),
        swarm_workers=int(s.AGENT_SWARM_WORKERS),
        swarm_process_workers=int(s.AGENT_SWARM_PROCESS_WORKERS),
        swarm_serp_concurrency=int(s.AGENT_SWARM_SERP_CONCURRENCY),
        swarm_browse_concurrency=int(s.AGENT_SWARM_BROWSE_CONCURRENCY),
        swarm_engine_concurrency=int(s.AGENT_SWARM_ENGINE_CONCURRENCY),
        swarm_haiku_concurrency=int(s.AGENT_SWARM_HAIKU_CONCURRENCY),
        swarm_max_seconds=int(s.AGENT_SWARM_MAX_SECONDS),
        worker_model=s.AGENT_WORKER_MODEL,
        worker_effort=s.AGENT_WORKER_EFFORT,
        worker_max_tokens=int(s.AGENT_WORKER_MAX_TOKENS),
        optimizer_max_tokens=int(_setting("AGENT_OPTIMIZER_MAX_TOKENS")),
        comparison_max_tokens=int(_setting("AGENT_COMPARISON_MAX_TOKENS")),
        social_max_tokens=int(_setting("AGENT_SOCIAL_MAX_TOKENS")),
        pr_max_tokens=int(_setting("AGENT_PR_MAX_TOKENS")),
        llms_txt_max_tokens=int(_setting("AGENT_LLMS_TXT_MAX_TOKENS")),
        messaging_max_tokens=int(_setting("AGENT_MESSAGING_MAX_TOKENS")),
        writer_max_pages=int(_setting("AGENT_WRITER_MAX_PAGES")),
        correction_max_pages=int(_setting("AGENT_CORRECTION_MAX_PAGES")),
        review_emails=bool(_setting("AGENT_REVIEW_EMAILS")),
        context_build_max_pages=int(_setting("CONTEXT_BUILD_MAX_PAGES")),
        context_check_max_pages=int(_setting("CONTEXT_CHECK_MAX_PAGES")),
        context_build_daily_runs=int(_setting("CONTEXT_BUILD_DAILY_RUNS")),
        context_build_usd_per_request=Decimal(str(_setting("CONTEXT_BUILD_USD_PER_REQUEST"))),
        context_build_max_tokens=int(_setting("CONTEXT_BUILD_MAX_TOKENS")),
    )


def api_key_configured() -> bool:
    """True when ANTHROPIC_API_KEY is set (runs can start)."""
    return bool(getattr(settings, "ANTHROPIC_API_KEY", ""))


def check_agent_settings(app_configs=None, **kwargs):
    """System check: reject AGENT_* values the runtime cannot honour."""
    errors = []

    def _error(setting: str, message: str, code: int):
        errors.append(checks.Error(f"{setting}: {message}", id=f"agent.E{code:03d}"))

    if settings.AGENT_RUN_MODE not in RUN_MODES:
        _error("AGENT_RUN_MODE", f"must be one of {', '.join(RUN_MODES)}.", 1)
    for setting in ("AGENT_EFFORT", "AGENT_SUBAGENT_EFFORT", "AGENT_TITLE_EFFORT"):
        if getattr(settings, setting) not in EFFORT_LEVELS:
            _error(setting, f"must be one of {', '.join(EFFORT_LEVELS)}.", 2)
    if settings.AGENT_WORKER_EFFORT not in WORKER_EFFORT_LEVELS:
        _error("AGENT_WORKER_EFFORT", f"must be one of {', '.join(WORKER_EFFORT_LEVELS)}.", 2)
    if settings.AGENT_CACHE_TTL not in CACHE_TTLS:
        _error("AGENT_CACHE_TTL", f"must be one of {', '.join(CACHE_TTLS)}.", 3)
    for setting in _POSITIVE_INT_SETTINGS:
        value = _setting(setting)
        if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
            _error(setting, "must be a positive integer.", 4)
    checks_left = _setting("CONTEXT_CHECK_MAX_PAGES")
    if isinstance(checks_left, bool) or not isinstance(checks_left, int) or checks_left < 0:
        _error("CONTEXT_CHECK_MAX_PAGES", "must be zero (no re-check) or a positive integer.", 9)
    try:
        if Decimal(str(_setting("CONTEXT_BUILD_USD_PER_REQUEST"))) <= 0:
            raise ValueError
    except (ArithmeticError, ValueError):
        _error("CONTEXT_BUILD_USD_PER_REQUEST", "must be an amount above zero.", 10)
    if not settings.AGENT_MODEL:
        _error("AGENT_MODEL", "must name a model.", 5)
    if settings.AGENT_RUN_MAX_COST_USD <= 0:
        _error("AGENT_RUN_MAX_COST_USD", "must be greater than zero.", 6)
    if settings.AGENT_CHAT_RETENTION_DAYS < 0:
        _error("AGENT_CHAT_RETENTION_DAYS", "must be zero (keep) or a number of days.", 7)
    if not settings.AGENT_WORKER_MODEL:
        _error("AGENT_WORKER_MODEL", "must name a model.", 8)
    return errors

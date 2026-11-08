"""Celery configuration for Cansee."""
import os

from celery import Celery
from celery.schedules import crontab

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings.prod")

app = Celery("cansee")
app.config_from_object("django.conf:settings", namespace="CELERY")
app.autodiscover_tasks()
# core is not an installed app, so autodiscovery skips it; its scheduled
# tasks (hard_delete_soft_deleted, check_encryption_key_rotation) were
# rejected by the worker as unregistered.
app.autodiscover_tasks(["core"])

# ── Queue topology ──
# Slow third-party API calls must not block pixel/analytics aggregation.
#   default      — analytics, pixel, accounts (fast, in-process work)
#   integrations — third-party HTTP (OAuth token refresh, Search Console,
#                  SerpAPI, Polar, report digests) and long batch deletes
#   webhooks     — outbound webhook delivery (user-controlled URLs, slow, isolated)
#   ai           — LLM ranking
app.conf.task_default_queue = "default"
app.conf.task_routes = {
    # webhooks
    "apps.websites.tasks.deliver_webhook": {"queue": "webhooks"},
    # integrations / OAuth
    "apps.websites.tasks.refresh_expiring_tokens": {"queue": "integrations"},
    "apps.search_console.tasks.*": {"queue": "integrations"},
    # Google rank checks are third-party HTTP (SerpAPI), not LLM work.
    "apps.search_gap.tasks.*": {"queue": "integrations"},
    # AI / LLM
    "apps.llm_ranking.tasks.*": {"queue": "ai"},
    "apps.prompt_library.tasks.dispatch_scheduled_prompt_scans": {"queue": "ai"},
    "apps.citations.tasks.*": {"queue": "ai"},
    "apps.brand_vault.tasks.*": {"queue": "ai"},
    # Brand Pulse delivers to third-party chat webhooks (no LLM work), so it
    # belongs with the other digest senders. The exact-name route wins over
    # the brand_vault glob above.
    "apps.brand_vault.tasks.run_brand_pulse": {"queue": "integrations"},
    # notifications: chat commands may call an LLM; report digests hit
    # third-party webhooks
    "apps.notifications.tasks.answer_chat_command": {"queue": "ai"},
    "apps.notifications.tasks.send_daily_growth_reports": {"queue": "integrations"},
    "apps.notifications.tasks.send_weekly_reports": {"queue": "integrations"},
    # metering: Polar delivery is a third-party HTTP call, and the ai queue
    # can be saturated for tens of minutes by audit fan-out — events must
    # keep flowing then (Polar attributes usage by ingestion time).
    "apps.metering.tasks.flush_polar_events": {"queue": "integrations"},
    "apps.metering.tasks.prune_ai_usage_ledger": {"queue": "integrations"},
    "apps.metering.tasks.prune_polar_outbox": {"queue": "integrations"},
    "apps.metering.tasks.notify_cap_threshold": {"queue": "default"},
    "apps.metering.tasks.release_stale_prompt_runs": {"queue": "default"},
    # Long-running batch deletes: keep them off the default queue so they
    # cannot delay request-path work.
    "apps.analytics.tasks.prune_analytics_events": {"queue": "integrations"},
    "apps.analytics.tasks.prune_llm_results": {"queue": "integrations"},
    "apps.agent.tasks.purge_expired_agent_chats": {"queue": "integrations"},
    # Autopilot: the plan and the engines are LLM work; Google and the text
    # are third-party HTTP; the dispatcher and finalize are quick DB work.
    # The exact names win over the glob.
    "apps.autopilot.tasks.*": {"queue": "ai"},
    "apps.autopilot.tasks.dispatch_autopilot": {"queue": "default"},
    "apps.autopilot.tasks.finalize_run": {"queue": "default"},
    "apps.autopilot.tasks.run_google": {"queue": "integrations"},
    "apps.autopilot.tasks.deliver_text": {"queue": "integrations"},
}

app.conf.beat_schedule = {
    # ── Daily ──
    "expire-sessions": {
        "task": "apps.accounts.tasks.expire_inactive_sessions",
        "schedule": crontab(minute=0, hour=3),
    },
    # Re-check DNS TXT proof on verified org domains; a domain that
    # expired or changed hands loses verified status (and with it
    # auto-join) after 3 consecutive failures.
    "reverify-org-domains": {
        "task": "apps.accounts.tasks.reverify_org_domains",
        "schedule": crontab(minute=20, hour=6),
    },
    # Agent chats idle past AGENT_CHAT_RETENTION_DAYS (0, the default,
    # keeps every chat and makes this a no-op).
    "purge-expired-agent-chats": {
        "task": "apps.agent.tasks.purge_expired_agent_chats",
        "schedule": crontab(minute=40, hour=3),
    },
    # ── Weekly ──
    "weekly-reports": {
        "task": "apps.notifications.tasks.send_weekly_reports",
        "schedule": crontab(minute=0, hour=9, day_of_week=1),
    },
    # ── Hourly ──
    "analytics-aggregation": {
        "task": "apps.analytics.tasks.aggregate_hourly_metrics",
        "schedule": crontab(minute=5),
    },
    # ── Every minute ──
    # Agent runs whose process stopped heartbeating (deploy, crash) end as
    # interrupted. Default queue; a cheap no-op when nothing is running.
    "sweep-stale-agent-runs": {
        "task": "apps.agent.tasks.sweep_stale_agent_runs",
        "schedule": crontab(minute="*"),
    },
    # ── Every 5 minutes ──
    # Autopilot: starts each project's daily run once its local run time has
    # passed, releases runs whose analysis ended, resumes silent stages and
    # sends texts that waited for quiet hours. A cheap no-op when idle.
    "autopilot-dispatch": {
        "task": "apps.autopilot.tasks.dispatch_autopilot",
        "schedule": crontab(minute="*/5"),
    },
    # ── Every 15 minutes ──
    "refresh-expiring-oauth-tokens": {
        "task": "apps.websites.tasks.refresh_expiring_tokens",
        "schedule": crontab(minute="*/15"),
    },
    # Prompt runs a worker reserved and never settled (it died mid-call)
    # are given back to the free plan's monthly count.
    "release-stale-prompt-runs": {
        "task": "apps.metering.tasks.release_stale_prompt_runs",
        "schedule": crontab(minute="*/15"),
    },
    # ── Monthly ──
    "hard-delete-expired": {
        "task": "core.tasks.hard_delete_soft_deleted",
        "schedule": crontab(minute=0, hour=0, day_of_month=1),
    },
    "rotate-encryption-keys": {
        "task": "core.tasks.check_encryption_key_rotation",
        "schedule": crontab(minute=0, hour=0, day_of_month=1),
    },
    # ── Integration Reports ──
    "daily-growth-reports": {
        "task": "apps.notifications.tasks.send_daily_growth_reports",
        "schedule": crontab(minute=0, hour=9),  # 9 AM daily
    },
    # ── LLM Ranking ──
    "llm-ranking-schedule-dispatcher": {
        "task": "apps.llm_ranking.tasks.dispatch_scheduled_audits",
        "schedule": crontab(minute="*/15"),  # Every 15 min — checks next_run_at
    },
    # ── Prompt Library ──
    "mine-daily-prompts": {
        "task": "apps.prompt_library.tasks.mine_daily_prompts",
        "schedule": crontab(minute=0, hour=4),
    },
    "compute-demand-scores": {
        "task": "apps.prompt_library.tasks.compute_demand_scores",
        "schedule": crontab(minute=0, hour=5),
    },
    "refresh-prompt-effectiveness": {
        "task": "apps.prompt_library.tasks.refresh_effectiveness_scores",
        "schedule": crontab(minute=30, hour=2),
    },
    "prompt-schedule-dispatcher": {
        "task": "apps.prompt_library.tasks.dispatch_scheduled_prompt_scans",
        "schedule": crontab(minute="*/15"),  # Every 15 min — checks next_run_at
    },
    # ── Citations / Source Influence ──
    "compute-source-influence": {
        "task": "apps.citations.tasks.compute_source_influence_snapshots",
        "schedule": crontab(minute=30, hour=5),
    },
    "classify-unknown-domains": {
        "task": "apps.citations.tasks.classify_unknown_domains",
        "schedule": crontab(minute=0, hour=6),
    },
    # ── Brand Vault ──
    "refresh-fact-embeddings": {
        "task": "apps.brand_vault.tasks.refresh_fact_embeddings",
        "schedule": crontab(minute=30, hour=3),
    },
    # Catch-up sweep for Brand Security: audits any stored responses the
    # completion hooks missed. Offset to :25 to stay clear of the :00/:15
    # dispatcher cluster.
    "brand-security-response-scan": {
        "task": "apps.brand_vault.tasks.scan_unaudited_responses",
        "schedule": crontab(minute=25),
    },
    # Brand Pulse digest sweep. Offset to :20 to stay clear of the :00/:15
    # dispatcher cluster; weekly pulses only fire on Mondays inside the task.
    "brand-pulse-digest": {
        "task": "apps.brand_vault.tasks.run_brand_pulse",
        "schedule": crontab(minute=20, hour=9),
    },
    # ── Search Console ──
    # Runs before compute-demand-scores (05:00) so fresh GSC_AGGREGATE
    # prompts are scored the same morning.
    "gsc-nightly-sync": {
        "task": "apps.search_console.tasks.sync_all_gsc",
        "schedule": crontab(minute=0, hour=3),
    },
    # ── Polar usage metering ──
    # Sweeper for outbox rows whose on_commit fast-path dispatch was lost
    # (broker blip, worker restart). Cheap no-op when the outbox is empty.
    "polar-outbox-sweep": {
        "task": "apps.metering.tasks.flush_polar_events",
        "schedule": crontab(minute="*/2"),
    },
    "prune-ai-usage-ledger": {
        "task": "apps.metering.tasks.prune_ai_usage_ledger",
        "schedule": crontab(minute=40, hour=2),
    },
    "prune-polar-outbox": {
        "task": "apps.metering.tasks.prune_polar_outbox",
        "schedule": crontab(minute=50, hour=2, day_of_week=0),
    },
    # Retention. Windows live in settings (ANALYTICS_RETENTION_DAYS et al) and
    # are published on /what-we-track, so these two tasks are what makes that
    # page true. Scheduled off-peak and after the metering prunes.
    #
    # NOTE: the first production run clears a backlog that has never been
    # pruned. Run `manage.py prune_retention --dry-run` and check the counts
    # before letting beat fire this.
    "prune-analytics-events": {
        "task": "apps.analytics.tasks.prune_analytics_events",
        "schedule": crontab(minute=10, hour=3),
    },
    "prune-llm-results": {
        "task": "apps.analytics.tasks.prune_llm_results",
        "schedule": crontab(minute=30, hour=3, day_of_week=0),
    },
}

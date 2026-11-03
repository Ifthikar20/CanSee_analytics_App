from celery import shared_task


@shared_task(name="apps.agent.tasks.sweep_stale_agent_runs", ignore_result=True)
def sweep_stale_agent_runs() -> int:
    """Runs whose process stopped heartbeating end as interrupted (beat, every minute)."""
    from apps.agent.runtime.sweeper import sweep_stale_runs

    return sweep_stale_runs()


@shared_task(name="apps.agent.tasks.purge_expired_agent_chats", ignore_result=True)
def purge_expired_agent_chats() -> int:
    """Chats idle past AGENT_CHAT_RETENTION_DAYS are deleted (beat, daily;
    0 keeps every chat)."""
    from apps.agent.services.chats import purge_expired_chats

    return purge_expired_chats()

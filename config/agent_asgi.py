"""ASGI entry point of the agent service (uvicorn, compose profile "agent").

The plain Django application: the agent process serves HTTP only (event
streams, long-poll, run and browse endpoints). Loading it also starts the
agent's process services, so the run heartbeat, the recovery of runs a
previous process on this host left running and the live flag status/
reads are up before the first request instead of after it.
"""
import os

from django.core.asgi import get_asgi_application

# Assigned, not setdefault: importing this module first runs
# config/__init__.py, whose Celery app has already defaulted the variable to
# the prod settings, and those cannot serve the live endpoints (AGENT_PROCESS
# is off, connections persist). This module is only ever the agent service's
# entry point. Celery reads settings lazily, so they are not configured yet.
os.environ["DJANGO_SETTINGS_MODULE"] = "config.settings.agent"

application = get_asgi_application()

from apps.agent.runtime.registry import start_process_services  # noqa: E402

start_process_services()

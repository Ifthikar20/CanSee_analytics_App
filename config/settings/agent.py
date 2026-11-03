"""Settings for the agent ASGI service (streams and runs agent turns).

Production settings plus: no persistent DB connections (Django advises
against them under ASGI, and runner threads close theirs when done), and
this process is the one allowed to execute runs.
"""
from .prod import *  # noqa

DATABASES["default"]["CONN_MAX_AGE"] = 0  # noqa: F405
AGENT_PROCESS = True
AGENT_RUN_MODE = "thread"

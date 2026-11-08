from .base import *  # noqa

DEBUG = True
SECURE_SSL_REDIRECT = False
ALLOWED_HOSTS = ["*"]
CORS_ALLOW_ALL_ORIGINS = True

# Run prompt scans inline (background thread) by default in dev so a plain
# runserver with no Celery worker/Redis still fills in mentions. Override
# with LLM_SCAN_MODE=celery if you run a worker locally.
LLM_SCAN_MODE = env("LLM_SCAN_MODE", default="inline")  # noqa: F405

# Chat-bot answers (Discord/Slack) must be dispatched AFTER the webhook
# view returns its 3-second ack. Eager Celery would run them inline
# before the ack, so dev uses a background thread instead.
CHAT_EAGER_THREAD_DISPATCH = True

# runserver is the agent process in dev: it serves the live endpoints and
# runs turns on the background executor. A dev stack served by several
# processes (docker/docker-compose.yml runs gunicorn workers) sets
# AGENT_PROCESS=false: each worker would keep its own runs and interrupt
# the others' as orphans. The agent is on unless .env sets
# AGENT_ENABLED=false; it still needs ANTHROPIC_API_KEY to run.
AGENT_ENABLED = env.bool("AGENT_ENABLED", default=True)  # noqa: F405
AGENT_PROCESS = env.bool("AGENT_PROCESS", default=True)  # noqa: F405
AGENT_RUN_MODE = "thread"

# Disable rate limiting in dev
REST_FRAMEWORK["DEFAULT_THROTTLE_CLASSES"] = []  # noqa: F405

# Use console email backend
EMAIL_BACKEND = "django.core.mail.backends.console.EmailBackend"

# Disable SSL requirement for DB in dev
DATABASES["default"]["OPTIONS"] = {"connect_timeout": 10}  # noqa: F405

# Don't keep DB connections open in dev. runserver auto-reload leaves the
# prior process's persistent connections idle until CONN_MAX_AGE expires,
# which quickly exhausts Postgres max_connections (default 100).
DATABASES["default"]["CONN_MAX_AGE"] = 0  # noqa: F405
DATABASES["default"]["CONN_HEALTH_CHECKS"] = False  # noqa: F405

# Django Debug Toolbar
try:
    import debug_toolbar  # noqa: F401
    INSTALLED_APPS += ["debug_toolbar"]  # noqa: F405
    MIDDLEWARE += ["debug_toolbar.middleware.DebugToolbarMiddleware"]  # noqa: F405
    INTERNAL_IPS = ["127.0.0.1"]
except ImportError:
    pass

# Simpler logging in dev — quiet the per-request noise from django.server,
# django_structlog (request_started / request_finished), and the audit
# middleware's INFO-level GET log so the console is readable. Errors,
# warnings, and our own apps logger still surface.
LOGGING["loggers"]["django"]["level"] = "WARNING"  # noqa: F405
LOGGING["loggers"]["django.server"] = {"handlers": ["console"], "level": "WARNING"}  # noqa: F405
LOGGING["loggers"]["django_structlog"] = {"handlers": ["console"], "level": "WARNING"}  # noqa: F405
LOGGING["loggers"]["audit"]["level"] = "WARNING"  # noqa: F405

# Run Celery tasks synchronously in dev (no Redis/worker needed)
CELERY_TASK_ALWAYS_EAGER = True
CELERY_TASK_EAGER_PROPAGATES = True

# Dev-only default so the ftb-min admin dashboard works out of the box
# against the local container (prod must set ADMIN_OPS_KEY via env).
if not ADMIN_OPS_KEY:  # noqa: F405
    ADMIN_OPS_KEY = "dev-admin-ops-key"

# Dev-only: the admin server runs on this same machine, so loopback and
# the docker bridge nets may call the internal ops surface. Production
# must set ADMIN_OPS_ALLOWED_CIDRS to the admin host's address only.
if not ADMIN_OPS_ALLOWED_CIDRS:  # noqa: F405
    # Loopback + the docker bridge nets only — NOT all private ranges;
    # a dev box on an office LAN should not serve the PII directory to
    # every 192.168.* neighbour holding the committed dev key.
    ADMIN_OPS_ALLOWED_CIDRS = ["127.0.0.0/8", "172.16.0.0/12"]

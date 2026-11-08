from datetime import timedelta
from pathlib import Path

import environ
from django.core.exceptions import ImproperlyConfigured

BASE_DIR = Path(__file__).resolve().parent.parent.parent

env = environ.Env(
    DEBUG=(bool, False),
    ALLOWED_HOSTS=(list, []),
)

# Read .env file if present
environ.Env.read_env(BASE_DIR / ".env")

# ── CORE ──
SECRET_KEY = env("DJANGO_SECRET_KEY", default="unsafe-secret-key-change-in-production")
DEBUG = env("DEBUG")
ALLOWED_HOSTS = env("ALLOWED_HOSTS")

# ── APPS ──
DJANGO_APPS = [
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
]

THIRD_PARTY_APPS = [
    "rest_framework",
    "rest_framework_simplejwt",
    "rest_framework_simplejwt.token_blacklist",
    "corsheaders",
    "django_filters",
    "django_celery_beat",
    "django_celery_results",
    "drf_spectacular",
    "django_structlog",
    "axes",
    "django_otp",
    "storages",
    "health_check",
    "health_check.db",
    "health_check.cache",
    "health_check.storage",
]

LOCAL_APPS = [
    "apps.accounts",
    "apps.websites",
    "apps.analytics",
    "apps.notifications",
    "apps.billing",
    "apps.metering",
    "apps.llm_ranking",
    "apps.rag",
    "apps.onboarding",
    "apps.prompt_library",
    "apps.citations",
    "apps.brand_vault",
    "apps.content_studio",
    "apps.search_console",
    "apps.search_gap",
    "apps.assistant",
    "apps.agent",
    "apps.web_analytics",
    "apps.insights",
    "apps.boards",
    "apps.prompt_box",
    "apps.autopilot",
    # Stub app — kept only to satisfy historical lazy FK references
    # from analytics migrations. All tables are managed=False.
    "apps.leads",
]

# Phase 2: extract citations from each LLMRankingResult after it's saved.
# Toggle off to disable the post-save hook (e.g. in narrow unit tests).
CITATION_EXTRACTION_ENABLED = True

# Phase 3 flags. CLAIM_VERIFICATION_ENABLED now gates the brand-alignment
# benchmark (embedding-based scoring of every response against Brand
# Input); BRAND_VAULT_EXTRACTION_ENABLED gates fact extraction, including
# the auto-extraction dispatched after Brand Input ingests.
CLAIM_VERIFICATION_ENABLED = True
BRAND_VAULT_EXTRACTION_ENABLED = True

# Brand Security: deployment-wide kill switch for the LLM judge step on
# nuanced detectors. Per-website opt-out lives on BrandSecurityConfig.
BRAND_SECURITY_JUDGE_ENABLED = env.bool("BRAND_SECURITY_JUDGE_ENABLED", default=True)

INSTALLED_APPS = DJANGO_APPS + THIRD_PARTY_APPS + LOCAL_APPS

# ── AUTH ──
AUTH_USER_MODEL = "accounts.User"
AUTHENTICATION_BACKENDS = [
    "axes.backends.AxesStandaloneBackend",
    "django.contrib.auth.backends.ModelBackend",
]

AUTH_PASSWORD_VALIDATORS = [
    {"NAME": "django.contrib.auth.password_validation.UserAttributeSimilarityValidator"},
    {
        "NAME": "django.contrib.auth.password_validation.MinimumLengthValidator",
        "OPTIONS": {"min_length": 12},
    },
    {"NAME": "django.contrib.auth.password_validation.CommonPasswordValidator"},
    {"NAME": "django.contrib.auth.password_validation.NumericPasswordValidator"},
]

# ── REST FRAMEWORK ──
REST_FRAMEWORK = {
    "DEFAULT_AUTHENTICATION_CLASSES": [
        "rest_framework_simplejwt.authentication.JWTAuthentication",
    ],
    "DEFAULT_PERMISSION_CLASSES": [
        "rest_framework.permissions.IsAuthenticated",
    ],
    "DEFAULT_RENDERER_CLASSES": [
        "core.interceptors.response_envelope.EnvelopeRenderer",
    ],
    "DEFAULT_PARSER_CLASSES": [
        "rest_framework.parsers.JSONParser",
        "rest_framework.parsers.MultiPartParser",
    ],
    "DEFAULT_THROTTLE_CLASSES": [
        "core.interceptors.throttling.BurstRateThrottle",
        "core.interceptors.throttling.SustainedRateThrottle",
    ],
    "DEFAULT_THROTTLE_RATES": {
        "burst": "500/min",
        "sustained": "20000/hour",
        "auth": "60/min",
        "password_reset": "3/hour",
        "ai_generation": "10/hour",
        "pixel_ingest": "10000/min",
    },
    "DEFAULT_PAGINATION_CLASS": "core.interceptors.pagination.StandardPagination",
    "PAGE_SIZE": 25,
    "DEFAULT_FILTER_BACKENDS": [
        "django_filters.rest_framework.DjangoFilterBackend",
        "rest_framework.filters.SearchFilter",
        "rest_framework.filters.OrderingFilter",
    ],
    "EXCEPTION_HANDLER": "core.interceptors.exception_handler.custom_exception_handler",
    "DEFAULT_SCHEMA_CLASS": "drf_spectacular.openapi.AutoSchema",
    "DEFAULT_VERSIONING_CLASS": "rest_framework.versioning.URLPathVersioning",
    "DEFAULT_VERSION": "v1",
    "ALLOWED_VERSIONS": ["v1"],
}

# ── JWT CONFIG ──
#
# Access tokens are short-lived on purpose: they carry org/role claims and
# are not individually revocable, so their lifetime is the maximum staleness
# window after a role change, a member removal, or an SSO-enforcement flip.
# The SPA refreshes transparently on 401 (single-flight, api/client.js), so
# shortening this is invisible to users. Custom claims (org_id/org_role/amr)
# are set by apps.accounts.services.token_service — the single token
# factory — NOT via TOKEN_OBTAIN_SERIALIZER, which SimpleJWT only consumes
# from its own (unrouted) TokenObtainPairView.
SIMPLE_JWT = {
    "ACCESS_TOKEN_LIFETIME": timedelta(minutes=env.int("JWT_ACCESS_MINUTES", default=30)),
    "REFRESH_TOKEN_LIFETIME": timedelta(days=60),
    "ROTATE_REFRESH_TOKENS": True,
    "BLACKLIST_AFTER_ROTATION": True,
    "UPDATE_LAST_LOGIN": True,
    "ALGORITHM": "HS256",
    "SIGNING_KEY": env("JWT_SIGNING_KEY", default=SECRET_KEY),
    "AUTH_HEADER_TYPES": ("Bearer",),
    "AUTH_HEADER_NAME": "HTTP_AUTHORIZATION",
    "USER_ID_FIELD": "id",
    "USER_ID_CLAIM": "user_id",
}

# ── MIDDLEWARE (ORDER MATTERS) ──
MIDDLEWARE = [
    "core.middleware.request_id.RequestIDMiddleware",
    "core.middleware.security_headers.SecurityHeadersMiddleware",
    "apps.billing.middleware.rate_limiter.WebhookRateLimitMiddleware",
    "corsheaders.middleware.CorsMiddleware",
    "django.middleware.security.SecurityMiddleware",
    "core.middleware.request_sanitizer.RequestSanitizerMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "core.middleware.audit_log.AuditLogMiddleware",
    "core.middleware.analytics_access_log.AnalyticsAccessLogMiddleware",
    "core.middleware.rate_limit.AdaptiveRateLimitMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
    "axes.middleware.AxesMiddleware",
    "django_structlog.middlewares.RequestMiddleware",
]

ROOT_URLCONF = "config.urls"

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.debug",
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
            ],
        },
    },
]

WSGI_APPLICATION = "config.wsgi.application"

# ── DATABASE ──
DATABASES = {
    "default": {
        "ENGINE": "django.db.backends.postgresql",
        "NAME": env("DB_NAME", default="cansee"),
        "USER": env("DB_USER", default="postgres"),
        "PASSWORD": env("DB_PASSWORD", default="postgres"),
        "HOST": env("DB_HOST", default="localhost"),
        "PORT": env("DB_PORT", default="5432"),
        "CONN_MAX_AGE": 600,
        "CONN_HEALTH_CHECKS": True,
        "OPTIONS": {
            "connect_timeout": 10,
        },
    },
}

# ── CACHE ──
CACHES = {
    "default": {
        "BACKEND": "django_redis.cache.RedisCache",
        "LOCATION": env("REDIS_URL", default="redis://localhost:6379/0"),
        "OPTIONS": {
            "CLIENT_CLASS": "django_redis.client.DefaultClient",
        },
        "KEY_PREFIX": "gp",
        "TIMEOUT": 300,
    }
}

# ── CELERY ──
CELERY_BROKER_URL = env("CELERY_BROKER_URL", default="redis://localhost:6379/1")
CELERY_RESULT_BACKEND = "django-db"
CELERY_ACCEPT_CONTENT = ["json"]
CELERY_TASK_SERIALIZER = "json"
CELERY_RESULT_SERIALIZER = "json"
CELERY_TASK_TRACK_STARTED = True
CELERY_TASK_TIME_LIMIT = 300
CELERY_TASK_SOFT_TIME_LIMIT = 240

# How newly-added prompts get scanned. "celery" enqueues the chord task
# (production); "inline" runs the audit in a background thread so a dev
# server with no broker/worker still scans. See apps.llm_ranking.services
# .scan_dispatch.
LLM_SCAN_MODE = env("LLM_SCAN_MODE", default="celery")

# ── SECURITY ──
#
# PUBLIC_SCHEME is the single switch for "is this deployment reachable over
# TLS or not". It defaults to "https", so an unset environment gets the safe
# behaviour and a plaintext deployment has to be asked for explicitly.
#
# Everything TLS-dependent is derived from it rather than being six separate
# booleans, because the failure mode of setting them inconsistently is
# vicious: leave the cookies Secure on a plaintext host and the browser
# silently discards them, so login returns 200 and then the session dies on
# the next page load. Deriving them together makes that combination
# unreachable.
#
# The plaintext mode exists for deployments reached by bare IP, where no
# certificate authority will issue a certificate. It is not a preference --
# core.checks.check_public_scheme refuses to let it ship alongside a real
# hostname. When a domain is added, set PUBLIC_SCHEME=https (or unset it) and
# nothing else changes.
PUBLIC_SCHEME = env("PUBLIC_SCHEME", default="https").strip().lower()
if PUBLIC_SCHEME not in ("http", "https"):
    raise ImproperlyConfigured(
        f"PUBLIC_SCHEME must be 'http' or 'https', got {PUBLIC_SCHEME!r}."
    )
_TLS = PUBLIC_SCHEME == "https"

SECURE_SSL_REDIRECT = _TLS
# HSTS over plaintext is not merely useless, it is a trap: a browser that
# ever honoured it would refuse the http:// origin afterwards. Zero disables
# the header entirely.
SECURE_HSTS_SECONDS = 31536000 if _TLS else 0
SECURE_HSTS_INCLUDE_SUBDOMAINS = _TLS
SECURE_HSTS_PRELOAD = _TLS
SECURE_CONTENT_TYPE_NOSNIFF = True
SECURE_BROWSER_XSS_FILTER = True
# The Secure attribute is what makes a cookie undeliverable over http://.
SESSION_COOKIE_SECURE = _TLS
SESSION_COOKIE_HTTPONLY = True
SESSION_COOKIE_SAMESITE = "Lax"
CSRF_COOKIE_SECURE = _TLS
# /health/ must answer 200 over plain HTTP, not redirect. The container
# healthcheck and the deploy smoke test both curl it from inside the box on
# http://localhost:8000/, where no proxy header is set -- so with
# SECURE_SSL_REDIRECT on, Django 301s them. `curl -sf` does not fail on a
# 3xx and does not follow it, which means the web container has been
# reporting healthy on a redirect rather than on a working app.
SECURE_REDIRECT_EXEMPT = [r"^health/"]
X_FRAME_OPTIONS = "DENY"
# Keep users signed in across tabs / browser restarts. The cookie lives
# for 30 days, and SAVE_EVERY_REQUEST slides the expiry forward on every
# request so an active user never gets logged out mid-session.
SESSION_EXPIRE_AT_BROWSER_CLOSE = False
SESSION_COOKIE_AGE = 60 * 60 * 24 * 30  # 30 days
SESSION_SAVE_EVERY_REQUEST = True

# ── AXES ──
AXES_FAILURE_LIMIT = 5
AXES_COOLOFF_TIME = timedelta(minutes=30)
# Nested list = a SINGLE compound key (IP + username together). A flat list
# would make each entry an INDEPENDENT lockout key, so 5 bad passwords for a
# victim's username would lock that account out from every IP — a targeted
# lockout DoS. Lock only the (IP, username) pair instead.
AXES_LOCKOUT_PARAMETERS = [["ip_address", "username"]]
# Derive the client IP the same proxy-aware way as core.utils.get_client_ip:
# trust the last hop (our nginx) and read the entry just before it, so the
# lockout key cannot be moved around with a spoofed left-most X-Forwarded-For.
AXES_IPWARE_META_PRECEDENCE_ORDER = ("HTTP_X_FORWARDED_FOR", "REMOTE_ADDR")
AXES_IPWARE_PROXY_COUNT = 1
AXES_RESET_ON_SUCCESS = True

# ── CORS ──
CORS_ALLOWED_ORIGINS = env.list("CORS_ALLOWED_ORIGINS", default=[])
CORS_ALLOW_CREDENTIALS = True
CORS_ALLOW_HEADERS = [
    "accept",
    "authorization",
    "content-type",
    "x-request-id",
    "x-client-id",
    "x-csrftoken",
    # Agent workspace: message POSTs dedupe on Idempotency-Key, and the
    # event stream resumes from Last-Event-ID.
    "idempotency-key",
    "last-event-id",
]

# ── STATIC & MEDIA ──
STATIC_URL = "/static/"
STATIC_ROOT = BASE_DIR / "staticfiles"
MEDIA_URL = "/media/"
MEDIA_ROOT = BASE_DIR / "media"

# ── INTERNATIONALIZATION ──
LANGUAGE_CODE = "en-us"
TIME_ZONE = "UTC"
USE_I18N = True
USE_TZ = True

DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

# ── FIELD ENCRYPTION ──
# Database hosts on private infrastructure that are allowed to run without
# enforced TLS. Loopback, Unix sockets and single-label container hostnames
# (DB_HOST=db) are recognised automatically -- this is only needed for a
# dotted name that is nonetheless private, e.g. a VPC-internal DNS record.
# Consumed by core.checks.check_database_ssl (deploy check only).
DB_TRUSTED_HOSTS = env.list("DB_TRUSTED_HOSTS", default=[])

FIELD_ENCRYPTION_KEY = env("FIELD_ENCRYPTION_KEY", default="")
# When true, EncryptedTextField refuses to read or write plaintext if the key
# is missing (fail closed) and a Django system check errors at startup. Left
# false in base for local dev; prod.py forces it true so OAuth tokens / webhook
# secrets can never silently land in Postgres as plaintext.
FIELD_ENCRYPTION_REQUIRED = env.bool("FIELD_ENCRYPTION_REQUIRED", default=False)

# ── EXTERNAL SERVICES ──
OPENAI_API_KEY = env("OPENAI_API_KEY", default="")
ANTHROPIC_API_KEY = env("ANTHROPIC_API_KEY", default="")
# Model overrides. Defaults are the cheapest current Claude model
# (Haiku 4.5); set these to a Sonnet/Opus id only if you want pricier,
# more representative answers.
LLM_CLAUDE_MODEL = env("LLM_CLAUDE_MODEL", default="")
LLM_EXTRACTION_MODEL = env("LLM_EXTRACTION_MODEL", default="")
# DeepSeek is used ONLY for offline tooling: prompt synthesis,
# auto-templating raw user text, and on-demand smoke tests. It is NEVER
# used to answer real audit prompts (those still go through the four
# canonical providers: claude, gpt4, gemini, perplexity). The key may be
# empty — every code path that uses DeepSeek must fall back gracefully.
DEEPSEEK_API_KEY = env("DEEPSEEK_API_KEY", default="")
PROMPT_SYNTHESIS_PROVIDER = env(
    "PROMPT_SYNTHESIS_PROVIDER",
    default=("deepseek" if env("DEEPSEEK_API_KEY", default="") else "anthropic"),
)
# Google Programmable Search (Custom Search JSON API): competitor discovery
# and run enrichment in apps.llm_ranking.
GOOGLE_SEARCH_API_KEY = env("GOOGLE_SEARCH_API_KEY", default="")
GOOGLE_SEARCH_ENGINE_ID = env("GOOGLE_SEARCH_ENGINE_ID", default="")
# Master paywall switch. When False, authenticated users are never
# routed to /paywall and every plan-entitlement check resolves to the
# top tier, so the full app is open regardless of subscription state.
# Set PAYWALL_ENABLED=True in the env file to turn billing gates back
# on; no code change needed.
PAYWALL_ENABLED = env.bool("PAYWALL_ENABLED", default=False)

# ── Internal admin ops API ──
# Shared secret for the ftb-min admin server's internal ops API
# (/api/v1/internal/admin/). Empty = the surface is disabled (all 404s).
# Only the admin server may hold this; it must never reach a browser.
ADMIN_OPS_KEY = env("ADMIN_OPS_KEY", default="")
# CIDRs whose SOURCE address (REMOTE_ADDR, never a forwarded header) may
# call the internal ops surface. Empty = deny everyone, key or not.
# Production: exactly the admin server's address, e.g. ["10.0.1.7/32"].
ADMIN_OPS_ALLOWED_CIDRS = env.list("ADMIN_OPS_ALLOWED_CIDRS", default=[])

# ── Polar.sh billing and usage metering (apps.billing, apps.metering) ──
# Every recorded AI call is mirrored to Polar as an immutable `llm_usage`
# event via a transactional outbox; checkout and subscriptions run on
# Polar too.
POLAR_ACCESS_TOKEN = env("POLAR_ACCESS_TOKEN", default="")
POLAR_ENVIRONMENT = env("POLAR_ENVIRONMENT", default="sandbox")  # sandbox | production
# Meter ids from the Polar dashboard (or `manage.py polar_bootstrap`).
POLAR_METER_TOKENS_ID = env("POLAR_METER_TOKENS_ID", default="")
POLAR_METER_SPEND_ID = env("POLAR_METER_SPEND_ID", default="")
# celery: outbox rows flushed by the beat sweeper + on_commit fast path.
# inline: flush synchronously after commit (dev without celery workers).
# off:    no outbox rows are written at all.
POLAR_INGEST_MODE = env(
    "POLAR_INGEST_MODE",
    default=("celery" if env("POLAR_ACCESS_TOKEN", default="") else "off"),
)
# Usage reads (Settings/Billing pages) come from Polar meter quantities
# only after ingestion parity is verified (`manage.py polar_parity_check`);
# until then, and whenever Polar is unreachable, reads fall back to the
# local AITokenUsage ledger.
POLAR_READS_ENABLED = env.bool("POLAR_READS_ENABLED", default=False)
# Billing (Phase 2): product ids from `manage.py polar_billing_bootstrap`
# and the webhook endpoint's signing secret (from the Polar dashboard's
# webhook settings; empty disables signature verification failures into
# hard 503s — the endpoint refuses to process unsigned payloads).
POLAR_PRODUCT_PRO_MONTHLY_ID = env("POLAR_PRODUCT_PRO_MONTHLY_ID", default="")
POLAR_PRODUCT_PRO_ANNUAL_ID = env("POLAR_PRODUCT_PRO_ANNUAL_ID", default="")
# The two self-serve plans (100 and 200 tracked prompts, billed monthly, no
# trial): product ids the owner creates in Polar. Checkout and plan changes
# stay off until both are set. Keep the Pro ids above while any Pro
# subscription exists: a product no setting names syncs as cancelled.
POLAR_PRODUCT_PROMPTS_100_ID = env("POLAR_PRODUCT_PROMPTS_100_ID", default="")
POLAR_PRODUCT_PROMPTS_200_ID = env("POLAR_PRODUCT_PROMPTS_200_ID", default="")
POLAR_WEBHOOK_SECRET = env("POLAR_WEBHOOK_SECRET", default="")
# Canonical event name meters filter on. Never change once events flow.
POLAR_EVENT_NAME = "llm_usage"
# Local ledger retention. Pruning only runs once POLAR_READS_ENABLED is on;
# the floor protects the audit-cost preflight's historical averages.
AI_USAGE_RETENTION_DAYS = env.int("AI_USAGE_RETENTION_DAYS", default=400)

# ── Data retention ──────────────────────────────────────────────────
# Single source of truth for how long each table is kept. These numbers are
# published to customers on /what-we-track, so changing one here means
# changing the copy there too — they are a representation, not a default.
#
# Enforced by apps.analytics.tasks.prune_analytics_events (beat: nightly).
# Before this existed the 180-day figure was only a READ clamp in
# analytics_views.EVENT_LOG_RETENTION_DAYS; rows were never actually deleted.
ANALYTICS_RETENTION_DAYS = env.int("ANALYTICS_RETENTION_DAYS", default=180)
# Which account read which website's analytics, and when. Shorter window than
# the analytics itself: it is a security trail, not product data. Holds no
# personal data of its own -- migration analytics/0013 dropped the
# denormalised email/IP/user-agent columns, so identity is the user FK alone.
ACCESS_LOG_RETENTION_DAYS = env.int("ACCESS_LOG_RETENTION_DAYS", default=90)
# Stored LLM answers and their extracted citations. Long by design — the
# verbatim record of what a model said on a given date is a product feature —
# but bounded, so storage limitation is satisfiable.
LLM_RESULT_RETENTION_DAYS = env.int("LLM_RESULT_RETENTION_DAYS", default=730)
# Brand-security findings history.
SAFETY_ALERT_RETENTION_DAYS = env.int("SAFETY_ALERT_RETENTION_DAYS", default=730)

# Hard ceiling for Google-provider rows when Gemini runs in grounded mode.
# Gemini's additional terms permit storing the text of Grounded Results for
# thirty days only (and exclude the links entirely), so LLM_RESULT_RETENTION_DAYS
# must not apply to those rows. See effective_llm_result_retention_days().
GROUNDED_RESULT_MAX_RETENTION_DAYS = 30

# ── Ask Cansee assistant ──
# The header side-panel chat plus (eventually) the Slack/Discord ask
# path. The kill switch exists so a misbehaving provider can be taken
# out of rotation without a deploy; the message is what users see.
ASSISTANT_ENABLED = env.bool("ASSISTANT_ENABLED", default=True)
ASSISTANT_MAINTENANCE_MESSAGE = env(
    "ASSISTANT_MAINTENANCE_MESSAGE",
    default="Ask Cansee is temporarily unavailable.",
)
# Ingest each completed prompt run into the assistant's knowledge base
# (apps.assistant.tasks.ingest_audit_knowledge).
ASSISTANT_KNOWLEDGE_INGEST_ENABLED = env.bool("ASSISTANT_KNOWLEDGE_INGEST_ENABLED", default=True)

# ── Agent workspace (apps.agent) ──
# The Claude chat agent for SEO and GEO research. Fails closed: nothing
# runs until AGENT_ENABLED is set. Runs execute only in the agent ASGI
# process (AGENT_PROCESS); gunicorn workers answer the live endpoints with
# 503 agent_unavailable.
AGENT_ENABLED = env.bool("AGENT_ENABLED", default=False)
AGENT_PROCESS = env.bool("AGENT_PROCESS", default=False)
# "thread": a process-wide executor runs each turn. "inline": the request
# runs it to completion (tests).
AGENT_RUN_MODE = env("AGENT_RUN_MODE", default="thread")
AGENT_MODEL = env("AGENT_MODEL", default="claude-opus-5-5")
AGENT_EFFORT = env("AGENT_EFFORT", default="medium")
AGENT_SUBAGENT_EFFORT = env("AGENT_SUBAGENT_EFFORT", default="medium")
AGENT_TITLE_EFFORT = env("AGENT_TITLE_EFFORT", default="low")
# max_tokens per request: the orchestrator, then each sub-agent.
AGENT_MAX_TOKENS = env.int("AGENT_MAX_TOKENS", default=32000)
AGENT_LANDING_MAX_TOKENS = env.int("AGENT_LANDING_MAX_TOKENS", default=64000)
AGENT_ARTICLE_MAX_TOKENS = env.int("AGENT_ARTICLE_MAX_TOKENS", default=32000)
AGENT_SHEET_MAX_TOKENS = env.int("AGENT_SHEET_MAX_TOKENS", default=16000)
# Per-run ceilings: API requests, wall clock, model cost.
AGENT_MAX_REQUESTS_PER_RUN = env.int("AGENT_MAX_REQUESTS_PER_RUN", default=24)
AGENT_RUN_MAX_SECONDS = env.int("AGENT_RUN_MAX_SECONDS", default=1200)
AGENT_RUN_MAX_COST_USD = env.float("AGENT_RUN_MAX_COST_USD", default=3.0)
# Global executor size, and active runs allowed per user.
AGENT_MAX_CONCURRENT_RUNS = env.int("AGENT_MAX_CONCURRENT_RUNS", default=4)
AGENT_USER_ACTIVE_RUNS = env.int("AGENT_USER_ACTIVE_RUNS", default=1)
# Prompt-cache TTL ("5m" or "1h") for the system prompt and the history.
AGENT_CACHE_TTL = env("AGENT_CACHE_TTL", default="1h")
# Server-side refusal fallback (beta server-side-fallback-2026-07-01).
AGENT_FALLBACKS_ENABLED = env.bool("AGENT_FALLBACKS_ENABLED", default=True)
# Anthropic's server-side web_search / web_fetch tools.
AGENT_WEB_TOOLS_ENABLED = env.bool("AGENT_WEB_TOOLS_ENABLED", default=True)
AGENT_WEB_SEARCH_MAX_USES = env.int("AGENT_WEB_SEARCH_MAX_USES", default=5)
AGENT_WEB_FETCH_MAX_USES = env.int("AGENT_WEB_FETCH_MAX_USES", default=5)
# Google results pages captured per query (SerpAPI bills per page).
AGENT_SERP_MAX_PAGES = env.int("AGENT_SERP_MAX_PAGES", default=5)
# Page fetches per user per day through the browse endpoint.
AGENT_BROWSE_DAILY_LIMIT = env.int("AGENT_BROWSE_DAILY_LIMIT", default=300)
# An event stream closes after AGENT_STREAM_MAX_SECONDS (the client
# reconnects). Runs heartbeat every AGENT_HEARTBEAT_SECONDS and are marked
# interrupted after AGENT_STALE_SECONDS without one.
AGENT_STREAM_MAX_SECONDS = env.int("AGENT_STREAM_MAX_SECONDS", default=300)
AGENT_HEARTBEAT_SECONDS = env.int("AGENT_HEARTBEAT_SECONDS", default=10)
AGENT_STALE_SECONDS = env.int("AGENT_STALE_SECONDS", default=90)
# Days a chat is kept after its last message; 0 keeps chats.
AGENT_CHAT_RETENTION_DAYS = env.int("AGENT_CHAT_RETENTION_DAYS", default=0)
# The research agents (research_swarm): one tool call runs up to
# AGENT_SWARM_MAX_TASKS tasks, AGENT_SWARM_WORKERS at a time in one swarm
# and AGENT_SWARM_PROCESS_WORKERS across the agent process, with limits per
# provider on Google results, page fetches, engine requests (per engine)
# and analysis requests. A swarm stops after AGENT_SWARM_MAX_SECONDS.
AGENT_SWARM_ENABLED = env.bool("AGENT_SWARM_ENABLED", default=True)
AGENT_SWARM_MAX_TASKS = env.int("AGENT_SWARM_MAX_TASKS", default=30)
AGENT_SWARM_WORKERS = env.int("AGENT_SWARM_WORKERS", default=6)
AGENT_SWARM_PROCESS_WORKERS = env.int("AGENT_SWARM_PROCESS_WORKERS", default=12)
AGENT_SWARM_SERP_CONCURRENCY = env.int("AGENT_SWARM_SERP_CONCURRENCY", default=5)
AGENT_SWARM_BROWSE_CONCURRENCY = env.int("AGENT_SWARM_BROWSE_CONCURRENCY", default=6)
AGENT_SWARM_ENGINE_CONCURRENCY = env.int("AGENT_SWARM_ENGINE_CONCURRENCY", default=3)
AGENT_SWARM_HAIKU_CONCURRENCY = env.int("AGENT_SWARM_HAIKU_CONCURRENCY", default=8)
AGENT_SWARM_MAX_SECONDS = env.int("AGENT_SWARM_MAX_SECONDS", default=420)
# The research agents' analysis model: structured output with thinking
# off, which the model accepts at effort low, medium or high.
AGENT_WORKER_MODEL = env("AGENT_WORKER_MODEL", default="claude-haiku-5-5")
AGENT_WORKER_EFFORT = env("AGENT_WORKER_EFFORT", default="low")
AGENT_WORKER_MAX_TOKENS = env.int("AGENT_WORKER_MAX_TOKENS", default=2048)
# max_tokens per request of the six writing agents.
AGENT_OPTIMIZER_MAX_TOKENS = env.int("AGENT_OPTIMIZER_MAX_TOKENS", default=24000)
AGENT_COMPARISON_MAX_TOKENS = env.int("AGENT_COMPARISON_MAX_TOKENS", default=24000)
AGENT_SOCIAL_MAX_TOKENS = env.int("AGENT_SOCIAL_MAX_TOKENS", default=8000)
AGENT_PR_MAX_TOKENS = env.int("AGENT_PR_MAX_TOKENS", default=16000)
AGENT_LLMS_TXT_MAX_TOKENS = env.int("AGENT_LLMS_TXT_MAX_TOKENS", default=8000)
AGENT_MESSAGING_MAX_TOKENS = env.int("AGENT_MESSAGING_MAX_TOKENS", default=16000)
# Pages a writer reads, and third-party pages a correction request reads.
AGENT_WRITER_MAX_PAGES = env.int("AGENT_WRITER_MAX_PAGES", default=5)
AGENT_CORRECTION_MAX_PAGES = env.int("AGENT_CORRECTION_MAX_PAGES", default=5)
# Emails for review requests and decisions.
AGENT_REVIEW_EMAILS = env.bool("AGENT_REVIEW_EMAILS", default=True)
# Build from my site (run kind context_build): pages read, cited pages
# re-checked, builds per website per UTC day, the estimate bound per request
# and the output cap per request.
CONTEXT_BUILD_MAX_PAGES = env.int("CONTEXT_BUILD_MAX_PAGES", default=8)
CONTEXT_CHECK_MAX_PAGES = env.int("CONTEXT_CHECK_MAX_PAGES", default=20)
CONTEXT_BUILD_DAILY_RUNS = env.int("CONTEXT_BUILD_DAILY_RUNS", default=5)
CONTEXT_BUILD_USD_PER_REQUEST = env("CONTEXT_BUILD_USD_PER_REQUEST", default="0.0100")
CONTEXT_BUILD_MAX_TOKENS = env.int("CONTEXT_BUILD_MAX_TOKENS", default=4000)

# ── RAG vector index (optional) ──
# "python" (default): retriever.py scores every (user, website) chunk in
# a Python loop - the original behaviour, no extra dependencies.
# "chroma": embedded ChromaDB index at RAG_CHROMA_PATH. Local/dev
# evaluation only for now: web and celery in prod are separate
# containers without a shared filesystem, and Chroma's PersistentClient
# is single-process. See apps/rag/services/vector_backends.py.
RAG_VECTOR_BACKEND = env("RAG_VECTOR_BACKEND", default="python")
RAG_CHROMA_PATH = env("RAG_CHROMA_PATH", default=str(BASE_DIR / ".chroma"))
# Set to e.g. http://chroma:8000 to use a Chroma SERVER instead of the
# embedded client. Required for any topology where more than one process
# touches the index (prod: web + celery are separate containers).
RAG_CHROMA_URL = env("RAG_CHROMA_URL", default="")

# Build identity of the running backend, surfaced by /api/v1/version/.
# Baked into the Docker image at deploy time via build args (see
# docker/Dockerfile and scripts/deploy.sh). All empty in local dev.
GIT_SHA = env("GIT_SHA", default="")
BUILD_NUMBER = env("BUILD_NUMBER", default="")
BUILD_TIME = env("BUILD_TIME", default="")

# Internal HTTP services (Docker-network only, never exposed via nginx).
# Empty URLs mean the Django facades run the shared logic in-process,
# which is the dev/test/CI default. See docs/ARCHITECTURE.md.
INTELLIGENCE_SERVICE_URL = env("INTELLIGENCE_SERVICE_URL", default="")
INTELLIGENCE_AUTH_TOKEN = env("INTELLIGENCE_AUTH_TOKEN", default="")
SOURCES_SERVICE_URL = env("SOURCES_SERVICE_URL", default="")
SOURCES_AUTH_TOKEN = env("SOURCES_AUTH_TOKEN", default="")

GOOGLE_OAUTH_CLIENT_ID = env("GOOGLE_OAUTH_CLIENT_ID", default="")
GOOGLE_OAUTH_CLIENT_SECRET = env("GOOGLE_OAUTH_CLIENT_SECRET", default="")
MICROSOFT_OAUTH_CLIENT_ID = env("MICROSOFT_OAUTH_CLIENT_ID", default="")
MICROSOFT_OAUTH_CLIENT_SECRET = env("MICROSOFT_OAUTH_CLIENT_SECRET", default="")
# ── BUSINESS / ORGANIZATION FEATURES MASTER SWITCH ──
# One flag gates every B2B surface: the /api/v1/orgs/ API (members,
# invitations, domains, usage, enforcement), invitation accept/register,
# SSO discovery (/auth/sso/start/), the Microsoft and SAML sign-in lanes,
# Google JIT/invite provisioning, and the org block in the session
# payload. FAIL-CLOSED: default False, so production stays pure B2C with
# zero config until launch day — flipping it on is one env var + restart.
# Dev (.env) and tests set it True. Google/password B2C login is never
# affected. The ops command `manage.py create_org` still works while off,
# so a customer can be staged before the flip.
ORG_FEATURES_ENABLED = env.bool("ORG_FEATURES_ENABLED", default=False)

# SAML bridge (the Okta/enterprise-IdP lane). The bridge terminates the
# customer's SAML and hands us a verified profile; we keep minting our own
# tokens. Two interchangeable providers behind one seam:
#   ssoready — MIT open source, free unlimited cloud (the default)
#   workos   — $125/connection/month, hosted admin portal for customer IT
# Dormant until the active provider's keys are set; an org additionally
# needs Organization.sso_connection_id before the "saml" button is offered.
SSO_BRIDGE_PROVIDER = env("SSO_BRIDGE_PROVIDER", default="ssoready")
SSOREADY_API_KEY = env("SSOREADY_API_KEY", default="")
WORKOS_API_KEY = env("WORKOS_API_KEY", default="")
WORKOS_CLIENT_ID = env("WORKOS_CLIENT_ID", default="")
# Public origin of THIS API service. The WorkOS callback is an API route
# (a browser redirect target), so it cannot be derived from FRONTEND_URL,
# which is the SPA's origin.
BACKEND_PUBLIC_URL = env("BACKEND_PUBLIC_URL", default="http://localhost:8000")
SENTRY_DSN = env("SENTRY_DSN", default="")
SERPAPI_KEY = env("SERPAPI_KEY", default="")
# ScraperAPI's structured Google search: the other Google results provider.
# With both keys set SerpAPI is used, unless SERP_PROVIDER ("serpapi" or
# "scraperapi") pins one.
SCRAPERAPI_KEY = env("SCRAPERAPI_KEY", default="")
SERP_PROVIDER = env("SERP_PROVIDER", default="")

# ── Search vs AI gap (apps.search_gap) ─────────────────────────────────
# Google rank per tracked prompt, via SerpAPI. A check pages 10 results at
# a time and stops at the site, so MAX_DEPTH=100 costs at most 10 calls.
SEARCH_GAP_MAX_DEPTH = env.int("SEARCH_GAP_MAX_DEPTH", default=100)
SEARCH_GAP_RECHECK_HOURS = env.int("SEARCH_GAP_RECHECK_HOURS", default=24)
# Manual rechecks per website in a rolling 24 hours: room for the user's own
# checks after a 200-prompt Autopilot run (which uses its own quotas).
SEARCH_GAP_DAILY_CHECKS_PER_WEBSITE = env.int("SEARCH_GAP_DAILY_CHECKS_PER_WEBSITE", default=250)
SERPAPI_COST_PER_SEARCH_USD = env.float("SERPAPI_COST_PER_SEARCH_USD", default=0.015)
# ScraperAPI bills credits from a monthly allowance: set your plan's dollar
# cost per Google search to record it (0 records nothing, as on the free plan).
SCRAPERAPI_COST_PER_SEARCH_USD = env.float("SCRAPERAPI_COST_PER_SEARCH_USD", default=0.0)
# Countries the ScraperAPI plan can search from: ISO codes ("us", the
# default, works on every plan) or "all" on plans with country targeting.
# Other countries search from the US with Google's country setting.
SCRAPERAPI_GEO_COUNTRIES = env("SCRAPERAPI_GEO_COUNTRIES", default="us")
# API credits per structured Google request (ScraperAPI docs).
SCRAPERAPI_CREDITS_PER_SEARCH = env.int("SCRAPERAPI_CREDITS_PER_SEARCH", default=25)
# Onboarding scan: Google searches for competitors and buyer questions. The
# scan is synchronous, so each search gets a short timeout (ScraperAPI's own
# default is 70s) and the scan carries on without Google when it runs out.
ONBOARDING_SERP_TIMEOUT = env.float("ONBOARDING_SERP_TIMEOUT", default=20.0)
# Google searches per user per UTC day across onboarding scans (up to 3 a
# scan), on top of the scan endpoint's own rate limit.
ONBOARDING_SERP_DAILY_SEARCHES = env.int("ONBOARDING_SERP_DAILY_SEARCHES", default=15)

# Country checks: Google and the AI engines for tracked prompts in several
# countries, run on demand after the user confirms the estimate.
COUNTRY_CHECKS_ENABLED = env.bool("COUNTRY_CHECKS_ENABLED", default=True)
COUNTRY_CHECK_MAX_COUNTRIES = env.int("COUNTRY_CHECK_MAX_COUNTRIES", default=5)
COUNTRY_CHECK_MAX_PROMPTS = env.int("COUNTRY_CHECK_MAX_PROMPTS", default=10)
# Google results pages per prompt and country.
COUNTRY_CHECK_MAX_PAGES = env.int("COUNTRY_CHECK_MAX_PAGES", default=2)
COUNTRY_CHECK_MAX_GOOGLE_PAGES = env.int("COUNTRY_CHECK_MAX_GOOGLE_PAGES", default=50)
COUNTRY_CHECK_MAX_ENGINE_ANSWERS = env.int("COUNTRY_CHECK_MAX_ENGINE_ANSWERS", default=100)
# Google pages per website per UTC day.
COUNTRY_CHECK_DAILY_GOOGLE_PAGES = env.int("COUNTRY_CHECK_DAILY_GOOGLE_PAGES", default=100)
COUNTRY_CHECK_SERP_CONCURRENCY = env.int("COUNTRY_CHECK_SERP_CONCURRENCY", default=2)
COUNTRY_CHECK_ENGINE_CONCURRENCY = env.int("COUNTRY_CHECK_ENGINE_CONCURRENCY", default=3)
# auto | celery | thread | inline. auto runs one Celery task per country on
# the integrations queue, or a background thread when Celery runs eagerly.
COUNTRY_CHECK_RUN_MODE = env("COUNTRY_CHECK_RUN_MODE", default="auto")
# A queued or running check with no heartbeat for this long is interrupted.
COUNTRY_CHECK_STALE_MINUTES = env.int("COUNTRY_CHECK_STALE_MINUTES", default=20)

# ── Insights (apps.insights) ───────────────────────────────────────────
# Answer analysis, crawler audits, prompt research, fan-out Google checks
# and shopping checks. Every paid run is estimated and confirmed first.
INSIGHTS_ENABLED = env.bool("INSIGHTS_ENABLED", default=True)
# auto | celery | thread | inline. auto runs a Celery task on the kind's
# queue, or a background thread when Celery runs eagerly.
INSIGHTS_RUN_MODE = env("INSIGHTS_RUN_MODE", default="auto")
# A queued or running run with no heartbeat for this long is interrupted.
INSIGHTS_RUN_STALE_MINUTES = env.int("INSIGHTS_RUN_STALE_MINUTES", default=20)
# Google pages per website per UTC day, all insights kinds together.
INSIGHTS_DAILY_GOOGLE_PAGES = env.int("INSIGHTS_DAILY_GOOGLE_PAGES", default=60)
# Answers per analysis run, the estimate bound per answer (two Claude Haiku
# 5.5 requests; typical about $0.002), answers analyzed at once and the
# output cap per analysis request.
INSIGHTS_ANALYSIS_MAX_ANSWERS = env.int("INSIGHTS_ANALYSIS_MAX_ANSWERS", default=60)
INSIGHTS_ANALYSIS_USD_PER_ANSWER = env("INSIGHTS_ANALYSIS_USD_PER_ANSWER", default="0.0060")
INSIGHTS_ANALYSIS_CONCURRENCY = env.int("INSIGHTS_ANALYSIS_CONCURRENCY", default=3)
INSIGHTS_ANALYSIS_MAX_TOKENS = env.int("INSIGHTS_ANALYSIS_MAX_TOKENS", default=3000)
# Own pages read as evidence for claim checks, and how long a read is reused.
INSIGHTS_EVIDENCE_PAGES = env.int("INSIGHTS_EVIDENCE_PAGES", default=5)
INSIGHTS_EVIDENCE_TTL_HOURS = env.int("INSIGHTS_EVIDENCE_TTL_HOURS", default=24)
# Google reads per research run, and the estimate bound of one suggestion request.
INSIGHTS_RESEARCH_MAX_GOOGLE_PROMPTS = env.int("INSIGHTS_RESEARCH_MAX_GOOGLE_PROMPTS", default=5)
INSIGHTS_SUGGESTIONS_USD = env("INSIGHTS_SUGGESTIONS_USD", default="0.0100")
# Searches per fan-out Google check.
INSIGHTS_FANOUT_MAX_QUERIES = env.int("INSIGHTS_FANOUT_MAX_QUERIES", default=10)
# Crawler audit: own pages per audit, the text_without_js threshold, the
# pause between audits, audits per website per UTC day and fetches at once
# (the browse parser reads 2 pages at a time per process).
SITE_AUDIT_MAX_PAGES = env.int("SITE_AUDIT_MAX_PAGES", default=20)
SITE_AUDIT_MIN_WORDS = env.int("SITE_AUDIT_MIN_WORDS", default=150)
SITE_AUDIT_COOLDOWN_MINUTES = env.int("SITE_AUDIT_COOLDOWN_MINUTES", default=10)
SITE_AUDIT_DAILY_RUNS = env.int("SITE_AUDIT_DAILY_RUNS", default=10)
SITE_AUDIT_CONCURRENCY = env.int("SITE_AUDIT_CONCURRENCY", default=2)
# Products: active tracked products per website, products per shopping check
# and engine answers per shopping check.
SHOPPING_MAX_PRODUCTS = env.int("SHOPPING_MAX_PRODUCTS", default=10)
SHOPPING_MAX_PRODUCTS_PER_RUN = env.int("SHOPPING_MAX_PRODUCTS_PER_RUN", default=5)
SHOPPING_MAX_ENGINE_ANSWERS = env.int("SHOPPING_MAX_ENGINE_ANSWERS", default=30)
# ScraperAPI credits per Google Shopping page (its Google-domain rate; confirm
# against the account balance before and after one check).
SCRAPERAPI_SHOPPING_CREDITS = env.int("SCRAPERAPI_SHOPPING_CREDITS", default=25)

# ── Ask AI (apps.prompt_box) ───────────────────────────────────────────
# One buyer question asked several times per engine. Answers per engine per
# question, answers per project per UTC day, answers asked at once in one
# run, how runs execute (auto | celery | thread | inline, as
# INSIGHTS_RUN_MODE) and how long a run may go without a heartbeat before
# it is interrupted.
PROMPT_BOX_MAX_SAMPLES = env.int("PROMPT_BOX_MAX_SAMPLES", default=10)
PROMPT_BOX_DAILY_ANSWERS = env.int("PROMPT_BOX_DAILY_ANSWERS", default=100)
PROMPT_BOX_CONCURRENCY = env.int("PROMPT_BOX_CONCURRENCY", default=3)
PROMPT_BOX_RUN_MODE = env("PROMPT_BOX_RUN_MODE", default="auto")
PROMPT_BOX_STALE_MINUTES = env.int("PROMPT_BOX_STALE_MINUTES", default=10)

# ── Autopilot (apps.autopilot) ─────────────────────────────────────────
# Once a day per project: every tracked prompt within the plan is asked to
# each configured engine, Google page one is read, new answers that name the
# brand are analyzed, then one text is sent. AUTOPILOT_ENABLED is the
# deployment switch (off: the dispatcher starts nothing). Run modes as
# above; the management command always runs inline.
AUTOPILOT_ENABLED = env.bool("AUTOPILOT_ENABLED", default=True)
AUTOPILOT_RUN_MODE = env("AUTOPILOT_RUN_MODE", default="auto")
# Runs in progress at once across every project (manual runs included), and
# prompts asked at once in one run.
AUTOPILOT_MAX_ACTIVE_RUNS = env.int("AUTOPILOT_MAX_ACTIVE_RUNS", default=2)
AUTOPILOT_PROMPT_CONCURRENCY = env.int("AUTOPILOT_PROMPT_CONCURRENCY", default=3)
# The engines and Google steps stop claiming prompts after this many seconds
# and hand over to a continuation (their soft time limit is this plus 360 s,
# room for the prompts in flight). A run with no heartbeat this long is
# interrupted.
AUTOPILOT_STEP_SECONDS = env.int("AUTOPILOT_STEP_SECONDS", default=1200)
AUTOPILOT_STALE_MINUTES = env.int("AUTOPILOT_STALE_MINUTES", default=30)
# Share of the owner's monthly AI allowance Autopilot never spends: it is
# kept for the user's own questions.
AUTOPILOT_RESERVE_PCT = env.int("AUTOPILOT_RESERVE_PCT", default=10)
# Google page one. The cadence a project's Autopilot starts with ("daily" or
# "weekly"; weekly is the cheap default until the owner decides), how old a
# successful check may be and still be reused on a daily and on a weekly
# cadence, pages per project and per deployment per UTC day (25 ScraperAPI
# credits a page) and requests at once in one run.
AUTOPILOT_GOOGLE_CADENCE = env("AUTOPILOT_GOOGLE_CADENCE", default="weekly")
AUTOPILOT_GOOGLE_REUSE_HOURS = env.int("AUTOPILOT_GOOGLE_REUSE_HOURS", default=20)
AUTOPILOT_GOOGLE_WEEKLY_REUSE_HOURS = env.int("AUTOPILOT_GOOGLE_WEEKLY_REUSE_HOURS", default=164)
AUTOPILOT_GOOGLE_PAGES_PER_PROJECT = env.int("AUTOPILOT_GOOGLE_PAGES_PER_PROJECT", default=200)
AUTOPILOT_GOOGLE_PAGES_PER_DAY = env.int("AUTOPILOT_GOOGLE_PAGES_PER_DAY", default=1000)
AUTOPILOT_GOOGLE_CONCURRENCY = env.int("AUTOPILOT_GOOGLE_CONCURRENCY", default=2)
# How long the text waits for the analysis run, and the answers a prompt
# needs in 7 days before it is ranked.
AUTOPILOT_ANALYSIS_WAIT_MINUTES = env.int("AUTOPILOT_ANALYSIS_WAIT_MINUTES", default=15)
AUTOPILOT_MIN_RANK_ANSWERS = env.int("AUTOPILOT_MIN_RANK_ANSWERS", default=4)

# ── SMS (Twilio) ───────────────────────────────────────────────────────
# The most intrusive channel in the product, so it is off by default and
# has to be turned on deliberately per environment. Off means nothing
# sends and the inbound webhook accepts-and-ignores.
SMS_ENABLED = env.bool("SMS_ENABLED", default=False)
TWILIO_ACCOUNT_SID = env("TWILIO_ACCOUNT_SID", default="")
TWILIO_AUTH_TOKEN = env("TWILIO_AUTH_TOKEN", default="")
TWILIO_FROM_NUMBER = env("TWILIO_FROM_NUMBER", default="")
# Twilio signs the URL it was configured with. Behind nginx/Cloudflare the
# scheme Django reconstructs can differ from what Twilio signed, which
# breaks validation -- set this to the exact configured webhook URL.
TWILIO_WEBHOOK_URL = env("TWILIO_WEBHOOK_URL", default="")

# Brand Research community lane (Reddit search). Unauthenticated public
# endpoint, so it has no key to gate on and can be rate-limited or blocked
# without warning. This is the operator switch for turning it off without a
# deploy; the lane then reports as skipped rather than failing the scan.
BRAND_RESEARCH_COMMUNITY_ENABLED = env.bool("BRAND_RESEARCH_COMMUNITY_ENABLED", default=True)

# Brand Research AI-engine lane. One LLM call per configured provider per
# scan, so this is the most expensive part of a scan and the first thing to
# turn off under cost pressure. Off means the lane reports as skipped.
BRAND_RESEARCH_ENGINES_ENABLED = env.bool("BRAND_RESEARCH_ENGINES_ENABLED", default=True)

# ── Chat bots ──
# Slack app (events + slash commands + chat.postMessage replies)
SLACK_SIGNING_SECRET = env("SLACK_SIGNING_SECRET", default="")
SLACK_BOT_TOKEN = env("SLACK_BOT_TOKEN", default="")
# Discord app (interactions endpoint + follow-ups + command registration)
DISCORD_PUBLIC_KEY = env("DISCORD_PUBLIC_KEY", default="")
DISCORD_APPLICATION_ID = env("DISCORD_APPLICATION_ID", default="")
DISCORD_BOT_TOKEN = env("DISCORD_BOT_TOKEN", default="")

# ── AI engines (besides OPENAI_API_KEY / ANTHROPIC_API_KEY above) ──
GEMINI_API_KEY = env("GEMINI_API_KEY", default="")
PERPLEXITY_API_KEY = env("PERPLEXITY_API_KEY", default="")
XAI_API_KEY = env("XAI_API_KEY", default="")

# When True, web-search-capable providers (Claude, GPT, Gemini, Grok) run
# with their web-search tool enabled and pass the audit region as the search
# user_location, so non-Perplexity models also answer as a local user.
# Off by default because web-grounded calls cost more and are slower.
LLM_WEBSEARCH_ENABLED = env.bool("LLM_WEBSEARCH_ENABLED", default=False)

# Claude-clustered sentiment themes in apps.llm_ranking.services.geo_deep_dive.
GEO_SENTIMENT_THEMES_ENABLED = env.bool("GEO_SENTIMENT_THEMES_ENABLED", default=True)

# Finite monthly AI-spend safety ceiling (USD) for enterprise / custom-priced
# accounts, which have no price to derive a cap from. Prevents the
# ENTERPRISE-default Organization plan from leaving org users effectively
# uncapped (core.ai_tracking.effective_ai_cap). A per-account
# monthly_ai_cost_cap_usd overrides this; set to 0 for truly unlimited.
AI_ENTERPRISE_MONTHLY_CAP_USD = env.float("AI_ENTERPRISE_MONTHLY_CAP_USD", default=500.0)
# Monthly AI allowance for accounts WITHOUT an active/trialing
# subscription (the Free plan). A small customer-acquisition budget —
# enough to run onboarding and a few scans, not enough to burn margin.
AI_FREE_MONTHLY_CAP_USD = env.float("AI_FREE_MONTHLY_CAP_USD", default=1.0)

# ── Google Search Console (apps.search_console) ──
# OAuth reuses GOOGLE_OAUTH_CLIENT_ID/SECRET via the integrations
# registry ("gsc" entry). The redirect URI below must be listed in the
# Google Cloud OAuth client's authorized redirect URIs.
FRONTEND_URL = env("FRONTEND_URL", default="http://localhost:5173")
GSC_OAUTH_REDIRECT_URI = env(
    "GSC_OAUTH_REDIRECT_URI",
    default="http://localhost:8000/api/v1/search-console/oauth/callback/",
)
# Max rows kept per dimension per sync run (query/page dimensions).
GSC_DAILY_ROW_LIMIT = env.int("GSC_DAILY_ROW_LIMIT", default=5000)
# First sync pulls this many days of history (ending 3 days ago, GSC lag).
GSC_BACKFILL_DAYS = env.int("GSC_BACKFILL_DAYS", default=28)
# Incremental syncs re-pull this window because Google revises late data.
GSC_SYNC_LOOKBACK_DAYS = env.int("GSC_SYNC_LOOKBACK_DAYS", default=7)
# Stored rows older than this are pruned nightly (GSC keeps 16 months).
GSC_RETENTION_DAYS = env.int("GSC_RETENTION_DAYS", default=480)
# Per-user daily cap on Search Console API calls.
GSC_DAILY_API_LIMIT_PER_USER = env.int("GSC_DAILY_API_LIMIT_PER_USER", default=2000)
# Prompt-library feed: minimum 28-day impressions for a query to qualify.
GSC_PROMPT_MIN_IMPRESSIONS = env.int("GSC_PROMPT_MIN_IMPRESSIONS", default=10)
# Prompt-library feed: max queries considered per website per sync.
GSC_PROMPT_TOP_N = env.int("GSC_PROMPT_TOP_N", default=50)

# ── Web Analytics external sources (apps.web_analytics) ──
# Read-only traffic sources connected per website: the client's own GA4
# property (OAuth via the "ga" registry entry, same Google client as GSC),
# a Cansee-owned GA4 pool property behind our hosted Google tag, and
# tenant-supplied Cloudflare zone tokens. Raw analytics rows are never
# stored — endpoints serve short-lived Redis snapshots (read-through,
# fetched only while a dashboard is open).
WEB_ANALYTICS_ENABLED = env.bool("WEB_ANALYTICS_ENABLED", default=True)
GA4_OAUTH_REDIRECT_URI = env(
    "GA4_OAUTH_REDIRECT_URI",
    default="http://localhost:8000/api/v1/web-analytics/ga4/oauth/callback/",
)
# Seconds a GA4 realtime snapshot is served from cache before re-fetching.
# At the frontend's 30s poll this costs at most ~480 API requests/hr per
# open dashboard vs Google's 40k tokens/hr/property realtime budget.
GA4_SNAPSHOT_TTL_SECONDS = env.int("GA4_SNAPSHOT_TTL_SECONDS", default=25)
# Hard cap on each upstream Google Analytics request (inline in a sync
# worker, so this bounds the worst-case request latency).
GA4_FETCH_TIMEOUT_SECONDS = env.float("GA4_FETCH_TIMEOUT_SECONDS", default=6.0)
# Per-website daily cap on Analytics Data/Admin API calls.
GA4_DAILY_API_LIMIT_PER_WEBSITE = env.int("GA4_DAILY_API_LIMIT_PER_WEBSITE", default=15000)
# Hosted Google tag: service-account key JSON (single line) with Editor
# access to the pool property below. Both unset = hosted source hidden.
GA4_SA_CREDENTIALS_JSON = env("GA4_SA_CREDENTIALS_JSON", default="")
GA4_HOSTED_PROPERTY_ID = env("GA4_HOSTED_PROPERTY_ID", default="")
# Cloudflare zone snapshots: edge data lags 1-5 minutes anyway, so a
# longer TTL; the GraphQL budget (~300 queries/5min) is never a factor.
CLOUDFLARE_SNAPSHOT_TTL_SECONDS = env.int("CLOUDFLARE_SNAPSHOT_TTL_SECONDS", default=120)
CLOUDFLARE_FETCH_TIMEOUT_SECONDS = env.float("CLOUDFLARE_FETCH_TIMEOUT_SECONDS", default=10.0)

# Beta gate — set SIGNUPS_ENABLED=true in env to re-enable public registration.
SIGNUPS_ENABLED = env.bool("SIGNUPS_ENABLED", default=False)

# ── ADMIN PATH ──
# Where Django admin is mounted. Defaults to "admin/" so development and the
# test suite are unchanged; production sets ADMIN_URL to an unguessable path.
#
# This is noise reduction, not access control. /admin/ is among the most
# scanned paths on the internet, and with django-axes locking an account
# after 5 failures, a bot spraying `admin` at the default path can lock out
# a real user. Moving it means the failed logins left in the log are worth
# investigating. It does NOT protect against anyone who learns the path.
#
# Normalised so ADMIN_URL=secret, /secret and secret/ all behave the same --
# Django's path() requires no leading slash and a trailing one.
ADMIN_URL = env("ADMIN_URL", default="admin/").strip().strip("/") + "/"

DEFAULT_FROM_EMAIL = env("SERVER_EMAIL", default="noreply@cansee.ai")

# ── LOGGING ──
LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "formatters": {
        "json": {
            "()": "core.logging.formatters.JSONFormatter",
        },
        "verbose": {
            "format": "{levelname} {asctime} {module} {message}",
            "style": "{",
        },
    },
    "handlers": {
        "console": {
            "class": "logging.StreamHandler",
            "formatter": "json",
        },
    },
    # Root catch-all so a logger name outside the named set below (new
    # module, third-party lib) is never silently dropped to
    # logging.lastResort. Named loggers set propagate=False so their
    # records don't also reach the root handler and print twice.
    "root": {"handlers": ["console"], "level": "WARNING"},
    "loggers": {
        "django": {"handlers": ["console"], "level": "WARNING", "propagate": False},
        "apps": {"handlers": ["console"], "level": "INFO", "propagate": False},
        "audit": {"handlers": ["console"], "level": "INFO", "propagate": False},
        "security": {"handlers": ["console"], "level": "INFO", "propagate": False},
        # Covers getLogger("billing") across apps/billing (Polar events
        # log at INFO; without this entry they were dropped).
        "billing": {"handlers": ["console"], "level": "INFO", "propagate": False},
        # Covers core.resilience (circuit-breaker state changes),
        # core.ai_tracking, and any other core.* module logger.
        "core": {"handlers": ["console"], "level": "INFO", "propagate": False},
    },
}

# ── DRF SPECTACULAR ──
SPECTACULAR_SETTINGS = {
    "TITLE": "Cansee API",
    "DESCRIPTION": "AI-powered growth intelligence platform API",
    "VERSION": "1.0.0",
    "SERVE_INCLUDE_SCHEMA": False,
}

# ── Analytics geo + proxy ──
# Number of trusted reverse proxies in front of the app (our nginx / CDN).
# get_client_ip reads the client hop from the right of X-Forwarded-For
# accordingly, so a client cannot spoof its IP by prepending an entry.
TRUSTED_PROXY_COUNT = env.int("TRUSTED_PROXY_COUNT", default=1)

# Request headers a trusted edge sets after resolving country itself.
# Cloudflare: CF-IPCountry (forwarded by nginx as CF-IPCountry ->
# HTTP_CF_IPCOUNTRY). An nginx geoip2 block can forward X-Geo-Country.
GEO_COUNTRY_HEADERS = ("HTTP_CF_IPCOUNTRY", "HTTP_X_GEO_COUNTRY")

# Absolute path to a MaxMind GeoLite2-Country.mmdb for in-process country
# lookups. Empty disables the local-DB layer (header + ip-api still work).
GEOIP_PATH = env("GEOIP_PATH", default="")

# Per-user daily cap on Perplexity Search API calls (Source Intelligence
# scans). Each scan consumes one search call plus one cheap-LLM
# extraction per readable result.
PERPLEXITY_SEARCH_DAILY_LIMIT_PER_USER = env.int(
    "PERPLEXITY_SEARCH_DAILY_LIMIT_PER_USER", default=200
)

# Yelp Fusion API (Source Intelligence). When set, Yelp URLs in scan
# results are read through the official API (ratings, review counts,
# excerpt reviews) instead of the bot-blocked HTML.
YELP_API_KEY = env("YELP_API_KEY", default="")

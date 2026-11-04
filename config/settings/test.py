from .base import *  # noqa

DEBUG = False
SECURE_SSL_REDIRECT = False

# The suite asserts the secure posture, so it must not inherit a developer's
# exported PUBLIC_SCHEME=http -- that would silently change what a dozen
# unrelated tests are checking. Tests that need plaintext behaviour use
# override_settings on the derived values instead. Mirrors the pinning of
# POLAR_ACCESS_TOKEN / RAG_VECTOR_BACKEND further down this file.
PUBLIC_SCHEME = "https"
SESSION_COOKIE_SECURE = True
CSRF_COOKIE_SECURE = True

DATABASES = {
    "default": {
        "ENGINE": "django.db.backends.postgresql",
        "NAME": "cansee_test",
        "USER": "postgres",
        "PASSWORD": "postgres",
        "HOST": "localhost",
        "PORT": "5432",
    }
}

CACHES = {
    "default": {
        "BACKEND": "django.core.cache.backends.locmem.LocMemCache",
    }
}

# Disable Celery tasks in tests - run synchronously
CELERY_TASK_ALWAYS_EAGER = True
CELERY_TASK_EAGER_PROPAGATES = True

# Disable throttling in tests
REST_FRAMEWORK["DEFAULT_THROTTLE_CLASSES"] = []  # noqa: F405

EMAIL_BACKEND = "django.core.mail.backends.locmem.EmailBackend"

PASSWORD_HASHERS = ["django.contrib.auth.hashers.MD5PasswordHasher"]

FIELD_ENCRYPTION_KEY = "YJGq9tGE_J3DT8L-Gg9KBgBBqChfPg1UYGU1cWTc_zI="

# The suite must never escalate findings to the Anthropic judge, even when
# the developer's shell exports a real ANTHROPIC_API_KEY. Tests that cover
# the escalation path re-enable this and mock the client.
BRAND_SECURITY_JUDGE_ENABLED = False

# Alignment scoring must never run eagerly inside unrelated suites
# (CELERY_TASK_ALWAYS_EAGER makes every .delay inline, so each created
# LLMRankingResult would trigger a scoring pass). Tests covering it
# re-enable the flag and inject fake vectors.
CLAIM_VERIFICATION_ENABLED = False

# Force in-process mode for the internal-service facades so exported
# shell variables can never flip the suite into HTTP mode.
INTELLIGENCE_SERVICE_URL = ""
SOURCES_SERVICE_URL = ""

# Brand Research discovery must never leave the process. SERPAPI_KEY and
# SCRAPERAPI_KEY come
# from .env on a developer machine, and the Reddit community lane has no key
# to blank -- without both of these the scan pipeline tests make real
# outbound calls. Tests covering these lanes mock the clients directly.
SERPAPI_KEY = ""
SCRAPERAPI_KEY = ""
SERP_PROVIDER = ""
BRAND_RESEARCH_COMMUNITY_ENABLED = False
# SMS must never leave the process in tests: a real send costs money and
# texts a real handset. Tests that cover the channel enable it explicitly
# and mock the transport.
SMS_ENABLED = False
TWILIO_ACCOUNT_SID = ""
TWILIO_AUTH_TOKEN = ""
TWILIO_FROM_NUMBER = ""
# The engine lane calls every configured LLM provider. Provider keys come
# from .env on a developer machine, so without this the scan pipeline tests
# would make real, billable calls to Claude/GPT/Perplexity.
BRAND_RESEARCH_ENGINES_ENABLED = False

# Business flows are ON for the suite (the org/SSO tests exercise them);
# individual tests assert the OFF behavior via override_settings.
ORG_FEATURES_ENABLED = True

# The SAML bridge stays dormant in the suite regardless of the
# developer's .env — tests that cover the lane set the keys via the
# settings fixture and mock the HTTP calls. Mirrors the POLAR pin below.
SSO_BRIDGE_PROVIDER = "ssoready"
SSOREADY_API_KEY = ""
WORKOS_API_KEY = ""
WORKOS_CLIENT_ID = ""

# Polar metering must never leave the process in tests: no outbox rows
# unless a test opts in via override_settings, never a real token, and
# reads always come from the local ledger. Mirrors the facade pattern
# above so an exported shell POLAR_ACCESS_TOKEN can't flip the suite.
POLAR_ACCESS_TOKEN = ""
POLAR_INGEST_MODE = "off"
POLAR_READS_ENABLED = False

# The suite must stay deterministic and offline: pin retrieval to the
# Python path regardless of what the developer's .env sets. Tests that
# cover the vector backend opt in per-test via override_settings, with
# a tmp-dir index - never the shared dev server.
RAG_VECTOR_BACKEND = "python"
RAG_CHROMA_URL = ""

# Deterministic free-plan allowance regardless of the developer's .env
# (local dev raises it so the $1 default doesn't stall testing).
AI_FREE_MONTHLY_CAP_USD = 1.0

# The agent runs turns inline in the suite, against the fake Anthropic
# client installed by apps/agent/tests (never the real API).
AGENT_ENABLED = True
AGENT_PROCESS = True
AGENT_RUN_MODE = "inline"

# Country checks run before the create request returns, in the request's
# thread, so tests see every cell settled.
COUNTRY_CHECK_RUN_MODE = "inline"

# Insights runs, likewise.
INSIGHTS_RUN_MODE = "inline"

# Ask AI and Autopilot runs, likewise (with "auto", eager Celery would start
# daemon threads).
PROMPT_BOX_RUN_MODE = "inline"
AUTOPILOT_RUN_MODE = "inline"

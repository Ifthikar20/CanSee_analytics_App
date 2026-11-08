from .base import *  # noqa
import os

DEBUG = False

# Never serve production with the public default secret: session data,
# password-reset tokens and (through SIMPLE_JWT's fallback) every access
# token would be signed with a key anyone can read.
if not SECRET_KEY or SECRET_KEY == INSECURE_DEFAULT_SECRET_KEY:  # noqa: F405
    raise ImproperlyConfigured("DJANGO_SECRET_KEY must be set to a private value in production.")  # noqa: F405
if SIMPLE_JWT.get("SIGNING_KEY") in ("", INSECURE_DEFAULT_SECRET_KEY):  # noqa: F405
    raise ImproperlyConfigured("JWT_SIGNING_KEY (or DJANGO_SECRET_KEY) must be set in production.")  # noqa: F405

# Trust nginx's X-Forwarded-Proto only when this deployment actually
# terminates TLS. On a plaintext deployment the header is meaningless, and
# honouring it would make request.is_secure() return True over http:// --
# which is precisely what makes Django emit Secure cookies the browser then
# throws away. See the PUBLIC_SCHEME block in base.py.
if PUBLIC_SCHEME == "https":  # noqa: F405
    SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")
else:
    SECURE_PROXY_SSL_HEADER = None
ADMINS = [("Ops Team", env("OPS_EMAIL", default="ops@cansee.ai"))]  # noqa: F405
SERVER_EMAIL = env("SERVER_EMAIL", default="noreply@cansee.ai")  # noqa: F405

# ── Email Backend (SMTP — works with SES, SendGrid, Mailgun, etc.) ──
EMAIL_BACKEND = "django.core.mail.backends.smtp.EmailBackend"
EMAIL_HOST = env("EMAIL_HOST", default="")  # noqa: F405
EMAIL_PORT = env.int("EMAIL_PORT", default=587)  # noqa: F405
EMAIL_HOST_USER = env("EMAIL_HOST_USER", default="")  # noqa: F405
EMAIL_HOST_PASSWORD = env("EMAIL_HOST_PASSWORD", default="")  # noqa: F405
EMAIL_USE_TLS = env.bool("EMAIL_USE_TLS", default=True)  # noqa: F405
DEFAULT_FROM_EMAIL = env("DEFAULT_FROM_EMAIL", default="Cansee <noreply@cansee.ai>")  # noqa: F405

# No EMAIL_HOST used to silently downgrade to the console backend, which
# made every "sent" email a lie in any misconfigured prod deploy —
# verification codes, password resets and org invitations would report
# success and go nowhere. Now: core.checks.check_email_transport errors at
# `manage.py check --deploy`, and a send on a host-less SMTP backend fails
# loudly instead of silently. EMAIL_ALLOW_CONSOLE=true is the explicit
# escape hatch for environments that genuinely have no mail (staging).
if not EMAIL_HOST and env.bool("EMAIL_ALLOW_CONSOLE", default=False):  # noqa: F405
    EMAIL_BACKEND = "django.core.mail.backends.console.EmailBackend"

# ── Trusted Origins (Cloudflare → Nginx → Django) ──
#
# Derived from ALLOWED_HOSTS + PUBLIC_SCHEME rather than hardcoded, so there
# is no second list to keep in sync -- the previous hardcoded pair silently
# broke Django admin on any host other than cansee.ai, with no env override
# to fix it. Django 4+ requires the scheme prefix on each entry.
#
# CSRF_TRUSTED_ORIGINS is still honoured as an explicit override for the
# cases the derivation cannot know about: a separate frontend origin, or an
# apex/www pair where only one is in ALLOWED_HOSTS.
_explicit_csrf_origins = env.list("CSRF_TRUSTED_ORIGINS", default=[])  # noqa: F405
if _explicit_csrf_origins:
    CSRF_TRUSTED_ORIGINS = _explicit_csrf_origins
else:
    CSRF_TRUSTED_ORIGINS = [
        f"{PUBLIC_SCHEME}://{host}"  # noqa: F405
        for host in ALLOWED_HOSTS  # noqa: F405
        # A leading-dot wildcard is valid in ALLOWED_HOSTS but not a valid
        # origin; "*" would trust everything. Neither belongs here.
        if host and host != "*" and not host.startswith(".")
    ]

# ── DB connection encryption ──
# "prefer" is NOT a security setting: libpq silently falls back to an
# unencrypted connection when the server does not offer TLS, and nothing
# in the app can tell the difference afterwards. It is only defensible
# while Postgres is on loopback (DB_HOST=localhost), which is today's
# deployment. The moment the database moves to another host -- a managed
# instance, a separate node, anything crossing a network -- this must be
# "require" at minimum, and "verify-full" with DB_SSLROOTCERT if the
# provider publishes a CA. core.checks enforces that pairing at deploy
# time rather than leaving it to a code review.
DB_SSLMODE = env("DB_SSLMODE", default="prefer")  # noqa: F405
DATABASES["default"]["OPTIONS"] = {  # noqa: F405
    "sslmode": DB_SSLMODE,
    "connect_timeout": 10,
}
_db_sslrootcert = env("DB_SSLROOTCERT", default="")  # noqa: F405
if _db_sslrootcert:
    DATABASES["default"]["OPTIONS"]["sslrootcert"] = _db_sslrootcert  # noqa: F405

# File storage. Django 5.1 reads only STORAGES (DEFAULT_FILE_STORAGE and
# STATICFILES_STORAGE were removed in 5.1, so the S3 lines that used to be
# here had no effect). Default: the local filesystem, which is what nginx
# serves today from the shared static volume. USE_S3_STORAGE=true sends
# static files and uploads to AWS_STORAGE_BUCKET_NAME instead (an IAM role
# supplies credentials; leave the AWS_*_KEY variables empty).
USE_S3_STORAGE = env.bool("USE_S3_STORAGE", default=False)  # noqa: F405
if USE_S3_STORAGE:
    STORAGES = {
        "default": {"BACKEND": "storages.backends.s3boto3.S3Boto3Storage"},
        "staticfiles": {"BACKEND": "storages.backends.s3boto3.S3StaticStorage"},
    }
else:
    STORAGES = {
        "default": {"BACKEND": "django.core.files.storage.FileSystemStorage"},
        "staticfiles": {"BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage"},
    }
AWS_ACCESS_KEY_ID = env("AWS_ACCESS_KEY_ID", default="")  # noqa: F405
AWS_SECRET_ACCESS_KEY = env("AWS_SECRET_ACCESS_KEY", default="")  # noqa: F405
AWS_STORAGE_BUCKET_NAME = env("AWS_STORAGE_BUCKET_NAME", default="")  # noqa: F405
AWS_S3_REGION_NAME = env("AWS_S3_REGION_NAME", default="us-east-1")  # noqa: F405
# A CloudFront domain in front of the bucket (e.g. static.cansee.ai), else
# the bucket's own S3 domain.
AWS_S3_CUSTOM_DOMAIN = env(  # noqa: F405
    "AWS_S3_CUSTOM_DOMAIN", default=f"{AWS_STORAGE_BUCKET_NAME}.s3.amazonaws.com",
)
AWS_DEFAULT_ACL = "private"
AWS_S3_FILE_OVERWRITE = False

# Encryption for anything we put in S3.
# At rest: request SSE explicitly on every PUT rather than relying on the
# bucket carrying a default-encryption policy -- if the bucket is ever
# recreated without one, uploads stay encrypted regardless. Set
# AWS_S3_KMS_KEY_ID to use a customer-managed KMS key instead of SSE-S3.
_s3_kms_key = env("AWS_S3_KMS_KEY_ID", default="")  # noqa: F405
AWS_S3_OBJECT_PARAMETERS = {
    "ServerSideEncryption": "aws:kms" if _s3_kms_key else "AES256",
}
if _s3_kms_key:
    AWS_S3_OBJECT_PARAMETERS["SSEKMSKeyId"] = _s3_kms_key
# In transit: boto3 defaults to HTTPS, but state it so a config change
# cannot quietly downgrade object traffic to plaintext.
AWS_S3_USE_SSL = True
AWS_S3_VERIFY = True

LOG_DIR = os.environ.get("LOG_DIR", "/app/logs")
os.makedirs(LOG_DIR, exist_ok=True)

LOGGING["handlers"].update(  # noqa: F405
    {
        "audit_file": {
            "class": "logging.handlers.RotatingFileHandler",
            "filename": f"{LOG_DIR}/audit.log",
            "maxBytes": 50_000_000,
            "backupCount": 90,
            "formatter": "json",
        },
        "security_file": {
            "class": "logging.handlers.RotatingFileHandler",
            "filename": f"{LOG_DIR}/security.log",
            "maxBytes": 50_000_000,
            "backupCount": 365,
            "formatter": "json",
        },
    }
)
LOGGING["loggers"]["audit"]["handlers"] = ["console", "audit_file"]  # noqa: F405
LOGGING["loggers"]["security"]["handlers"] = ["console", "security_file"]  # noqa: F405

if SENTRY_DSN:  # noqa: F405
    import sentry_sdk
    from sentry_sdk.integrations.celery import CeleryIntegration
    from sentry_sdk.integrations.django import DjangoIntegration

    sentry_sdk.init(
        dsn=SENTRY_DSN,  # noqa: F405
        integrations=[DjangoIntegration(), CeleryIntegration()],
        traces_sample_rate=0.05,
        environment="production",
    )

# Fail closed on field encryption in prod: EncryptedTextField refuses to store
# or read plaintext, and `manage.py check --deploy` errors if FIELD_ENCRYPTION_KEY
# is missing or invalid. Never store OAuth tokens / webhook secrets in the clear.
FIELD_ENCRYPTION_REQUIRED = True

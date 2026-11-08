"""Project-wide Django system checks.

Registered from apps.accounts.apps.AccountsConfig.ready() so they run on every
``manage.py check`` (and therefore in CI and at deploy time).
"""
from django.conf import settings
from django.core.checks import Error, Tags, register


@register(Tags.security)
def check_field_encryption_key(app_configs, **kwargs):
    """Error when field encryption is required but the key is missing/invalid.

    Pairs with core.encryption.field_encryption.EncryptedTextField, which fails
    closed at read/write time; this surfaces the misconfiguration up front via
    ``manage.py check --deploy`` instead of at the first secret read.
    """
    errors = []
    if not getattr(settings, "FIELD_ENCRYPTION_REQUIRED", False):
        return errors

    key = getattr(settings, "FIELD_ENCRYPTION_KEY", "") or ""
    if not key:
        errors.append(
            Error(
                "FIELD_ENCRYPTION_REQUIRED is set but FIELD_ENCRYPTION_KEY is empty.",
                hint=(
                    "Generate one with: python -c "
                    "\"from cryptography.fernet import Fernet; "
                    "print(Fernet.generate_key().decode())\" and set "
                    "FIELD_ENCRYPTION_KEY in the environment."
                ),
                id="core.E001",
            )
        )
        return errors

    try:
        from cryptography.fernet import Fernet

        Fernet(key.encode() if isinstance(key, str) else key)
    except Exception as exc:  # noqa: BLE001 — any parse failure is a config error
        errors.append(
            Error(
                f"FIELD_ENCRYPTION_KEY is not a valid Fernet key: {exc}",
                hint="It must be a url-safe base64-encoded 32-byte key.",
                id="core.E002",
            )
        )
    return errors


@register(Tags.security, deploy=True)
def check_email_transport(app_configs, **kwargs):
    """Error when the SMTP backend is active with no host to speak to.

    prod.py used to silently swap in the console backend when EMAIL_HOST
    was unset, so verification, password-reset and invitation emails
    reported success and went nowhere. The downgrade is now opt-in
    (EMAIL_ALLOW_CONSOLE=true); this check catches the remaining state —
    SMTP configured, host empty — before the first send fails at runtime.

    Deploy-only: ``manage.py check --deploy``. Dev (console) and tests
    (locmem) are unaffected.
    """
    errors = []
    backend = getattr(settings, "EMAIL_BACKEND", "") or ""
    host = (getattr(settings, "EMAIL_HOST", "") or "").strip()
    if backend.endswith("smtp.EmailBackend") and not host:
        errors.append(
            Error(
                "EMAIL_BACKEND is SMTP but EMAIL_HOST is empty — every email "
                "send (verification codes, password resets, org invitations) "
                "will fail.",
                hint=(
                    "Set EMAIL_HOST/EMAIL_PORT/EMAIL_HOST_USER/"
                    "EMAIL_HOST_PASSWORD/EMAIL_USE_TLS for your SMTP provider, "
                    "or set EMAIL_ALLOW_CONSOLE=true on an environment that "
                    "genuinely sends no mail."
                ),
                id="core.E007",
            )
        )
    return errors


# Hosts for which an unencrypted Postgres connection never leaves the machine.
_LOOPBACK_DB_HOSTS = {"", "localhost", "127.0.0.1", "::1"}
# sslmode values that permit a silent fallback to an unencrypted connection.
_UNSAFE_SSLMODES = {"disable", "allow", "prefer"}


def _is_private_db_host(host: str) -> bool:
    """True when an unencrypted connection to ``host`` stays on private infra.

    Three cases are treated as private:

    * loopback and Unix domain sockets -- never touch a network at all;
    * single-label hostnames such as ``db`` or ``postgres`` -- these are
      Docker Compose service names and Kubernetes in-cluster short names,
      resolvable only on a private bridge/overlay network. Every managed
      database endpoint is a dotted FQDN or a bare IP, so the absence of a
      dot is a reliable signal here even though it is a heuristic;
    * anything named in ``DB_TRUSTED_HOSTS`` -- the escape hatch for a
      private network that does use dotted names (a VPC-internal DNS
      record, for instance).

    The heuristic errs toward flagging: an unrecognised dotted host is
    reported rather than assumed safe.
    """
    if host in _LOOPBACK_DB_HOSTS or host.startswith("/"):
        return True
    trusted = {
        h.strip().lower()
        for h in getattr(settings, "DB_TRUSTED_HOSTS", []) or []
        if h and h.strip()
    }
    if host in trusted:
        return True
    # No dot and no colon -> a short name on a private container network.
    return "." not in host and ":" not in host


@register(Tags.security, deploy=True)
def check_database_ssl(app_configs, **kwargs):
    """Error when Postgres is reached over a network without enforced TLS.

    ``sslmode=prefer`` is the libpq default and reads as if it does
    something, but it silently falls back to plaintext when the server
    does not offer TLS -- and the application cannot tell afterwards
    which it got. That is acceptable only while the connection stays on
    private infrastructure. Once DB_HOST resolves over a real network,
    credentials and every row in transit depend on the mode being
    ``require`` or stricter.

    See ``_is_private_db_host`` for what counts as private; set
    ``DB_TRUSTED_HOSTS`` to add a dotted name on a private network.

    Deploy-only: ``manage.py check --deploy``. Development and Docker
    Compose (``DB_HOST=db``) are unaffected.
    """
    errors = []
    db = settings.DATABASES.get("default", {})
    if "postgresql" not in db.get("ENGINE", ""):
        return errors

    host = (db.get("HOST") or "").strip().lower()
    if _is_private_db_host(host):
        return errors

    sslmode = str(db.get("OPTIONS", {}).get("sslmode", "prefer")).strip().lower()
    if sslmode in _UNSAFE_SSLMODES:
        errors.append(
            Error(
                f"DB_HOST is {host!r} (not loopback) but sslmode is "
                f"{sslmode!r}, which allows an unencrypted connection.",
                hint=(
                    "Set DB_SSLMODE=require. Prefer verify-full and point "
                    "DB_SSLROOTCERT at the provider's CA bundle so the "
                    "server certificate is actually validated -- 'require' "
                    "encrypts but does not authenticate the server."
                ),
                id="core.E003",
            )
        )
    return errors


# Reserved local suffixes and names that are never worth a certificate.
_NON_PUBLIC_HOSTS = {"", "*", "localhost", "testserver"}
_NON_PUBLIC_SUFFIXES = (".local", ".internal", ".localdomain", ".test", ".localhost")


def _is_public_domain(host: str) -> bool:
    """True when ``host`` is a DNS name a certificate authority would sign.

    Bare IPs, loopback, single-label container names (``web``, ``db``) and
    the reserved local suffixes are not: reaching those over plaintext is
    the deployment PUBLIC_SCHEME=http exists for. A dotted, non-reserved
    name is, and serving it over plaintext is the accident this guards.
    """
    import ipaddress

    host = (host or "").strip().lower().rstrip(".")
    if host in _NON_PUBLIC_HOSTS:
        return False
    try:
        ipaddress.ip_address(host.strip("[]"))
        return False
    except ValueError:
        pass
    if host.startswith("."):  # ALLOWED_HOSTS subdomain wildcard
        return True
    if "." not in host:  # container / LAN short name
        return False
    return not host.endswith(_NON_PUBLIC_SUFFIXES)


@register(Tags.security, deploy=True)
def check_public_scheme(app_configs, **kwargs):
    """Error when the app serves a real domain over plaintext.

    PUBLIC_SCHEME=http is a genuine downgrade: session cookies, CSRF
    tokens, the JWT refresh cookie and every Authorization header travel
    in the clear. It is defensible for exactly one deployment -- a host
    reached by bare IP, where no certificate authority will issue a
    certificate -- and for nothing else.

    The risk is not that someone chooses it deliberately. It is that an
    .env from the IP box gets copied to the one serving the domain, where
    the downgrade is invisible: every page still renders and every request
    still succeeds. Django's own deploy checks flag the individual
    settings (W004, W008, W012, W016) but only as warnings, and in this
    mode each one is expected in isolation. The combination is what is
    never acceptable, and that is what this errors on.

    Deploy-only: ``manage.py check --deploy``.
    """
    errors = []
    if getattr(settings, "DEBUG", False):
        return errors
    if getattr(settings, "PUBLIC_SCHEME", "https") == "https":
        return errors

    domains = sorted(
        {h for h in getattr(settings, "ALLOWED_HOSTS", []) or [] if _is_public_domain(h)}
    )
    if domains:
        errors.append(
            Error(
                "PUBLIC_SCHEME=http but ALLOWED_HOSTS contains the domain "
                f"name(s) {', '.join(domains)}. Requests arriving on a domain "
                "would carry their session cookie, CSRF token and JWT refresh "
                "cookie in the clear, and nothing in the response would say so.",
                hint=(
                    "If the domain resolves here, unset PUBLIC_SCHEME (it "
                    "defaults to https) and mount docker/nginx/nginx.prod.conf. "
                    "If this is the bare-IP deployment, remove the domain from "
                    "ALLOWED_HOSTS."
                ),
                id="core.E004",
            )
        )

    # A settings module re-setting one of the derived values after importing
    # base.py puts the deployment back into one of the two broken states.
    contradictions = []
    if getattr(settings, "SECURE_SSL_REDIRECT", False):
        contradictions.append(
            "SECURE_SSL_REDIRECT=True (301s every request to an https:// URL "
            "where nothing is listening -- total outage)"
        )
    if getattr(settings, "SECURE_PROXY_SSL_HEADER", None):
        contradictions.append(
            "SECURE_PROXY_SSL_HEADER is set (Django then treats any request "
            "carrying X-Forwarded-Proto: https as secure and marks every "
            "cookie Secure; the browser discards them over http:// without "
            "raising anything, so login returns 200 and the session is gone "
            "on the next request)"
        )
    for name in ("SESSION_COOKIE_SECURE", "CSRF_COOKIE_SECURE"):
        if getattr(settings, name, False):
            contradictions.append(
                f"{name}=True (the browser discards the cookie over http)"
            )
    if contradictions:
        errors.append(
            Error(
                "PUBLIC_SCHEME=http but these settings contradict it: "
                + "; ".join(contradictions)
                + ".",
                hint=(
                    "config/settings/base.py derives all four from "
                    "PUBLIC_SCHEME. A settings module is re-setting one after "
                    "importing base -- check prod.py and staging.py, which "
                    "each used to hardcode SECURE_PROXY_SSL_HEADER."
                ),
                id="core.E005",
            )
        )

    # https:// origins left in the trust lists match nothing on a plaintext
    # deployment, and fail in ways that name no setting in any log.
    leftovers = []
    for name in ("CSRF_TRUSTED_ORIGINS", "CORS_ALLOWED_ORIGINS"):
        for origin in getattr(settings, name, []) or []:
            if str(origin).startswith("https://"):
                leftovers.append(f"{name}={origin}")
    frontend_url = str(getattr(settings, "FRONTEND_URL", "") or "")
    if frontend_url.startswith("https://"):
        leftovers.append(f"FRONTEND_URL={frontend_url}")
    if leftovers:
        errors.append(
            Error(
                "PUBLIC_SCHEME=http but https:// origins remain configured: "
                + ", ".join(sorted(leftovers))
                + ".",
                hint=(
                    "Django compares CSRF_TRUSTED_ORIGINS against the browser's "
                    "Origin header verbatim, so an https:// entry never matches "
                    "an http:// request -- every admin POST 403s with nothing "
                    "in the log naming the setting. An https:// FRONTEND_URL "
                    "calling a plaintext API is blocked as mixed content. "
                    "Update ALLOWED_HOSTS (CSRF_TRUSTED_ORIGINS derives from "
                    "it), CORS_ALLOWED_ORIGINS and FRONTEND_URL together."
                ),
                id="core.E006",
            )
        )

    return errors


@register(Tags.security)
def check_secret_keys(app_configs, **kwargs):
    """Error when DEBUG is off and the Django or JWT signing key is the
    public default from settings/base.py (anyone could sign tokens)."""
    if getattr(settings, "DEBUG", False):
        return []
    default = getattr(settings, "INSECURE_DEFAULT_SECRET_KEY", None)
    errors = []
    if not settings.SECRET_KEY or settings.SECRET_KEY == default:
        errors.append(Error(
            "SECRET_KEY is empty or the public default.",
            hint="Set DJANGO_SECRET_KEY to a long random value.",
            id="core.E008",
        ))
    signing = (getattr(settings, "SIMPLE_JWT", {}) or {}).get("SIGNING_KEY")
    if not signing or signing == default:
        errors.append(Error(
            "The JWT signing key is empty or the public default.",
            hint="Set JWT_SIGNING_KEY (or DJANGO_SECRET_KEY) to a long random value.",
            id="core.E009",
        ))
    return errors


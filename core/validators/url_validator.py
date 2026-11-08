import ipaddress
import re
from urllib.parse import urlparse

from django.core.exceptions import ValidationError

# One DNS label: letters, digits and inner hyphens, 1-63 characters.
_LABEL = re.compile(r"^(?!-)[a-z0-9-]{1,63}(?<!-)$")
# A top-level domain is alphabetic (com, ai, travel) or an IDN in
# punycode (xn--p1ai). A numeric last label is an IP address, not a name.
_TLD = re.compile(r"^(?:[a-z]{2,63}|xn--[a-z0-9-]{1,59})$")

_PRIVATE_PATTERNS = (
    r"^localhost",
    r"^127\.",
    r"^10\.",
    r"^192\.168\.",
    r"^172\.(1[6-9]|2\d|3[01])\.",
)

_NOT_PUBLIC = "URL must be a publicly reachable domain."


def _check_ip(host: str) -> bool:
    """True when ``host`` is an IP literal (raising unless it is public)."""
    try:
        ip = ipaddress.ip_address(host.strip("[]"))
    except ValueError:
        return False
    if not ip.is_global:
        raise ValidationError(_NOT_PUBLIC)
    return True


def _check_domain(host: str) -> None:
    """A public domain name has at least two labels and a real TLD.

    ``not-a-url`` or ``intranet`` resolve, if at all, only on a private
    network, so they cannot be a customer's public website.
    """
    try:
        # Internationalised names (münchen.de) are checked in punycode.
        host = host.encode("idna").decode("ascii")
    except UnicodeError:
        raise ValidationError("Enter a website address such as example.com.") from None
    labels = host.split(".")
    if len(labels) < 2 or not all(_LABEL.match(label) for label in labels):
        raise ValidationError("Enter a website address such as example.com.")
    if not _TLD.match(labels[-1]):
        raise ValidationError("Enter a website address such as example.com.")


def validate_website_url(url: str) -> str:
    """Validate that a URL is a proper, publicly reachable website URL."""
    if not url:
        raise ValidationError("URL is required.")

    # Normalize — add https if missing
    if not url.startswith(("http://", "https://")):
        url = f"https://{url}"

    try:
        parsed = urlparse(url)
        host = (parsed.hostname or "").rstrip(".").lower()
        parsed.port  # noqa: B018 - raises ValueError for a malformed port
    except ValueError:
        raise ValidationError("Invalid URL format.") from None

    if not parsed.netloc or not host:
        raise ValidationError("URL must include a domain.")

    # Block localhost and private IPs
    if any(re.match(p, host) for p in _PRIVATE_PATTERNS):
        raise ValidationError(_NOT_PUBLIC)

    if not _check_ip(host):
        _check_domain(host)

    return url

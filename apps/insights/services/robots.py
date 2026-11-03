"""robots.txt as RFC 9309 reads it (contract 5).

``parse`` turns the file into groups of user agents and their rules,
``rules_for`` picks the rules a crawler's product token obeys, and
``is_allowed`` decides one URL path the way RFC 9309 tells crawlers to:
the longest matching pattern wins, ``allow`` wins a tie, and no match
means allowed. ``fetch`` reads ``/robots.txt`` with the SSRF-safe fetcher
and says which RFC state it is in: ``found`` (2xx, parsed), ``missing``
(4xx, or more than five redirects: crawlers may read everything),
``unreachable`` (5xx or a network error: crawlers that follow the
standard treat the whole site as blocked) or ``failed`` (not a public web
address, so Cansee may not fetch it).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from urllib.parse import urlsplit

from apps.agent.services.browse import USER_AGENT
from core.validators.safe_http import FetchError, safe_get

# RFC 9309 2.5: crawlers must parse at least the first 500 KiB.
MAX_BYTES = 512_000
FETCH_TIMEOUT = (5, 10)
FETCH_DEADLINE = 10
MAX_REDIRECTS = 5
ALLOWED_PORTS = (80, 443, 8080, 8443)
# What safe_get raises past max_redirects (code fetch_failed).
TOO_MANY_REDIRECTS = "too many redirects"

FOUND, MISSING, UNREACHABLE, FAILED = "found", "missing", "unreachable", "failed"

_LINE_BREAK = re.compile(r"\r\n|\r|\n")
_UNRESERVED = frozenset("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-._~")
_HEX = frozenset("0123456789abcdefABCDEF")


@dataclass
class Group:
    agents: list[str] = field(default_factory=list)
    # ("allow" | "disallow", pattern as written)
    rules: list[tuple[str, str]] = field(default_factory=list)


@dataclass
class Robots:
    groups: list[Group] = field(default_factory=list)
    sitemaps: list[str] = field(default_factory=list)


def decode(body) -> str:
    """The first MAX_BYTES of ``body`` as text: UTF-8 with replacement,
    without a byte order mark."""
    if isinstance(body, str):
        body = body.encode("utf-8")
    text = bytes(body or b"")[:MAX_BYTES].decode("utf-8", errors="replace")
    return text[1:] if text.startswith("﻿") else text


def parse(body) -> Robots:
    """Groups and sitemaps of a robots.txt (bytes or text).

    Consecutive ``user-agent`` lines open one group and the rules after
    them belong to it; rules before any ``user-agent`` are ignored;
    ``sitemap`` lines are global; an empty ``allow`` or ``disallow`` is no
    rule; lines that are not ``key: value`` are skipped.
    """
    robots = Robots()
    current: Group | None = None
    for raw in _LINE_BREAK.split(decode(body)):
        line = raw.split("#", 1)[0]
        if ":" not in line:
            continue
        key, value = line.split(":", 1)
        key, value = key.strip().lower(), value.strip()
        if key == "user-agent":
            if current is None or current.rules:
                current = Group()
                robots.groups.append(current)
            if value:
                current.agents.append(value)
        elif key in ("allow", "disallow"):
            if current is not None and value:
                current.rules.append((key, value))
        elif key == "sitemap" and value:
            robots.sitemaps.append(value)
    return robots


def _product(agent: str) -> str:
    return agent.strip().split("/", 1)[0].strip().casefold()


def rules_for(robots: Robots, token: str) -> tuple[list[str], list[tuple[str, str]]]:
    """(matched user-agent values, combined rules) for a crawler's product
    token. Every group naming the token is combined; without one, every
    ``*`` group; without those, no rules (everything allowed)."""
    wanted = token.strip().casefold()
    matched = [g for g in robots.groups if any(_product(a) == wanted for a in g.agents)]
    if not matched:
        matched = [g for g in robots.groups if any(a.strip() == "*" for a in g.agents)]
        agents = ["*"] if matched else []
    else:
        agents = []
        for group in matched:
            for agent in group.agents:
                if _product(agent) == wanted and agent not in agents:
                    agents.append(agent)
    return agents, [rule for group in matched for rule in group.rules]


def normalize(value: str) -> str:
    """Percent-encoding normalized as RFC 9309 compares paths: unreserved
    characters decoded, every other escape upper-cased, non-ASCII and
    control characters encoded as UTF-8."""
    out: list[str] = []
    i, n = 0, len(value)
    while i < n:
        char = value[i]
        if char == "%" and i + 2 < n and value[i + 1] in _HEX and value[i + 2] in _HEX:
            decoded = chr(int(value[i + 1:i + 3], 16))
            out.append(decoded if decoded in _UNRESERVED else "%" + value[i + 1:i + 3].upper())
            i += 3
            continue
        if ord(char) > 126 or ord(char) <= 32:
            out.extend(f"%{byte:02X}" for byte in char.encode("utf-8"))
        else:
            out.append(char)
        i += 1
    return "".join(out)


def url_path(url: str) -> str:
    """The part of a URL robots rules match: the path (``/`` when empty)
    plus ``?query``."""
    try:
        parts = urlsplit(url or "")
    except ValueError:
        return "/"
    path = parts.path or "/"
    return f"{path}?{parts.query}" if parts.query else path


def _glob(pattern: str) -> str:
    """A rule pattern as a whole-path glob: ``*`` is any sequence, a
    trailing ``$`` anchors the end, otherwise the pattern is a prefix."""
    return pattern[:-1] if pattern.endswith("$") else f"{pattern}*"


def _matches(glob: str, path: str) -> bool:
    """Whether ``glob`` (only ``*`` is special) matches all of ``path``.
    Greedy, backtracking only to the last ``*``: the work stays within the
    path's length times the pattern's, where a regex of many ``.*`` can
    take exponential time on a hostile robots.txt."""
    i = j = 0
    star, mark = -1, 0
    while i < len(path):
        if j < len(glob) and glob[j] != "*" and glob[j] == path[i]:
            i += 1
            j += 1
        elif j < len(glob) and glob[j] == "*":
            star, mark = j, i
            j += 1
        elif star != -1:
            j = star + 1
            mark += 1
            i = mark
        else:
            return False
    while j < len(glob) and glob[j] == "*":
        j += 1
    return j == len(glob)


class Matcher:
    """One token's rules compiled once, for checking many paths."""

    def __init__(self, rules):
        self.items = []
        for kind, pattern in rules:
            normalized = normalize(pattern)
            self.items.append((
                _glob(normalized), len(normalized.encode("utf-8")), kind == "allow",
                f"{'Allow' if kind == 'allow' else 'Disallow'}: {pattern}",
            ))

    def check(self, path: str) -> tuple[bool, str | None]:
        target = normalize(path or "/")
        if target.split("?", 1)[0] == "/robots.txt":
            return True, None
        best: tuple[int, bool] | None = None
        best_rule = None
        for glob, length, allow, text in self.items:
            if (best is None or (length, allow) > best) and _matches(glob, target):
                best, best_rule = (length, allow), text
        if best is None:
            return True, None
        return best[1], best_rule


def is_allowed(rules, path: str) -> tuple[bool, str | None]:
    """(allowed, the deciding rule as written, e.g. "Disallow: /x") for a
    path (with its ``?query``); the rule is None when none matched.
    ``/robots.txt`` itself is always allowed. ``rules`` is a list of
    ``(kind, pattern)`` or a Matcher."""
    matcher = rules if isinstance(rules, Matcher) else Matcher(rules)
    return matcher.check(path)


@dataclass
class Fetched:
    """One robots.txt read: ``info`` is what the audit stores and shows
    (contract 5d ``robots``), ``robots`` the parsed file when found."""

    info: dict
    robots: Robots | None = None
    text: str = ""

    @property
    def state(self) -> str:
        return self.info["state"]


def _info(state: str, url: str, status_code=None, robots: Robots | None = None,
          size: int | None = None) -> dict:
    return {
        "state": state,
        "url": url,
        "status_code": status_code,
        "groups": len(robots.groups) if robots is not None else None,
        "sitemaps": len(robots.sitemaps) if robots is not None else None,
        "bytes": size,
    }


def fetch(origin: str) -> Fetched:
    """Read ``{origin}/robots.txt`` and classify it (RFC 9309 2.3.1)."""
    url = f"{origin.rstrip('/')}/robots.txt"
    try:
        resp = safe_get(
            url,
            timeout=FETCH_TIMEOUT,
            max_bytes=MAX_BYTES,
            truncate=True,
            max_redirects=MAX_REDIRECTS,
            allowed_content_types=(),
            allowed_ports=ALLOWED_PORTS,
            deadline=FETCH_DEADLINE,
            headers={"User-Agent": USER_AGENT},
        )
    except FetchError as exc:
        if exc.code == "blocked_url":
            return Fetched(_info(FAILED, url))
        if str(exc) == TOO_MANY_REDIRECTS:
            # RFC 9309 2.3.1.1: crawlers may treat the file as unavailable.
            return Fetched(_info(MISSING, url))
        return Fetched(_info(UNREACHABLE, url))
    status = resp.status_code
    if 200 <= status < 300:
        robots = parse(resp.content)
        text = decode(resp.content).replace("\x00", "")
        return Fetched(_info(FOUND, url, status, robots, len(resp.content)), robots, text)
    if 400 <= status < 500:
        return Fetched(_info(MISSING, url, status))
    return Fetched(_info(UNREACHABLE, url, status))

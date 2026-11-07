"""The Anthropic client every agent request goes through.

One client per process and API key (the SDK client is thread-safe and
pools connections). Tests replace it with set_client_factory(), which
lets scripted SSE run through the real SDK parser.
"""
from __future__ import annotations

import threading
from collections.abc import Callable

import anthropic
from django.conf import settings

from apps.agent.exceptions import AgentNotConfigured

# Per phase: connect, wait between streamed bytes, upload, pool checkout.
CLIENT_TIMEOUT = anthropic.Timeout(connect=10.0, read=300.0, write=30.0, pool=10.0)
CLIENT_MAX_RETRIES = 2

ClientFactory = Callable[[], anthropic.Anthropic]

_lock = threading.Lock()
_factory: ClientFactory | None = None
_clients: dict[str, anthropic.Anthropic] = {}


def get_client() -> anthropic.Anthropic:
    """The client for the next request.

    Raises AgentNotConfigured when no ANTHROPIC_API_KEY is set (and no
    test factory is installed).
    """
    factory = _factory
    if factory is not None:
        return factory()
    api_key = getattr(settings, "ANTHROPIC_API_KEY", "") or ""
    if not api_key:
        raise AgentNotConfigured()
    client = _clients.get(api_key)
    if client is None:
        with _lock:
            client = _clients.get(api_key)
            if client is None:
                client = anthropic.Anthropic(
                    api_key=api_key,
                    timeout=CLIENT_TIMEOUT,
                    max_retries=CLIENT_MAX_RETRIES,
                )
                _clients.clear()
                _clients[api_key] = client
    return client


def set_client_factory(factory: ClientFactory | None) -> None:
    """Install a factory that builds every client from now on (tests).

    None restores the default client.
    """
    global _factory
    with _lock:
        _factory = factory
        _clients.clear()

"""Plain chat-completion providers for utility LLM calls.

Unlike the audit-answer providers in apps/llm_ranking/providers — which
inject a numbered-list system prompt and may attach web-search tools —
these send the prompt exactly as given. model and max_tokens are
per-instance so extraction, drafting, judging, and onboarding keep the
model they were tuned for, while every call still passes through the
base class's spend wall, cost recording, rate limit, and breaker.

They deliberately reuse the audit providers' breaker/bucket names
("claude", "gpt4"): the upstream account limits are shared, so audit
fan-out and utility calls must drain the same bucket to stay under them.

Usage:
    from core.llm import ClaudeUtility
    result = ClaudeUtility(model="claude-haiku-4-5").query(
        prompt, system_prompt=..., user=user, website=website,
        module="brand_vault", role="fact_extraction",
    )
    if result.succeeded: ... result.text ...
"""
from __future__ import annotations

from core.llm.base import LLMProvider, ProviderResult

# What an unset LLM_CLAUDE_MODEL resolves to; matches the audit-side
# ClaudeProvider.DEFAULT_MODEL (the cheapest current Claude model).
DEFAULT_CLAUDE_MODEL = "claude-haiku-4-5"


def configured_claude_model() -> str:
    """The deployment's configured Claude model (settings.LLM_CLAUDE_MODEL)."""
    from django.conf import settings

    return getattr(settings, "LLM_CLAUDE_MODEL", "") or DEFAULT_CLAUDE_MODEL


class ClaudeUtility(LLMProvider):
    name = "claude"
    api_key_setting = "ANTHROPIC_API_KEY"
    # Match the audit-side ClaudeProvider so the shared Anthropic bucket
    # keeps the combined call rate under the account tier limit.
    rpm = 40
    burst = 15

    # No sampling parameters: API version 2026-10-21 (anthropic SDK 2.x)
    # rejects temperature/top_p/top_k on every model.
    def __init__(self, *, model: str, max_tokens: int = 1024):
        super().__init__()
        self.model = model
        self.max_tokens = max_tokens

    def _call(self, *, prompt: str, system_prompt: str,
              region: str = "") -> ProviderResult:
        import anthropic

        client = anthropic.Anthropic(api_key=self.api_key)
        kwargs = dict(
            model=self.model,
            max_tokens=self.max_tokens,
            messages=[{"role": "user", "content": prompt}],
        )
        if system_prompt:
            kwargs["system"] = system_prompt
        resp = client.messages.create(**kwargs)
        text = " ".join(
            b.text for b in resp.content
            if getattr(b, "type", "") == "text" and getattr(b, "text", "")
        ).strip()
        return ProviderResult(
            succeeded=True,
            text=text,
            input_tokens=resp.usage.input_tokens,
            output_tokens=resp.usage.output_tokens,
        )


class OpenAIUtility(LLMProvider):
    name = "gpt4"
    api_key_setting = "OPENAI_API_KEY"

    def __init__(self, *, model: str, max_tokens: int = 1024,
                 temperature: float | None = None,
                 response_format: dict | None = None):
        super().__init__()
        self.model = model
        self.max_tokens = max_tokens
        self.temperature = temperature
        self.response_format = response_format

    def _call(self, *, prompt: str, system_prompt: str,
              region: str = "") -> ProviderResult:
        import openai

        client = openai.OpenAI(api_key=self.api_key)
        messages = []
        if system_prompt:
            messages.append({"role": "system", "content": system_prompt})
        messages.append({"role": "user", "content": prompt})
        kwargs = dict(model=self.model, max_tokens=self.max_tokens, messages=messages)
        if self.temperature is not None:
            kwargs["temperature"] = self.temperature
        if self.response_format is not None:
            kwargs["response_format"] = self.response_format
        resp = client.chat.completions.create(**kwargs)
        usage = getattr(resp, "usage", None)
        return ProviderResult(
            succeeded=True,
            text=(resp.choices[0].message.content or "").strip(),
            input_tokens=getattr(usage, "prompt_tokens", 0) if usage else 0,
            output_tokens=getattr(usage, "completion_tokens", 0) if usage else 0,
        )

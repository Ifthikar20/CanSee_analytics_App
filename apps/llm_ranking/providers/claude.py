"""Anthropic Claude provider."""
from django.conf import settings

from .base import LLMProvider, ProviderResult

DEFAULT_SYSTEM = (
    "When listing tools, platforms, or products, please use a numbered list "
    "(1., 2., 3., etc.) and include a brief description for each. "
    "Be specific and mention actual product/company names."
)


class ClaudeProvider(LLMProvider):
    name = "claude"
    # Cheap, current default. Haiku 4.5 is the lowest-cost Claude model and
    # is plenty for generating a listing-style answer. Override per
    # environment with LLM_CLAUDE_MODEL (e.g. a Sonnet id) when you want the
    # answer to reflect a pricier consumer model.
    DEFAULT_MODEL = "claude-haiku-4-5"
    model = DEFAULT_MODEL
    api_key_setting = "ANTHROPIC_API_KEY"
    # Anthropic Tier 1 = 50 RPM. Stay well under to share with extraction.
    rpm = 40
    burst = 15

    # Output budget for models that answer without thinking unless asked
    # (Haiku 4.5, the 4.x Sonnet and Opus models). A buying-guide answer
    # with a ranked list often runs past 1024 tokens and was cut off; the
    # bill follows what the model writes, not this cap.
    ANSWER_MAX_TOKENS = 4096
    # Model families that think whenever `thinking` is omitted (adaptive by
    # default, or always on). max_tokens caps thinking plus the answer on
    # these, so a 1024 budget can end mid-list; they get room for both, and
    # low effort keeps the thinking short for a listing-style answer.
    THINKING_BY_DEFAULT = (
        "claude-sonnet-5", "claude-opus-5", "claude-fable-5",
        "claude-mythos-5", "claude-haiku-5",
    )
    THINKING_MAX_TOKENS = 8000
    THINKING_EFFORT = "low"

    def __init__(self):
        super().__init__()
        self.model = getattr(settings, "LLM_CLAUDE_MODEL", "") or self.DEFAULT_MODEL

    def _call(self, *, prompt: str, system_prompt: str,
              region: str = "") -> ProviderResult:
        import anthropic

        from apps.llm_ranking.services.regions import get_region

        client = anthropic.Anthropic(api_key=self.api_key)
        thinks = self.model.startswith(self.THINKING_BY_DEFAULT)
        max_tokens = self.THINKING_MAX_TOKENS if thinks else self.ANSWER_MAX_TOKENS
        kwargs = dict(
            model=self.model,
            max_tokens=max_tokens,
            system=system_prompt or DEFAULT_SYSTEM,
            messages=[{"role": "user", "content": prompt}],
        )
        if thinks:
            kwargs["output_config"] = {"effort": self.THINKING_EFFORT}
        if getattr(settings, "LLM_WEBSEARCH_ENABLED", False):
            tool: dict = {
                "type": "web_search_20250305",
                "name": "web_search",
                "max_uses": 5,
            }
            country = get_region(region).perplexity_country if region else ""
            if country:
                # Bias Anthropic's server-side web search to this geo.
                tool["user_location"] = {"type": "approximate", "country": country}
            kwargs["tools"] = [tool]

        try:
            resp = client.messages.create(**kwargs)
        except Exception:
            # Web search may be unavailable for this account/model — retry as
            # a plain completion so the scan still succeeds.
            if "tools" in kwargs:
                kwargs.pop("tools")
                resp = client.messages.create(**kwargs)
            else:
                raise

        # With web search the response holds several blocks: text blocks
        # (each carrying a `citations` list for the passages it grounds),
        # server_tool_use blocks, and web_search_tool_result blocks listing
        # every page the search retrieved. Concatenate text verbatim — the
        # old " ".join injected stray gaps at citation boundaries, because
        # cited answers arrive split into many mid-sentence text blocks
        # ("budget allows ." in the UI).
        text = "".join(
            b.text for b in resp.content
            if getattr(b, "type", "") == "text" and getattr(b, "text", "")
        ).strip()

        usage = resp.usage
        # Server-side searches are billed per search on top of the tokens.
        searches = int(getattr(getattr(usage, "server_tool_use", None),
                               "web_search_requests", 0) or 0)

        # Only a finished answer is a measurement. A refusal, an answer cut
        # off at max_tokens or a turn the web search paused is a failure, so
        # a scan never records "not named" from an answer that never got
        # there. The call was still billed, so its usage goes with it.
        incomplete = _incomplete_answer(resp, text, max_tokens)
        if incomplete:
            return ProviderResult(
                succeeded=False,
                error=incomplete,
                input_tokens=usage.input_tokens,
                output_tokens=usage.output_tokens,
                web_search_requests=searches,
            )

        # Sources, Perplexity-style: URLs Claude itself cited (in first-cited
        # order), then the retrieved-but-uncited pages, capped so a 5-search
        # answer cannot flood the citations pipeline (which has no cap of its
        # own). On a failed search the tool_result `content` is an error
        # OBJECT rather than a list — server-tool errors arrive in-band with
        # HTTP 200, never as exceptions — so the isinstance guard skips it.
        citations: list[str] = []
        seen: set[str] = set()

        def _url_of(item):
            # SDK 2.x types citation entries and search results; older SDKs
            # returned them as plain dicts. Accept both.
            if isinstance(item, dict):
                return item.get("url")
            return getattr(item, "url", None)

        def _add(url) -> None:
            if isinstance(url, str) and url and url not in seen and len(citations) < 20:
                seen.add(url)
                citations.append(url)

        for b in resp.content:
            if getattr(b, "type", "") == "text":
                for c in getattr(b, "citations", None) or []:
                    _add(_url_of(c))
        for b in resp.content:
            if getattr(b, "type", "") == "web_search_tool_result":
                content = getattr(b, "content", None)
                if isinstance(content, list):
                    for r in content:
                        _add(_url_of(r))

        return ProviderResult(
            succeeded=True,
            text=text,
            input_tokens=usage.input_tokens,
            output_tokens=usage.output_tokens,
            citations=citations,
            web_search_requests=searches,
        )


def _incomplete_answer(resp, text: str, max_tokens: int) -> str:
    """Why ``resp`` is not a complete answer, or "" when it is one."""
    stop = getattr(resp, "stop_reason", None)
    if stop == "refusal":
        # stop_details is informational and may be absent; branch on
        # stop_reason only.
        category = getattr(getattr(resp, "stop_details", None), "category", None)
        suffix = f" ({category})" if category else ""
        return f"refusal: Claude declined to answer{suffix}"
    if stop == "max_tokens":
        return f"max_tokens: the answer was cut off at the {max_tokens}-token limit"
    if stop == "pause_turn":
        return "pause_turn: the web search paused the turn before an answer was written"
    if not text:
        return "empty_answer: Claude returned no answer text"
    return ""

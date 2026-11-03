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

    def __init__(self):
        super().__init__()
        self.model = getattr(settings, "LLM_CLAUDE_MODEL", "") or self.DEFAULT_MODEL

    def _call(self, *, prompt: str, system_prompt: str,
              region: str = "") -> ProviderResult:
        import anthropic

        from apps.llm_ranking.services.regions import get_region

        client = anthropic.Anthropic(api_key=self.api_key)
        kwargs = dict(
            model=self.model,
            max_tokens=1024,
            system=system_prompt or DEFAULT_SYSTEM,
            messages=[{"role": "user", "content": prompt}],
        )
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
            input_tokens=resp.usage.input_tokens,
            output_tokens=resp.usage.output_tokens,
            citations=citations,
        )

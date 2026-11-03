"""xAI Grok provider.

xAI is OpenAI-compatible. The legacy Live Search ``search_parameters`` block
was retired (Jan 2026), so web search now goes through the Responses API
``tools=[{"type": "web_search"}]``. The xAI API is location-agnostic — there
is no country parameter — so we localize by injecting the user's location
into the system instruction (see ``region_system_instruction``). That makes
Grok shift its web-search sub-queries and recommendations to that market.
"""
from django.conf import settings

from .base import LLMProvider, ProviderResult
from .claude import DEFAULT_SYSTEM

XAI_BASE_URL = "https://api.x.ai/v1"


class GrokProvider(LLMProvider):
    name = "grok"
    model = "grok-4"
    api_key_setting = "XAI_API_KEY"
    timeout_seconds = 40
    rpm = 30
    burst = 10

    def _call(self, *, prompt: str, system_prompt: str,
              region: str = "") -> ProviderResult:
        import openai

        from apps.llm_ranking.services.regions import region_system_instruction

        client = openai.OpenAI(api_key=self.api_key, base_url=XAI_BASE_URL)

        sys_prompt = system_prompt or DEFAULT_SYSTEM
        loc = region_system_instruction(region)
        if loc:
            sys_prompt = f"{sys_prompt}\n\n{loc}"

        if getattr(settings, "LLM_WEBSEARCH_ENABLED", False):
            result = self._web_search_call(client, prompt, sys_prompt)
            if result is not None:
                return result

        resp = client.chat.completions.create(
            model=self.model,
            max_tokens=4096,
            messages=[
                {"role": "system", "content": sys_prompt},
                {"role": "user", "content": prompt},
            ],
        )
        usage = getattr(resp, "usage", None)
        return ProviderResult(
            succeeded=True,
            text=resp.choices[0].message.content.strip(),
            input_tokens=getattr(usage, "prompt_tokens", 0) if usage else 0,
            output_tokens=getattr(usage, "completion_tokens", 0) if usage else 0,
        )

    def _web_search_call(self, client, prompt, sys_prompt):
        """Responses API with the web_search tool. None -> fall back to chat."""
        from apps.llm_ranking.providers.openai_compat import (
            extract_responses_citations,
        )

        try:
            resp = client.responses.create(
                model=self.model,
                instructions=sys_prompt,
                input=prompt,
                tools=[{"type": "web_search"}],
                max_output_tokens=1024,
            )
            usage = getattr(resp, "usage", None)
            return ProviderResult(
                succeeded=True,
                text=(getattr(resp, "output_text", "") or "").strip(),
                input_tokens=getattr(usage, "input_tokens", 0) if usage else 0,
                output_tokens=getattr(usage, "output_tokens", 0) if usage else 0,
                citations=extract_responses_citations(resp),
            )
        except Exception:
            return None

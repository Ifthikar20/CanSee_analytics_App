"""Perplexity provider (web-grounded Sonar models via REST)."""
import requests

from .base import LLMProvider, ProviderResult
from .claude import DEFAULT_SYSTEM


class PerplexityProvider(LLMProvider):
    name = "perplexity"
    # The llama-3.1-sonar-* family was retired by Perplexity in early 2025
    # (requests 400 with "invalid model"); "sonar" is its replacement.
    model = "sonar"
    api_key_setting = "PERPLEXITY_API_KEY"
    timeout_seconds = 30
    # Cap well under Perplexity's rate limit for headroom.
    rpm = 30
    burst = 10

    def _call(self, *, prompt: str, system_prompt: str,
              region: str = "") -> ProviderResult:
        from apps.llm_ranking.services.regions import get_region

        body: dict = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": system_prompt or DEFAULT_SYSTEM},
                {"role": "user", "content": prompt},
            ],
            "max_tokens": 4096,
        }
        # Perplexity respects ``web_search_options.user_location`` and
        # biases the web grounding step toward that country. The other
        # providers ignore region entirely (their behaviour is purely
        # prompt-driven).
        region_obj = get_region(region) if region else None
        if region_obj and region_obj.perplexity_country:
            body["web_search_options"] = {
                "user_location": {"country": region_obj.perplexity_country},
            }
        resp = requests.post(
            "https://api.perplexity.ai/chat/completions",
            headers={
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json",
            },
            json=body,
            timeout=self.timeout_seconds,
        )
        resp.raise_for_status()
        body = resp.json()
        usage = body.get("usage") or {}
        # Perplexity reports its sources out-of-band: legacy responses in a
        # top-level "citations" list of URLs, newer ones in "search_results"
        # dicts. The answer text only has [1][2] markers, so these fields
        # are the only route to real source URLs.
        citations = [u for u in (body.get("citations") or []) if isinstance(u, str)]
        if not citations:
            citations = [
                row.get("url")
                for row in (body.get("search_results") or [])
                if isinstance(row, dict) and row.get("url")
            ]
        from .passages import marker_passages

        text = body["choices"][0]["message"]["content"].strip()
        results = [r for r in (body.get("search_results") or []) if isinstance(r, dict)]
        if not results:
            # Legacy shape: the [n] markers index the plain URL list.
            results = [{"url": u} for u in citations]
        return ProviderResult(
            succeeded=True,
            text=text,
            input_tokens=int(usage.get("prompt_tokens", 0)),
            output_tokens=int(usage.get("completion_tokens", 0)),
            raw=body,
            citations=citations,
            # Sonar lists its sources only when it searched the web.
            searched=bool(citations),
            passages=marker_passages(text, results),
        )

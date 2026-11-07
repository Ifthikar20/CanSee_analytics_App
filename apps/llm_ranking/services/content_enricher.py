"""
Content Enricher — Multi-URL scanning + Google context for LLM ranking.

Combines the website's main page, user-provided URLs (blogs, product pages),
and Google Search results into a rich context payload that Claude can use
to evaluate and rank the business with real data.
"""
import logging
from concurrent.futures import ThreadPoolExecutor, as_completed
from urllib.parse import urlparse

from django.conf import settings

from apps.llm_ranking.services.domain_scanner import scan_domain

logger = logging.getLogger("apps")

MAX_EXTRA_URLS = 5
SCAN_POOL_SIZE = 3  # parallel HTTP workers


class ContentEnricher:
    """Aggregate content from multiple sources for LLM evaluation."""

    # ── Multi-URL scanning ─────────────────────────────────────────────

    @staticmethod
    def scan_urls(urls: list[str]) -> list[dict]:
        """
        Scan multiple URLs in parallel and return structured summaries.

        Each result dict:
            {
                "url": str,
                "success": bool,
                "title": str,
                "summary": str,       # condensed content (≤400 chars)
                "content": str,       # full content_summary from domain_scanner
                "products": [str],
                "features": [str],
                "selling_points": [str],
                "error": str | None,
            }
        """
        urls = urls[:MAX_EXTRA_URLS]
        results = []

        def _scan_one(url: str) -> dict:
            scan = scan_domain(url)
            return {
                "url": url,
                "success": scan["success"],
                "title": scan.get("business_name", ""),
                "summary": (scan.get("content_summary") or "")[:400],
                "content": scan.get("content_summary", ""),
                "products": scan.get("products", []),
                "features": scan.get("features", []),
                "selling_points": scan.get("selling_points", []),
                "error": scan.get("error"),
            }

        with ThreadPoolExecutor(max_workers=SCAN_POOL_SIZE) as pool:
            futures = {pool.submit(_scan_one, u): u for u in urls}
            for future in as_completed(futures):
                try:
                    results.append(future.result())
                except Exception as e:
                    url = futures[future]
                    logger.warning("URL scan failed for %s: %s", url, e)
                    results.append({
                        "url": url, "success": False, "title": "",
                        "summary": "", "content": "", "products": [],
                        "features": [], "selling_points": [],
                        "error": str(e)[:200],
                    })

        # Preserve original order
        url_order = {u: i for i, u in enumerate(urls)}
        results.sort(key=lambda r: url_order.get(r["url"], 999))
        return results

    # ── Google Search enrichment ───────────────────────────────────────

    @staticmethod
    def google_search_context(
        business_name: str,
        industry: str,
        domain: str = "",
    ) -> dict:
        """
        Search Google for the business and extract what the web says.

        Returns:
            {
                "snippets": [str],        # search result descriptions
                "competitors": [dict],    # {name, domain}
                "search_queries": [str],  # queries used
            }
        """
        api_key = getattr(settings, "GOOGLE_SEARCH_API_KEY", "")
        cx = getattr(settings, "GOOGLE_SEARCH_ENGINE_ID", "")

        result = {"snippets": [], "competitors": [], "search_queries": []}

        if not api_key or not cx:
            logger.info("Google Search not configured — skipping enrichment")
            return result

        import requests

        queries = [f"{business_name} review"]
        if industry:
            queries.append(f"best {industry} tools {business_name}")
        result["search_queries"] = queries

        own_domain = _normalize_domain(domain) if domain else ""

        for query in queries:
            try:
                resp = requests.get(
                    "https://www.googleapis.com/customsearch/v1",
                    params={"key": api_key, "cx": cx, "q": query, "num": 5},
                    timeout=8,
                )
                resp.raise_for_status()
                items = resp.json().get("items", [])
                for item in items:
                    snippet = item.get("snippet", "").strip()
                    if snippet:
                        result["snippets"].append(snippet)
                    # Extract competitor info
                    link_domain = _normalize_domain(
                        urlparse(item.get("link", "")).netloc
                    )
                    if link_domain and link_domain != own_domain:
                        title = item.get("title", "").split("|")[0].split("–")[0].strip()
                        if title and len(title) > 2:
                            result["competitors"].append({
                                "name": title[:100],
                                "domain": link_domain,
                            })
            except Exception as e:
                logger.warning("Google search failed for '%s': %s", query, e)

        # Deduplicate competitors
        seen = set()
        deduped = []
        for c in result["competitors"]:
            if c["domain"] not in seen:
                seen.add(c["domain"])
                deduped.append(c)
        result["competitors"] = deduped[:8]
        result["snippets"] = result["snippets"][:10]

        return result

    # ── Build combined LLM context ─────────────────────────────────────

    @staticmethod
    def build_llm_context(
        *,
        website_scan: dict,
        extra_scans: list[dict] | None = None,
        google_context: dict | None = None,
        business_name: str = "",
        industry: str = "",
    ) -> str:
        """
        Combine all sources into a rich context block for Claude.

        This is injected into the system prompt so Claude has real data
        about the business when evaluating its ranking.
        """
        sections = []

        # ── Main website ──
        if website_scan.get("success"):
            sections.append("=== MAIN WEBSITE ===")
            sections.append(website_scan.get("content_summary", ""))
            prods = website_scan.get("products", [])
            if prods:
                sections.append(f"Products/Services: {', '.join(prods[:6])}")
            feats = website_scan.get("features", [])
            if feats:
                sections.append(f"Features: {', '.join(feats[:8])}")
            sps = website_scan.get("selling_points", [])
            if sps:
                sections.append(f"Value Propositions: {', '.join(sps[:5])}")

        # ── Extra URLs (blogs, product pages) ──
        if extra_scans:
            successful = [s for s in extra_scans if s.get("success")]
            if successful:
                sections.append("")
                sections.append("=== ADDITIONAL PAGES ===")
                for scan in successful[:MAX_EXTRA_URLS]:
                    url_short = scan["url"][:80]
                    title = scan.get("title", "Untitled")
                    content = scan.get("content", "")[:300]
                    sections.append(f"\n--- {title} ({url_short}) ---")
                    if content:
                        sections.append(content)
                    sps = scan.get("selling_points", [])
                    if sps:
                        sections.append(f"Key points: {'; '.join(sps[:3])}")

        # ── Google Search context ──
        if google_context and google_context.get("snippets"):
            sections.append("")
            sections.append("=== WHAT GOOGLE SAYS ===")
            for i, snippet in enumerate(google_context["snippets"][:5], 1):
                sections.append(f"{i}. {snippet}")

            comps = google_context.get("competitors", [])
            if comps:
                names = [c["name"] for c in comps[:5]]
                sections.append(f"\nCompetitors found via Google: {', '.join(names)}")

        return "\n".join(sections)

    # ── All-in-one enrichment ──────────────────────────────────────────

    @classmethod
    def enrich(
        cls,
        *,
        main_url: str,
        extra_urls: list[str] | None = None,
        business_name: str = "",
        industry: str = "",
        include_google: bool = True,
    ) -> dict:
        """
        Run the full enrichment pipeline.

        Returns:
            {
                "website_scan": dict,
                "extra_scans": [dict],
                "google_context": dict,
                "llm_context": str,      # the combined text for Claude
                "context_urls_meta": [dict],  # metadata to store on the audit
            }
        """
        # 1. Scan main website
        website_scan = scan_domain(main_url)
        if not business_name and website_scan.get("success"):
            business_name = website_scan.get("business_name", "")
        if not industry and website_scan.get("success"):
            industry = website_scan.get("industry", "")

        # 2. Scan extra URLs
        extra_scans = []
        if extra_urls:
            extra_scans = cls.scan_urls(extra_urls)

        # 3. Google Search
        google_context = {}
        if include_google:
            domain = urlparse(main_url).netloc if main_url.startswith("http") else ""
            google_context = cls.google_search_context(
                business_name=business_name,
                industry=industry,
                domain=domain,
            )

        # 4. Build combined context
        llm_context = cls.build_llm_context(
            website_scan=website_scan,
            extra_scans=extra_scans,
            google_context=google_context,
            business_name=business_name,
            industry=industry,
        )

        # 5. Build metadata for storage
        context_urls_meta = []
        for scan in extra_scans:
            context_urls_meta.append({
                "url": scan["url"],
                "title": scan.get("title", ""),
                "success": scan["success"],
                "summary": scan.get("summary", "")[:200],
            })

        return {
            "website_scan": website_scan,
            "extra_scans": extra_scans,
            "google_context": google_context,
            "llm_context": llm_context,
            "context_urls_meta": context_urls_meta,
        }


def _normalize_domain(domain: str) -> str:
    """Normalize a domain for comparison."""
    import re
    d = domain.lower().strip()
    d = re.sub(r"^(https?://)?", "", d)
    d = re.sub(r"^www\.", "", d)
    d = d.split("/")[0]
    return d

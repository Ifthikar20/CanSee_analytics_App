"""
Deep domain scanner for LLM Ranking audit setup.

Fetches a domain's homepage and performs deep content analysis:
  - Business name (from <title> or og:site_name)
  - Description (from meta description or og:description)
  - Industry hints (from meta keywords, whole-word heading/body matches;
    empty when nothing matches)
  - Products/services extracted from headings, features, nav
  - Key phrases and selling points from body content
"""

import logging
import re
from html.parser import HTMLParser
from urllib.parse import urlparse

import requests

logger = logging.getLogger("apps")


def _friendly_fetch_error(detail: str) -> str:
    """Turn an internal FetchError string into something a person can act on."""
    d = (detail or "").lower()
    if "unsafe url" in d or "private" in d or "blocked" in d:
        return "That address isn't a public website we can reach."
    if "timed out" in d or "timeout" in d:
        return "That site took too long to respond. Try again in a moment."
    if "content-type" in d:
        return "That address didn't return a web page."
    if "exceeds" in d:
        # Should no longer happen for scans (they truncate), kept as a net.
        return "That page was too large to read fully. Try again."
    if "name or service not known" in d or "nodename" in d or "resolve" in d:
        return "We couldn't find that domain. Check the spelling."
    if "connection" in d or "ssl" in d or "certificate" in d:
        return "We couldn't connect to that site. Check the address and try again."
    return "We couldn't read that site. Check the address and try again."

SCAN_TIMEOUT = 12  # seconds
MAX_BODY_SIZE = 800_000  # 800KB limit

# Common stop-words to filter out of content extraction
STOP_WORDS = frozenset([
    "the", "a", "an", "and", "or", "is", "are", "was", "were", "be", "been",
    "for", "to", "of", "in", "on", "at", "by", "with", "from", "as", "it",
    "this", "that", "we", "our", "you", "your", "they", "their", "its",
    "all", "can", "will", "get", "has", "have", "had", "do", "does",
    "about", "more", "most", "new", "just", "also", "into", "out",
    "up", "down", "over", "how", "what", "when", "where", "why", "who",
    "not", "no", "so", "but", "than", "then", "only", "very", "every",
    "home", "page", "website", "site", "click", "here", "learn",
    "us", "me", "my", "one", "two", "use", "try", "see", "let",
    "free", "start", "sign", "log", "menu", "close", "open",
])

# Words that indicate product/service features (used for extraction)
FEATURE_SIGNALS = frozenset([
    "platform", "tool", "software", "solution", "service", "app",
    "dashboard", "analytics", "tracking", "automation", "integration",
    "api", "management", "monitoring", "reporting", "engine",
    "system", "suite", "module", "feature", "capability",
])


class DeepExtractor(HTMLParser):
    """Parse HTML to extract comprehensive content for business analysis."""

    def __init__(self):
        super().__init__()
        self.title = ""
        self.meta_description = ""
        self.meta_keywords = ""
        self.og_site_name = ""
        self.og_description = ""
        self.og_title = ""
        self.h1_texts = []
        self.h2_texts = []
        self.h3_texts = []
        self.nav_items = []
        self.list_items = []
        self.strong_texts = []
        self.button_texts = []
        self.body_text_chunks = []
        self._tag_stack = []
        # Parallel to _tag_stack: True where the element (or an ancestor) is
        # hidden. Hidden text is a prompt-injection channel — it is invisible
        # to a human reviewing the page but harvested and fed to the model —
        # so it is dropped here, at the HTML layer, where CSS-hidden content
        # is still detectable.
        self._hidden_stack = []
        self._in_title = False
        self._in_h1 = False
        self._in_h2 = False
        self._in_h3 = False
        self._in_nav = False
        self._in_li = False
        self._in_strong = False
        self._in_button = False
        self._in_body = False
        self._in_script = False
        self._in_style = False
        self._in_footer = False

    @staticmethod
    def _is_hidden_element(tag, attrs_dict) -> bool:
        """True if this element hides its text (so it must not be harvested)."""
        if tag in ("template", "noscript"):
            return True
        if "hidden" in attrs_dict:  # boolean ``hidden`` attribute
            return True
        if (attrs_dict.get("aria-hidden") or "").lower() == "true":
            return True
        style = (attrs_dict.get("style") or "").lower().replace(" ", "")
        if "display:none" in style or "visibility:hidden" in style:
            return True
        # opacity:0 / font-size:0, but not 0.5 / 0.9rem (visible).
        return bool(re.search(r"(?:opacity|font-size):0(?![.0-9])", style))

    def handle_starttag(self, tag, attrs):
        attrs_dict = dict(attrs)
        self._tag_stack.append(tag)
        parent_hidden = bool(self._hidden_stack and self._hidden_stack[-1])
        self._hidden_stack.append(parent_hidden or self._is_hidden_element(tag, attrs_dict))

        if tag == "title":
            self._in_title = True
        elif tag == "h1":
            self._in_h1 = True
        elif tag == "h2":
            self._in_h2 = True
        elif tag == "h3":
            self._in_h3 = True
        elif tag == "nav":
            self._in_nav = True
        elif tag == "li":
            self._in_li = True
        elif tag in ("strong", "b"):
            self._in_strong = True
        elif tag in ("button", "a") and attrs_dict.get("class", ""):
            cls = attrs_dict.get("class", "").lower()
            if "btn" in cls or "button" in cls or "cta" in cls:
                self._in_button = True
        elif tag == "body":
            self._in_body = True
        elif tag == "script":
            self._in_script = True
        elif tag == "style":
            self._in_style = True
        elif tag == "footer":
            self._in_footer = True
        elif tag == "meta":
            name = (attrs_dict.get("name") or "").lower()
            prop = (attrs_dict.get("property") or "").lower()
            content = attrs_dict.get("content", "")
            if name == "description":
                self.meta_description = content
            elif name == "keywords":
                self.meta_keywords = content
            elif prop == "og:site_name":
                self.og_site_name = content
            elif prop == "og:description":
                self.og_description = content
            elif prop == "og:title":
                self.og_title = content

    def handle_endtag(self, tag):
        if tag == "title":
            self._in_title = False
        elif tag == "h1":
            self._in_h1 = False
        elif tag == "h2":
            self._in_h2 = False
        elif tag == "h3":
            self._in_h3 = False
        elif tag == "nav":
            self._in_nav = False
        elif tag == "li":
            self._in_li = False
        elif tag in ("strong", "b"):
            self._in_strong = False
        elif tag in ("button", "a"):
            self._in_button = False
        elif tag == "script":
            self._in_script = False
        elif tag == "style":
            self._in_style = False
        elif tag == "footer":
            self._in_footer = False

        if self._tag_stack and self._tag_stack[-1] == tag:
            self._tag_stack.pop()
            if self._hidden_stack:
                self._hidden_stack.pop()

    def handle_data(self, data):
        text = data.strip()
        if not text or len(text) < 2:
            return
        if self._in_script or self._in_style:
            return
        # Drop text inside hidden elements — a prompt-injection channel.
        if self._hidden_stack and self._hidden_stack[-1]:
            return

        if self._in_title:
            self.title += text
        if self._in_h1 and not self._in_footer:
            self.h1_texts.append(text)
        if self._in_h2 and not self._in_footer:
            self.h2_texts.append(text)
        if self._in_h3 and not self._in_footer:
            self.h3_texts.append(text)
        if self._in_nav:
            self.nav_items.append(text)
        if self._in_li and not self._in_footer and not self._in_nav:
            self.list_items.append(text)
        if self._in_strong and not self._in_footer:
            self.strong_texts.append(text)
        if self._in_button:
            self.button_texts.append(text)
        if self._in_body and not self._in_script and not self._in_style:
            self.body_text_chunks.append(text)


def _clean_title_to_name(title: str) -> str:
    """Extract a brand name from a page title like 'Acme - The Best Tool'."""
    if not title:
        return ""
    parts = re.split(r"\s*[\|–—-]\s*", title)
    if len(parts) > 1:
        candidates = [p.strip() for p in parts if len(p.strip()) > 1]
        if candidates:
            return min(candidates, key=len)
    return title.strip()


def _extract_key_phrases(texts: list[str], max_items: int = 15) -> list[str]:
    """Extract meaningful phrases from a list of text chunks."""
    phrases = []
    seen = set()
    for text in texts:
        # Clean the text
        clean = re.sub(r"\s+", " ", text).strip()
        if not clean or len(clean) < 4 or len(clean) > 120:
            continue
        key = clean.lower()
        if key in seen:
            continue
        # Skip if it's just numbers or very short
        if re.match(r"^[\d\s%$.,]+$", clean):
            continue
        # Skip navigation-type items
        if key in ("home", "about", "contact", "blog", "login", "sign up",
                    "sign in", "menu", "close", "privacy", "terms",
                    "cookie", "subscribe", "newsletter"):
            continue
        seen.add(key)
        phrases.append(clean)
        if len(phrases) >= max_items:
            break
    return phrases


def _extract_products_and_features(parser: DeepExtractor) -> dict:
    """
    Analyze parsed HTML to extract products, services, features, and
    key selling points.
    """
    products = []
    features = []
    selling_points = []

    # H1 usually contains the main value proposition
    for h1 in parser.h1_texts[:3]:
        selling_points.append(h1)

    # H2s often describe features or product sections
    for h2 in parser.h2_texts[:10]:
        clean = h2.strip()
        if len(clean) > 3:
            lower = clean.lower()
            # Check if it describes a product/feature
            if any(sig in lower for sig in FEATURE_SIGNALS):
                products.append(clean)
            else:
                features.append(clean)

    # H3s provide more detail
    for h3 in parser.h3_texts[:10]:
        clean = h3.strip()
        if len(clean) > 3:
            features.append(clean)

    # List items often contain feature descriptions
    meaningful_items = _extract_key_phrases(parser.list_items, 8)
    features.extend(meaningful_items)

    # Strong/bold text often highlights key features
    for s in parser.strong_texts[:8]:
        clean = s.strip()
        if len(clean) > 4 and len(clean) < 80:
            selling_points.append(clean)

    return {
        "products": _extract_key_phrases(products, 6),
        "features": _extract_key_phrases(features, 10),
        "selling_points": _extract_key_phrases(selling_points, 6),
    }


def _build_content_summary(
    parser: DeepExtractor,
    analysis: dict,
    name: str,
    description: str,
) -> str:
    """Build a comprehensive content summary from all extracted data."""
    parts = []

    if name:
        parts.append(f"Business: {name}")
    if description:
        parts.append(f"Description: {description}")

    if analysis["selling_points"]:
        parts.append(f"Key Value Props: {'; '.join(analysis['selling_points'][:4])}")

    if analysis["products"]:
        parts.append(f"Products/Services: {'; '.join(analysis['products'][:5])}")

    if analysis["features"]:
        parts.append(f"Features: {'; '.join(analysis['features'][:8])}")

    # Add first meaningful body text paragraphs
    body_paragraphs = [
        chunk for chunk in parser.body_text_chunks
        if len(chunk) > 40 and not chunk.startswith(("{", "<", "//"))
    ][:5]
    if body_paragraphs:
        parts.append(f"Page Content: {' '.join(body_paragraphs[:3])[:400]}")

    return "\n".join(parts)


def scan_domain(url: str) -> dict:
    """
    Deep-scan a domain and return comprehensive business information.

    Returns:
        {
            "success": bool,
            "business_name": str,
            "description": str,
            "industry": str,
            "domain": str,
            "content_summary": str,   # rich content for LLM prompt
            "products": [str],         # extracted product/service names
            "features": [str],         # extracted features
            "selling_points": [str],   # key value propositions
            "error": str | None,
        }
    """
    # Normalize URL
    if not url.startswith("http"):
        url = f"https://{url}"

    parsed = urlparse(url)
    domain = parsed.netloc or parsed.path.split("/")[0]

    result = {
        "success": False,
        "business_name": "",
        "description": "",
        "industry": "",
        "domain": domain,
        "url": url,
        "content_summary": "",
        "products": [],
        "features": [],
        "selling_points": [],
        "error": None,
    }

    try:
        # All HTTP fetches go through ``safe_get`` — SSRF guard, redirect
        # re-validation, content-type allowlist, and a streaming body cap.
        from core.validators.safe_http import FetchError, safe_get
        try:
            resp = safe_get(
                url,
                timeout=SCAN_TIMEOUT,
                max_bytes=MAX_BODY_SIZE,
                # Everything this scanner needs (title, meta/og tags, hero
                # copy) is at the top of the document, so a large page is
                # truncated rather than rejected. Big storefronts routinely
                # ship more than the cap in inline HTML/JSON.
                truncate=True,
                headers={
                    "User-Agent": (
                        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                        "AppleWebKit/537.36 (KHTML, like Gecko) "
                        "Chrome/120.0.0.0 Safari/537.36"
                    ),
                    "Accept": "text/html,application/xhtml+xml",
                    "Accept-Language": "en-US,en;q=0.9",
                },
            )
        except FetchError as exc:
            # This message is shown to a person mid-onboarding, so it must
            # say what to do next — never the raw internal reason (a user
            # once saw "response body exceeds 800000 bytes"). The detail
            # stays in the logs for us.
            logger.warning("domain scan fetch failed for %s: %s", url, exc)
            result["error"] = _friendly_fetch_error(str(exc))
            return result
        url = resp.final_url
        if resp.status_code >= 400:
            result["error"] = f"HTTP error: {resp.status_code}"
            return result

        html = resp.text[:MAX_BODY_SIZE]

        parser = DeepExtractor()
        parser.feed(html)

        # Extract business name
        name = (
            parser.og_site_name
            or _clean_title_to_name(parser.og_title or parser.title)
            or domain.split(".")[0].capitalize()
        )

        # Extract description
        description = (
            parser.og_description
            or parser.meta_description
            or ""
        )
        # If no meta description, build one from h1 + body text
        if not description and parser.h1_texts:
            h1 = parser.h1_texts[0]
            body_start = " ".join(parser.body_text_chunks[:10])[:200]
            description = f"{h1}. {body_start}".strip()

        if len(description) > 500:
            description = description[:497] + "..."

        # Extract industry hints from meta keywords + body
        body_text = " ".join(parser.body_text_chunks)
        industry = _extract_industry_from_content(
            parser.meta_keywords, body_text, parser.h1_texts, parser.h2_texts
        )

        # Deep content analysis
        analysis = _extract_products_and_features(parser)

        # Build content summary for LLM context
        content_summary = _build_content_summary(
            parser, analysis, name, description
        )

        result["success"] = True
        result["business_name"] = name[:200]
        result["description"] = description
        result["industry"] = industry[:100]
        result["content_summary"] = content_summary
        result["products"] = analysis["products"]
        result["features"] = analysis["features"]
        result["selling_points"] = analysis["selling_points"]

    except requests.exceptions.Timeout:
        result["error"] = "Domain took too long to respond."
    except requests.exceptions.ConnectionError:
        result["error"] = "Could not connect to domain."
    except requests.exceptions.HTTPError as e:
        result["error"] = f"HTTP error: {e.response.status_code}"
    except Exception as e:
        logger.warning("Domain scan error for %s: %s", url, str(e))
        result["error"] = f"Scan failed: {str(e)[:100]}"

    return result


def _extract_industry_from_content(
    keywords: str,
    body_text: str,
    h1_texts: list[str],
    h2_texts: list[str],
) -> str:
    """Infer an industry from meta keywords, headings, and body text.

    Heading and body matches are whole-word. Returns "" when nothing
    matches: an unknown industry stays unknown.
    """
    # Use meta keywords first
    if keywords:
        kw_list = [k.strip() for k in keywords.split(",") if k.strip()]
        meaningful = [
            k for k in kw_list
            if k.lower() not in STOP_WORDS and len(k) > 2
        ]
        if meaningful:
            return ", ".join(meaningful[:3])

    # Try to extract from headings
    all_headings = " ".join(h1_texts[:2] + h2_texts[:4]).lower()
    industry_keywords = [
        ("analytics", "Analytics"),
        ("marketing", "Marketing"),
        ("ecommerce", "E-commerce"), ("e-commerce", "E-commerce"),
        ("fintech", "Fintech"), ("finance", "Finance"),
        ("healthcare", "Healthcare"), ("health", "Health"),
        ("education", "Education"), ("learning", "EdTech"),
        ("real estate", "Real Estate"), ("property", "Real Estate"),
        ("cybersecurity", "Cybersecurity"), ("security", "Security"),
        ("crm", "CRM"), ("sales", "Sales"),
        ("hr", "HR"), ("recruiting", "Recruiting"), ("hiring", "HR"),
        ("project management", "Project Management"),
        ("design", "Design"), ("creative", "Creative"),
        ("developer", "Developer Tools"), ("devops", "DevOps"),
        ("ai", "AI/ML"), ("machine learning", "AI/ML"),
        ("data", "Data"), ("database", "Data"),
        ("communication", "Communication"),
        ("collaboration", "Collaboration"),
        ("automation", "Automation"),
        ("logistics", "Logistics"), ("supply chain", "Supply Chain"),
        ("insurance", "Insurance"),
        ("travel", "Travel"), ("hospitality", "Hospitality"),
        ("food", "Food & Beverage"), ("restaurant", "Food & Beverage"),
        ("fitness", "Fitness"), ("wellness", "Wellness"),
        ("saas", "SaaS"), ("software", "Software"),
    ]
    for keyword, industry in industry_keywords:
        if _has_word(keyword, all_headings):
            return industry

    # Fallback: check body text
    text_lower = body_text[:2000].lower() if body_text else ""
    for keyword, industry in industry_keywords:
        if _has_word(keyword, text_lower):
            return industry

    return ""


def _has_word(phrase: str, text: str) -> bool:
    """True when ``phrase`` occurs in ``text`` as whole words."""
    return re.search(rf"(?<![a-z0-9]){re.escape(phrase)}(?![a-z0-9])", text) is not None

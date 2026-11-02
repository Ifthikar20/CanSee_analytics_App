"""DEBUG only: a demo project with search-vs-AI data for every gap class.

Lets the dashboard and prompt pages be exercised locally without SerpAPI
or LLM provider keys. Everything is fictional ("Northwind CRM" on a
.example domain) and written with bulk_create, so no post-save hooks
(citation extraction, alignment scoring) fire.

    python manage.py seed_search_gap_demo --email you@example.com [--replace]
"""

import math
from datetime import timedelta
from decimal import Decimal

from django.conf import settings
from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.utils import timezone

from apps.citations.models import Citation, SourceClass
from apps.citations.services.url_normalizer import normalize_url
from apps.llm_ranking.models import LLMRankingAudit, LLMRankingResult
from apps.prompt_library.models import BrandPrompt, Industry, Prompt, PromptSource
from apps.prompt_library.services._hash import text_hash
from apps.search_gap.models import GoogleRankCheck
from apps.websites.models import Website

SITE = "https://www.northwind-crm.example"
BRAND = "Northwind CRM"
COMPETITORS = [
    {"name": "HubSpot", "domain": "hubspot.com"},
    {"name": "Pipedrive", "domain": "pipedrive.com"},
    {"name": "Zoho CRM", "domain": "zoho.com"},
    {"name": "Salesforce", "domain": "salesforce.com"},
]
OTHER_BRANDS = ["Freshsales", "Copper", "Capsule", "Less Annoying CRM", "Nimble", "Insightly"]
THIRD_PARTY = [
    "https://www.g2.com/categories/crm",
    "https://www.capterra.com/customer-relationship-management-software/",
    "https://www.reddit.com/r/smallbusiness/comments/demo01/which_crm_do_you_use/",
    "https://www.forbes.com/advisor/business/software/best-crm-small-business/",
    "https://www.pcmag.com/picks/the-best-crm-software",
    "https://zapier.com/blog/best-crm-app/",
]
COMPETITOR_PAGES = [
    "https://www.hubspot.com/products/crm",
    "https://www.pipedrive.com/en/features",
    "https://www.zoho.com/crm/",
    "https://www.salesforce.com/crm/",
]
ENGINES = ("gpt4", "claude", "gemini", "grok", "perplexity")

# (prompt, Google position or None, your ranking path, ranks per engine
# over three runs; None = not mentioned in that run). Together they cover
# AI ahead, Google ahead, strong in both, invisible in both, and even.
SCENARIOS = [
    ("best crm for small business", 94, "/blog/small-business-crm", {
        "gpt4": [2, 2, 3], "claude": [3, 3, 2], "gemini": [None, None, None],
        "grok": [5, 4, None], "perplexity": [4, 3, 4]}),
    ("crm with built-in email automation", 4, "/features/email-automation", {
        "gpt4": [None, None, None], "claude": [None, None, None], "gemini": [None, None, None],
        "grok": [None, None, None], "perplexity": [None, 9, None]}),
    ("hubspot alternatives for startups", 2, "/compare/hubspot-alternatives", {
        "gpt4": [1, 1, 2], "claude": [2, 1, 2], "gemini": [1, 2, 1],
        "grok": [3, 2, 3], "perplexity": [2, 2, 1]}),
    ("how to choose a crm", None, "", {
        "gpt4": [None, None, None], "claude": [None, None, None], "gemini": [None, None, None],
        "grok": [None, None, None], "perplexity": [None, None, None]}),
    ("affordable crm for real estate agents", 12, "/industries/real-estate", {
        "gpt4": [4, 5, 4], "claude": [6, 5, 6], "gemini": [5, None, 6],
        "grok": [None, 6, 5], "perplexity": [1, 1, 2]}),
    ("crm pricing comparison", 7, "/pricing", {
        "gpt4": [None, None, None], "claude": [6, None, 7], "gemini": [None, None, None],
        "grok": [None, None, None], "perplexity": [8, None, None]}),
    ("simple crm for freelancers", 31, "/solutions/freelancers", {
        "gpt4": [2, 3, 2], "claude": [3, 2, 3], "gemini": [3, 3, None],
        "grok": [2, 2, 3], "perplexity": [3, 4, 3]}),
    ("crm that integrates with gmail", 3, "/integrations/gmail", {
        "gpt4": [9, None, 10], "claude": [None, None, None], "gemini": [8, 9, None],
        "grok": [None, None, None], "perplexity": [10, None, None]}),
]
RUN_AGES_DAYS = (15, 8, 1)


def _brand_list(rank, offset=0):
    """Brands in answer order, with ours at ``rank`` (or absent).

    The category leader (first competitor) leads every list; ``offset``
    rotates the rest and varies the list length, so share of voice differs
    from brand to brand the way it does in real answers.
    """
    leader, *rest = [c["name"] for c in COMPETITORS] + OTHER_BRANDS
    shift = offset % len(rest)
    names = [leader, *rest[shift:], *rest[:shift]]
    length = max(4 + offset % 4, rank or 0)
    listed = names[: length - (1 if rank else 0)]
    if rank:
        listed.insert(rank - 1, BRAND)
    return listed


def _domain(url: str) -> tuple[str, str]:
    _, host, apex = normalize_url(url)
    return host, apex


class Command(BaseCommand):
    help = "DEBUG only: create a demo project with search-vs-AI gap data."

    def add_arguments(self, parser):
        parser.add_argument("--email", required=True, help="Existing user who owns the demo project.")
        parser.add_argument("--replace", action="store_true", help="Wipe and re-seed the demo project.")

    def handle(self, *args, email, replace, **options):
        if not settings.DEBUG:
            raise CommandError("Refusing to seed demo data with DEBUG off.")
        user = get_user_model().objects.filter(email__iexact=email).first()
        if user is None:
            raise CommandError(f"No user with email {email}. Create one first.")

        with transaction.atomic():
            website = Website.objects.filter(user=user, url=SITE).first()
            if website and not replace:
                raise CommandError("The demo project already exists. Pass --replace to rebuild it.")
            if website:
                GoogleRankCheck.objects.filter(website=website).delete()
                LLMRankingAudit.objects.filter(website=website).delete()
                BrandPrompt.objects.filter(website=website).delete()
            else:
                website = Website.objects.create(
                    user=user, url=SITE, name=BRAND, industry="CRM software",
                    description="Simple CRM for small teams.", competitors=COMPETITORS,
                )
            counts = self._seed(website, user)

        self.stdout.write(self.style.SUCCESS(
            f"Seeded {BRAND}: {counts['prompts']} prompts, {counts['answers']} AI answers, "
            f"{counts['citations']} citations, {counts['checks']} Google checks."
        ))

    def _seed(self, website, user) -> dict:
        now = timezone.now()
        industry, _ = Industry.objects.get_or_create(
            slug="demo-crm", defaults={"name": "CRM (demo)", "is_active": True},
        )
        counts = {"prompts": 0, "answers": 0, "citations": 0, "checks": 0}
        for index, (text, position, path, ranks) in enumerate(SCENARIOS):
            prompt, _ = Prompt.objects.get_or_create(
                text_hash=text_hash(text),
                defaults=dict(
                    industry=industry, text=text, template_text=text,
                    intent_bucket="category", style="question", source=PromptSource.MANUAL,
                ),
            )
            brand_prompt, _ = BrandPrompt.objects.get_or_create(
                website=website, prompt=prompt, defaults={"location": "us"},
            )
            counts["prompts"] += 1
            own_url = f"{SITE}{path}" if path else ""

            for run, age in enumerate(RUN_AGES_DAYS):
                ran_at = now - timedelta(days=age)
                audit = LLMRankingAudit.objects.create(
                    website=website, created_by=user, status=LLMRankingAudit.STATUS_COMPLETED,
                    prompts=[text], business_name=BRAND, providers_queried=list(ENGINES),
                    completed_at=ran_at,
                )
                results = [
                    self._result(
                        audit, prompt, text, engine, ranks[engine][run], run,
                        offset=3 * index + ENGINES.index(engine) + run,
                    )
                    for engine in ENGINES
                ]
                LLMRankingResult.objects.bulk_create(results)
                LLMRankingResult.objects.filter(audit=audit).update(created_at=ran_at)
                counts["answers"] += len(results)
                citations = []
                for result in results:
                    citations.extend(self._citations(result, own_url))
                Citation.objects.bulk_create(citations)
                counts["citations"] += len(citations)

            GoogleRankCheck.objects.create(**self._google(website, brand_prompt, text, position, own_url, now))
            counts["checks"] += 1
        return counts

    def _result(self, audit, prompt, text, engine, rank, run, offset=0) -> LLMRankingResult:
        brands = _brand_list(rank, offset)
        answer = "\n".join(f"{i}. {name}" for i, name in enumerate(brands, start=1))
        return LLMRankingResult(
            audit=audit, provider=engine, prompt=text, prompt_index=0, run_id=run,
            response_text=f"Here are good options for {text}:\n{answer}",
            is_mentioned=rank is not None,
            mention_rank=rank,
            sentiment=(
                LLMRankingResult.SENTIMENT_NOT_MENTIONED if rank is None
                else LLMRankingResult.SENTIMENT_POSITIVE if rank <= 3
                else LLMRankingResult.SENTIMENT_NEUTRAL
            ),
            confidence_score=80.0,
            mention_context=(
                f"{BRAND} is a solid pick here: quick to set up, fairly priced, and easy for a small team to adopt."
                if rank else ""
            ),
            competitors_mentioned=[
                {"name": name, "position": i, "linked": False}
                for i, name in enumerate(brands, start=1) if name != BRAND
            ],
            source_prompt=prompt,
            extraction_model="demo",
        )

    def _citations(self, result, own_url) -> list[Citation]:
        offset = (ENGINES.index(result.provider) + len(result.prompt)) % len(THIRD_PARTY)
        urls = [THIRD_PARTY[(offset + i) % len(THIRD_PARTY)] for i in range(3)]
        # Engines that rank you near the top tend to cite your own page.
        if own_url and result.is_mentioned and result.mention_rank and result.mention_rank <= 3:
            urls.insert(0, own_url)
        rows = []
        for position, url in enumerate(urls, start=1):
            normalized, host, apex = normalize_url(url)
            rows.append(Citation(
                result=result, audit=result.audit, url=url, normalized_url=normalized,
                domain=host, apex_domain=apex,
                source_class=SourceClass.YOUR_SITE if url == own_url else SourceClass.OTHER,
                position=position, is_target=url == own_url,
            ))
        return rows

    def _google(self, website, brand_prompt, text, position, own_url, now) -> dict:
        pool = COMPETITOR_PAGES + THIRD_PARTY
        offset = len(text) % len(pool)
        top = [pool[(offset + i) % len(pool)] for i in range(10)]
        if position and position <= 10:
            top[position - 1] = own_url
        top_results = []
        for i, url in enumerate(top, start=1):
            host, _ = _domain(url)
            top_results.append({
                "position": i, "url": url, "domain": host[4:] if host.startswith("www.") else host,
                "title": f"{text.title()} - result {i}",
            })
        pages = math.ceil(position / 10) if position else 10
        return dict(
            website=website, brand_prompt=brand_prompt, query=text, location="us",
            depth=100, position=position, url=own_url if position else "",
            title=f"{BRAND}: {text}" if position else "", top_results=top_results,
            ai_overview={
                "shown": True, "mentions_you": bool(position and position <= 3),
                "cites_you": bool(position and position <= 3),
                "references": [{"url": u, "domain": _domain(u)[0], "title": ""} for u in THIRD_PARTY[:3]],
            },
            pages_fetched=pages,
            cost_usd=Decimal(str(settings.SERPAPI_COST_PER_SEARCH_USD)) * pages,
            checked_at=now - timedelta(days=1),
        )

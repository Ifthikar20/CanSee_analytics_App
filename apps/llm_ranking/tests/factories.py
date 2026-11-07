import factory
from factory.django import DjangoModelFactory

from apps.accounts.tests.factories import UserFactory
from apps.llm_ranking.models import LLMRankingAudit, LLMRankingResult
from apps.websites.tests.factories import WebsiteFactory


class LLMRankingAuditFactory(DjangoModelFactory):
    class Meta:
        model = LLMRankingAudit

    website = factory.SubFactory(WebsiteFactory)
    created_by = factory.SubFactory(UserFactory)
    business_name = "Acme SaaS"
    business_description = "A SaaS platform for growth analytics"
    industry = "SaaS"
    keywords = ["analytics", "growth", "dashboard"]
    prompts = [
        "What are the best SaaS analytics tools?",
        "Recommend a growth analytics platform",
    ]
    status = LLMRankingAudit.STATUS_PENDING


class LLMRankingResultFactory(DjangoModelFactory):
    class Meta:
        model = LLMRankingResult

    audit = factory.SubFactory(LLMRankingAuditFactory)
    provider = LLMRankingResult.PROVIDER_CLAUDE
    prompt = "What are the best SaaS analytics tools?"
    response_text = "1. Acme SaaS — great analytics platform\n2. Competitor X"
    is_mentioned = True
    mention_rank = 1
    sentiment = LLMRankingResult.SENTIMENT_POSITIVE
    confidence_score = 90.0
    mention_context = "1. Acme SaaS — great analytics platform"
    query_succeeded = True

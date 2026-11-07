"""factory_boy factories for the prompt_library models."""
from __future__ import annotations

import factory
from factory.django import DjangoModelFactory

from apps.prompt_library.models import (
    Industry,
    IntentBucket,
    Prompt,
    PromptSource,
)
from apps.prompt_library.services._hash import text_hash


class IndustryFactory(DjangoModelFactory):
    class Meta:
        model = Industry
        django_get_or_create = ("slug",)

    slug = factory.Sequence(lambda n: f"industry-{n}")
    name = factory.Sequence(lambda n: f"Industry {n}")
    is_active = True


class PromptFactory(DjangoModelFactory):
    class Meta:
        model = Prompt

    industry = factory.SubFactory(IndustryFactory)
    text = factory.Sequence(lambda n: f"What are the best widgets for case {n}?")
    intent_bucket = IntentBucket.CATEGORY
    source = PromptSource.LLM_SYNTH
    text_hash = factory.LazyAttribute(lambda o: text_hash(o.text))
    is_active = True

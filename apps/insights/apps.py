import importlib

from django.apps import AppConfig

# Each of these modules registers its run kind (services.runs.register) when
# it is imported. AgentConfig.ready() registers context_build the same way.
KIND_MODULES = (
    "apps.insights.services.answer_analysis",
    "apps.insights.services.prompt_ideas",
    "apps.insights.services.site_audit",
    "apps.insights.services.fanouts",
    "apps.insights.services.shopping",
)


class InsightsConfig(AppConfig):
    name = "apps.insights"
    label = "insights"
    verbose_name = "Insights"

    def ready(self):
        for module in KIND_MODULES:
            importlib.import_module(module)

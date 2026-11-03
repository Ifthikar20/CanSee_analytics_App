from django.apps import AppConfig
from django.apps import apps as django_apps


class AgentConfig(AppConfig):
    name = "apps.agent"
    label = "agent"
    verbose_name = "Agent workspace"

    def ready(self):
        from django.core import checks

        from apps.agent.conf import check_agent_settings

        checks.register(check_agent_settings)
        # Build from my site is a run kind of the insights runs lifecycle;
        # importing the module registers it (after every app has loaded).
        if django_apps.is_installed("apps.insights"):
            from apps.agent.services import context_build  # noqa: F401

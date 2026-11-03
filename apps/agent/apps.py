from django.apps import AppConfig


class AgentConfig(AppConfig):
    name = "apps.agent"
    label = "agent"
    verbose_name = "Agent workspace"

    def ready(self):
        from django.core import checks

        from apps.agent.conf import check_agent_settings

        checks.register(check_agent_settings)

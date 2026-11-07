"""Run Autopilot by hand (backend 4g). The owner runs it in the container;
agents never do: it asks the AI engines and Google for real.

    python manage.py run_autopilot --website <uuid> --dry-run   # plan and estimate, writes nothing
    python manage.py run_autopilot --website <uuid> [--no-text] # today's run, every stage inline
    python manage.py run_autopilot --dispatch                   # one dispatcher tick inline
"""

import json

from django.core.exceptions import ValidationError
from django.core.management.base import BaseCommand, CommandError
from django.db import IntegrityError

from apps.autopilot.models import AutopilotRun
from apps.autopilot.services import pipeline
from apps.autopilot.services import settings as autopilot_settings


class Command(BaseCommand):
    help = "Run a project's Autopilot now, or one dispatcher tick, in this process."

    def add_arguments(self, parser):
        parser.add_argument("--website", help="The project's id.")
        parser.add_argument("--dry-run", action="store_true",
                            help="Print the plan and the estimate; write nothing.")
        parser.add_argument("--no-text", action="store_true",
                            help="Run without sending the text.")
        parser.add_argument("--dispatch", action="store_true",
                            help="One dispatcher tick (what beat does every 5 minutes).")

    def _print(self, payload) -> None:
        self.stdout.write(json.dumps(payload, indent=2, default=str))

    @staticmethod
    def _plan(website) -> dict:
        """Which prompts a run would ask now, and which would wait."""
        from apps.billing.services import tracked_prompts

        autopilot = autopilot_settings.get_or_build(website)
        runnable, over = tracked_prompts.runnable_brand_prompts(website)
        plan = pipeline.make_plan(autopilot, website, runnable)
        fit = {bp.id for bp in plan["fit"]}
        return {
            "engines": [e["label"] for e in plan["engines"]],
            "ask": [bp.prompt.text for bp in runnable if bp.id in fit],
            "wait": [bp.prompt.text for bp in runnable if bp.id not in fit],
            "over_limit": over,
            "google": plan["google"],
            "skip": plan["skip"],
        }

    def handle(self, *args, **options):
        if options["dispatch"]:
            with pipeline.forced_mode("inline"):
                self._print(pipeline.dispatch_tick(mode="inline"))
            return
        if not options["website"]:
            raise CommandError("Pass --website <uuid>, or --dispatch.")
        from apps.websites.models import Website

        try:
            website = Website.objects.select_related("user").get(id=options["website"])
        except (Website.DoesNotExist, ValueError, ValidationError) as exc:
            raise CommandError("No project with this id.") from exc

        estimate = pipeline.run_estimate(website, website.user)
        if options["dry_run"]:
            self._print({"plan": self._plan(website), "estimate": estimate})
            return

        autopilot = autopilot_settings.get_or_build(website)
        day = autopilot_settings.local_today(autopilot)
        if AutopilotRun.objects.filter(website=website, local_date=day).exists():
            raise CommandError(pipeline.today_exists_message(autopilot))
        plan = {"confirmed": estimate["confirm"]}
        if options["no_text"]:
            plan["no_text"] = True
        try:
            run = AutopilotRun.objects.create(
                website=website, local_date=day, trigger=AutopilotRun.TRIGGER_MANUAL,
                status=AutopilotRun.STATUS_RUNNING, plan=plan,
            )
        except IntegrityError as exc:
            raise CommandError(pipeline.today_exists_message(autopilot)) from exc
        AutopilotRun.objects.filter(id=run.id).update(
            started_at=run.created_at, heartbeat_at=run.created_at,
        )
        with pipeline.forced_mode("inline"):
            pipeline.drive(run.id)
        run = AutopilotRun.objects.select_related("analysis_run").get(id=run.id)
        self._print({"run": pipeline.run_summary(run), "text": run.text_body})

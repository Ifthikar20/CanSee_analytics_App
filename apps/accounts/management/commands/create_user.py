"""Create an account that can sign in immediately.

The operator path for provisioning accounts while public sign-up is
closed (SIGNUPS_ENABLED=False). The email is marked verified. The
password is never a command-line argument, where it would land in shell
history and the process list: it is read from the environment variable
named by --password-env, or typed at a hidden prompt.

    manage.py create_user jane@acme.com "Jane Doe"
    manage.py create_user jane@acme.com "Jane Doe" --org acme
    manage.py create_user jane@acme.com "Jane Doe" --password-env NEW_USER_PASSWORD
"""
import getpass
import os
import sys

from django.conf import settings
from django.contrib.auth.password_validation import validate_password
from django.core.exceptions import ValidationError
from django.core.management.base import BaseCommand, CommandError
from django.core.validators import validate_email
from django.db import transaction

from apps.accounts.models import Organization, User
from core.logging.audit_logger import audit_log
from core.utils.constants import OrgRole


class Command(BaseCommand):
    help = "Create a user who can sign in immediately (email already verified)."

    def add_arguments(self, parser):
        parser.add_argument("email", help="Login email address")
        parser.add_argument("full_name", help='Full name, e.g. "Jane Doe"')
        parser.add_argument(
            "--org",
            default="",
            metavar="SLUG",
            help="Slug of an existing organization to join as a member",
        )
        parser.add_argument(
            "--password-env",
            default="",
            metavar="VAR",
            help="Read the password from this environment variable instead of prompting",
        )

    def handle(self, *args, **options):
        email = options["email"].strip().lower()
        full_name = options["full_name"].strip()
        try:
            validate_email(email)
        except ValidationError:
            raise CommandError(f"{options['email']!r} is not a valid email address.") from None
        if not full_name:
            raise CommandError("Full name is required.")
        if User.objects.filter(email__iexact=email).exists():
            raise CommandError(f"A user with email {email} already exists.")

        org = self._resolve_org(options["org"].strip())
        password = self._read_password(options["password_env"].strip())

        # create_user does not run AUTH_PASSWORD_VALIDATORS; sign-up does,
        # so this path must too.
        try:
            validate_password(password, User(email=email, full_name=full_name))
        except ValidationError as exc:
            raise CommandError(" ".join(exc.messages)) from None

        with transaction.atomic():
            user = User.objects.create_user(
                email=email,
                password=password,
                full_name=full_name,
                is_email_verified=True,
            )
            if org is not None:
                from apps.accounts.services.org_service import OrgService

                OrgService.add_member(
                    organization=org,
                    user=user,
                    role=OrgRole.MEMBER,
                    joined_via="admin",
                )

        audit_log(
            "user.registered",
            user=user,
            action="create",
            resource_type="user",
            resource_id=str(user.id),
            metadata={"method": "create_user_command"},
        )

        joined = f" and added to {org.name} as a member" if org is not None else ""
        self.stdout.write(self.style.SUCCESS(f"Created {user.email} (id {user.id}){joined}."))

    def _resolve_org(self, slug: str):
        if not slug:
            return None
        try:
            org = Organization.objects.get(slug=slug)
        except Organization.DoesNotExist:
            raise CommandError(f"No organization with slug {slug!r}.") from None

        # Seats are the enterprise billing unit; an operator-added member
        # takes one exactly like an accepted invitation does.
        from apps.billing.services.org_entitlements import seat_limit_for, seats_used

        cap = seat_limit_for(org)
        if cap > 0 and seats_used(org) >= cap:
            raise CommandError(f"{org.name} has no free seats ({cap} in use).")
        if not getattr(settings, "ORG_FEATURES_ENABLED", False):
            self.stdout.write(self.style.WARNING(
                "ORG_FEATURES_ENABLED is off: the membership is saved, but organization "
                "features stay hidden until the flag is turned on."
            ))
        return org

    def _read_password(self, env_var: str) -> str:
        if env_var:
            password = os.environ.get(env_var, "")
            if not password:
                raise CommandError(f"Environment variable {env_var} is not set or is empty.")
            return password

        if not sys.stdin.isatty():
            raise CommandError(
                "No terminal to prompt on. Run with a TTY, or pass --password-env VAR."
            )
        password = getpass.getpass("Password: ")
        if not password:
            raise CommandError("A password is required.")
        if getpass.getpass("Password (again): ") != password:
            raise CommandError("The passwords do not match.")
        return password

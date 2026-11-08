from django.contrib.auth.models import BaseUserManager


class UserManager(BaseUserManager):
    """Custom manager for email-based authentication (no username field).

    Emails are case-insensitive identities: they are stored trimmed and in
    lowercase, and sign-in (ModelBackend, Django admin) finds the account
    whatever case the email was typed in.
    """

    @classmethod
    def normalize_email(cls, email):
        return (email or "").strip().lower()

    def get_by_natural_key(self, username):
        email = (username or "").strip()
        matches = self.filter(**{f"{self.model.USERNAME_FIELD}__iexact": email})
        # Rows created before emails were lowercased may differ only in case;
        # an exact match wins, then the oldest account.
        user = (
            matches.filter(**{self.model.USERNAME_FIELD: email}).first()
            or matches.order_by("created_at").first()
        )
        if user is None:
            raise self.model.DoesNotExist
        return user

    def create_user(self, email: str, password: str = None, **extra_fields):
        if not email:
            raise ValueError("Email address is required.")
        email = self.normalize_email(email)
        user = self.model(email=email, **extra_fields)
        user.set_password(password)
        user.save(using=self._db)
        return user

    def create_superuser(self, email: str, password: str, **extra_fields):
        extra_fields.setdefault("is_staff", True)
        extra_fields.setdefault("is_superuser", True)
        extra_fields.setdefault("is_email_verified", True)
        return self.create_user(email, password, **extra_fields)

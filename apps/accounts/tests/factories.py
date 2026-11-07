import factory
from factory.django import DjangoModelFactory

from apps.accounts.models import User, UserProfile


class UserFactory(DjangoModelFactory):
    class Meta:
        model = User

    email = factory.Sequence(lambda n: f"user{n}@example.com")
    full_name = factory.Faker("name")
    company_name = factory.Faker("company")
    plan = "starter"
    is_active = True
    is_email_verified = True

    @factory.post_generation
    def password(obj, create, extracted, **kwargs):
        password = extracted or "TestPass123!"
        obj.set_password(password)
        if create:
            obj.save(update_fields=["password"])


class UserProfileFactory(DjangoModelFactory):
    class Meta:
        model = UserProfile

    user = factory.SubFactory(UserFactory)
    timezone = "UTC"

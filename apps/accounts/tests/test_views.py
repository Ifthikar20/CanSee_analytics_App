import pytest
from rest_framework.test import APIClient

from apps.accounts.tests.factories import UserFactory


@pytest.mark.django_db
class TestRegisterView:
    def test_register_success(self):
        client = APIClient()
        response = client.post(
            "/api/v1/auth/register/",
            {
                "email": "newuser@test.com",
                "password": "SecurePass123!",
                "full_name": "New User",
            },
            format="json",
        )
        assert response.status_code == 201

    def test_register_duplicate_email(self):
        UserFactory(email="existing@test.com")
        client = APIClient()
        response = client.post(
            "/api/v1/auth/register/",
            {
                "email": "existing@test.com",
                "password": "SecurePass123!",
                "full_name": "New User",
            },
            format="json",
        )
        assert response.status_code == 400


@pytest.mark.django_db
class TestLoginView:
    def test_login_success(self):
        user = UserFactory(email="login@test.com", is_email_verified=True)
        user.set_password("TestPass123!")
        user.save()

        client = APIClient()
        response = client.post(
            "/api/v1/auth/login/",
            {"email": "login@test.com", "password": "TestPass123!"},
            format="json",
        )
        assert response.status_code == 200
        assert "access" in response.json()["data"]

    def test_login_wrong_credentials(self):
        client = APIClient()
        response = client.post(
            "/api/v1/auth/login/",
            {"email": "nobody@test.com", "password": "wrong"},
            format="json",
        )
        assert response.status_code == 400


@pytest.mark.django_db
class TestMeView:
    def test_get_profile_authenticated(self):
        user = UserFactory()
        client = APIClient()
        client.force_authenticate(user=user)

        response = client.get("/api/v1/auth/me/")
        assert response.status_code == 200
        assert response.json()["data"]["email"] == user.email

    def test_get_profile_unauthenticated(self):
        client = APIClient()
        response = client.get("/api/v1/auth/me/")
        assert response.status_code == 401

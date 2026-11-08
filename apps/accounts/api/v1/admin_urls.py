from django.urls import path

from apps.accounts.api.v1 import admin_views

urlpatterns = [
    path("overview/", admin_views.AdminOverviewView.as_view(), name="internal-admin-overview"),
    path("users/", admin_views.AdminUsersView.as_view(), name="internal-admin-users"),
    path("accounts/", admin_views.AdminAccountsView.as_view(), name="internal-admin-accounts"),
]

from django.urls import path

from . import views

urlpatterns = [
    path("", views.WebsiteListCreateView.as_view(), name="website-list"),
    path("<uuid:pk>/", views.WebsiteDetailView.as_view(), name="website-detail"),
    path("<uuid:pk>/pixel/", views.PixelView.as_view(), name="website-pixel"),
    path("<uuid:pk>/pixel/verify/", views.PixelVerifyView.as_view(), name="website-pixel-verify"),
    path("<uuid:pk>/pixel/regenerate/", views.PixelRegenerateView.as_view(), name="website-pixel-regenerate"),
    path("<uuid:pk>/settings/", views.WebsiteSettingsView.as_view(), name="website-settings"),
]

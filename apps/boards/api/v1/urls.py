from django.urls import path

from apps.boards.api.v1.views import BoardLayoutView

urlpatterns = [
    path("websites/<uuid:website_id>/layout/", BoardLayoutView.as_view(), name="boards-layout"),
]

from django.urls import path
from . import views

app_name = "runs"

urlpatterns = [
    path("", views.run_list, name="list"),
    path("new/", views.run_create, name="create"),
    path("<str:run_id>/", views.run_detail, name="detail"),
    path("<str:run_id>/verify/", views.run_verify, name="verify"),
    path("<str:run_id>/reproduce/", views.run_reproduce, name="reproduce"),
    path(
        "<str:run_id>/artifacts/<int:artifact_id>/download/",
        views.artifact_download,
        name="artifact_download",
    ),
    path(
        "<str:run_id>/artifacts/<int:artifact_id>/view/",
        views.artifact_view,
        name="artifact_view",
    ),
]

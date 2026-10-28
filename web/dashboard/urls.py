from django.urls import path
from . import views

app_name = "dashboard"

urlpatterns = [
    path("<str:run_id>/", views.run_dashboard, name="run_dashboard"),
]

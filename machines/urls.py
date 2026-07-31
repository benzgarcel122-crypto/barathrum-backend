from django.urls import path

from . import views

app_name = "machines"

urlpatterns = [
    path("api/box/validate-license/", views.validate_license_view, name="validate_license"),
]

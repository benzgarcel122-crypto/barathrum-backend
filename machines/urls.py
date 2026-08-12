from django.urls import path

from . import views

app_name = "machines"

urlpatterns = [
    path("api/box/validate-license/", views.validate_license_view, name="validate_license"),
    path("api/box/license-points/", views.license_points_view, name="license_points"),
    path("api/box/unbind-license/", views.unbind_license_view, name="unbind_license"),
]

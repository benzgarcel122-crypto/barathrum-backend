from django.urls import path

from . import views

app_name = "dashboard"

urlpatterns = [
    path("", views.home_view, name="home"),
    path("machines/add/", views.add_machine_view, name="add_machine"),
    path("machines/bulk-topup/", views.bulk_topup_view, name="bulk_topup"),
    path("machines/<int:machine_id>/", views.machine_detail_view, name="machine_detail"),
    path("machines/<int:machine_id>/topup/", views.topup_view, name="topup"),
    path("wallet/topup/", views.wallet_topup_view, name="wallet_topup"),
    path("payments/return/", views.payment_return_view, name="payment_return"),
    path("payments/cancel/", views.payment_cancel_view, name="payment_cancel"),
    path("account/", views.account_settings_view, name="account_settings"),
]

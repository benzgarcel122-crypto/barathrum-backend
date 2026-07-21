from django.contrib import admin

from .models import Machine, Payment, Transaction


class TransactionInline(admin.TabularInline):
    model = Transaction
    extra = 0
    readonly_fields = ["created_at"]


@admin.register(Machine)
class MachineAdmin(admin.ModelAdmin):
    list_display = [
        "license_key",
        "nickname",
        "owner",
        "days_remaining",
        "last_topup_bundle_type",
        "created_at",
        "last_checkin_at",
    ]
    list_filter = ["last_topup_bundle_type"]
    search_fields = ["license_key", "nickname", "owner__phone_number"]
    readonly_fields = ["license_key", "created_at"]
    inlines = [TransactionInline]


@admin.register(Transaction)
class TransactionAdmin(admin.ModelAdmin):
    list_display = ["machine", "bundle_type", "days_added", "amount_paid_pesos", "created_at"]
    list_filter = ["bundle_type"]
    search_fields = ["machine__license_key"]
    readonly_fields = ["created_at"]


@admin.register(Payment)
class PaymentAdmin(admin.ModelAdmin):
    list_display = [
        "id", "account", "amount_pesos", "status",
        "paymongo_checkout_session_id", "created_at", "paid_at",
    ]
    list_filter = ["status"]
    search_fields = ["account__phone_number", "paymongo_checkout_session_id"]
    readonly_fields = ["created_at", "paid_at"]

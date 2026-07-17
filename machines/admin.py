from django.contrib import admin

from .models import Machine, Transaction


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

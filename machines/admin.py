from django.contrib import admin

from .models import License, Machine, Payment, Transaction


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


@admin.register(License)
class LicenseAdmin(admin.ModelAdmin):
    list_display = ["license_key", "account", "is_claimed", "created_at"]
    search_fields = ["license_key", "account__phone_number"]
    readonly_fields = ["license_key", "created_at"]

    def get_fields(self, request, obj=None):
        """
        STEP 2.6 (Session 32): a License now starts ownerless -- account is only ever set once a
        Machine actually claims it (see dashboard/views.py::add_machine_view). The raw admin
        "Add" form no longer offers an Account selector at all, so a manually-added License
        starts unclaimed just like every other one. "Change" still shows account (now optional)
        for the rare support case of manually correcting who claimed a key.
        """
        if obj is None:
            return ["license_key", "created_at"]
        return ["license_key", "account", "created_at"]

    @admin.display(boolean=True)
    def is_claimed(self, obj):
        return obj.is_claimed


@admin.register(Payment)
class PaymentAdmin(admin.ModelAdmin):
    list_display = [
        "id", "account", "amount_pesos", "status",
        "paymongo_checkout_session_id", "created_at", "paid_at",
    ]
    list_filter = ["status"]
    search_fields = ["account__phone_number", "paymongo_checkout_session_id"]
    readonly_fields = ["created_at", "paid_at"]

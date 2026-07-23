from django.contrib import admin

from .models import License, Machine, Payment, Transaction


class TransactionInline(admin.TabularInline):
    """
    Same "ledger, not a control surface" rule as TransactionAdmin below applies here too --
    without this, someone could still add/edit a Transaction row through Machine's change page,
    silently bypassing TransactionAdmin's has_add_permission=False.
    """
    model = Transaction
    extra = 0
    fields = ["bundle_type", "days_added", "amount_paid_pesos", "created_at"]
    readonly_fields = ["bundle_type", "days_added", "amount_paid_pesos", "created_at"]

    def has_add_permission(self, request, obj=None):
        return False


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

    # "Add" and "Change" both stay available here -- genuinely needed for support cases (e.g.
    # "my Add Machine keeps rejecting my key, just set it up for me"). But both bypass the app's
    # own claim validation entirely (no license-key-exists check, no already-claimed check), so
    # this fieldset description warns whoever's using the raw admin form directly.
    fieldsets = (
        (
            None,
            {
                "description": (
                    "⚠️ Adding or editing a Machine here bypasses the app's own Add Machine "
                    "flow entirely — no check that the license key exists, no check that it "
                    "isn't already claimed by another machine. Only use this after you've "
                    "manually confirmed it's safe (e.g. you've personally verified the license "
                    "key in Licenses below and confirmed it's genuinely unclaimed). Note: the "
                    "license key field below is read-only and gets auto-generated on save, on "
                    "both Add and Change — there is no way to type in or set a specific "
                    "existing key through this form. If the goal is to attach a specific "
                    "license key someone already has, that's not currently possible through "
                    "admin at all; flag it as a gap rather than working around it here."
                ),
                "fields": (
                    "license_key",
                    "owner",
                    "nickname",
                    "days_remaining",
                    "last_topup_bundle_type",
                    "created_at",
                    "last_checkin_at",
                ),
            },
        ),
    )


@admin.register(Transaction)
class TransactionAdmin(admin.ModelAdmin):
    """
    Ledger/audit record only -- editing here does NOT reverse a top-up's effect on
    Machine.days_remaining (that field lives on Machine itself), so allowing edits here would be
    actively misleading, not just unnecessary. Fully read-only, no manual Add.
    """
    list_display = ["machine", "bundle_type", "days_added", "amount_paid_pesos", "created_at"]
    list_filter = ["bundle_type"]
    search_fields = ["machine__license_key"]
    readonly_fields = ["machine", "bundle_type", "days_added", "amount_paid_pesos", "created_at"]

    def has_add_permission(self, request):
        return False


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
    """
    Trace/debug view only -- "did PayMongo ever start a checkout for them, is it stuck pending"
    for tracing a "my payment didn't go through" complaint. Never a control surface: Payments
    are only ever created by wallet_topup_view; manually adding one, or hand-flipping status to
    "paid," could create a wallet-funding record with no real payment behind it. Fully read-only,
    no manual Add.
    """
    list_display = [
        "id", "account", "amount_pesos", "status",
        "paymongo_checkout_session_id", "created_at", "paid_at",
    ]
    list_filter = ["status"]
    search_fields = ["account__phone_number", "paymongo_checkout_session_id"]
    readonly_fields = [
        "account", "amount_pesos", "paymongo_checkout_session_id", "status", "created_at", "paid_at",
    ]

    def has_add_permission(self, request):
        return False

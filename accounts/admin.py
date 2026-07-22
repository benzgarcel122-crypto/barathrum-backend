from django.contrib import admin
from django.contrib.auth.admin import UserAdmin

from .models import Account, OTPCode


@admin.register(Account)
class AccountAdmin(UserAdmin):
    model = Account
    ordering = ["-created_at"]
    list_display = ["phone_number", "display_name", "balance_points", "is_verified", "is_staff", "created_at"]
    list_filter = ["is_verified", "is_staff", "is_superuser", "is_active"]
    search_fields = ["phone_number", "display_name"]
    readonly_fields = ["created_at"]

    fieldsets = (
        (None, {"fields": ("phone_number", "password")}),
        ("Profile", {"fields": ("display_name", "is_verified", "balance_points")}),
        (
            "Permissions",
            {"fields": ("is_active", "is_staff", "is_superuser", "groups", "user_permissions")},
        ),
        ("Important dates", {"fields": ("last_login", "created_at")}),
    )
    add_fieldsets = (
        (
            None,
            {
                "classes": ("wide",),
                "fields": ("phone_number", "display_name", "password1", "password2"),
            },
        ),
    )


@admin.register(OTPCode)
class OTPCodeAdmin(admin.ModelAdmin):
    list_display = ["phone_number", "code", "used", "created_at", "expires_at"]
    list_filter = ["used"]
    search_fields = ["phone_number"]
    readonly_fields = ["created_at"]

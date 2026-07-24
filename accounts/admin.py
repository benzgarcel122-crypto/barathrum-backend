from django import forms
from django.contrib import admin, messages
from django.contrib.admin.models import ADDITION, LogEntry
from django.contrib.admin.options import get_content_type_for_model
from django.contrib.auth.admin import UserAdmin
from django.contrib.auth.models import Group
from django.db import transaction as db_transaction
from django.shortcuts import redirect, render
from django.urls import path, reverse

from .models import Account, OTPCode

# Groups (Django's built-in auth Group model) is registered automatically by
# django.contrib.auth's own admin.py before this module loads (see INSTALLED_APPS order in
# settings.py). Nothing in this app checks group membership -- exposing Add/Change for it is
# pure confusion with zero function right now, so it's removed from the admin index entirely.
admin.site.unregister(Group)


class GiftPointsForm(forms.Form):
    points = forms.IntegerField(
        min_value=1,
        label="Points to gift",
        help_text="Whole pesos/points, added to each selected account's wallet balance.",
    )
    reason = forms.CharField(
        required=False,
        label="Reason (optional)",
        help_text="Shown in each account's admin history for this change (e.g. 'referral gift').",
        widget=forms.TextInput,
    )


@admin.register(Account)
class AccountAdmin(UserAdmin):
    model = Account
    ordering = ["-created_at"]
    list_display = ["phone_number", "display_name", "balance_points", "is_verified", "is_staff", "created_at"]
    list_filter = ["is_verified", "is_staff", "is_superuser", "is_active"]
    search_fields = ["phone_number", "display_name"]
    readonly_fields = ["created_at"]
    actions = ["gift_points_action"]

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

    def get_urls(self):
        # Extra admin-only URL for the "Gift points" action's intermediate form. Same pattern
        # Django's own built-in actions (e.g. delete_selected) use for a confirm/detail step
        # instead of applying instantly from the changelist action dropdown.
        custom_urls = [
            path(
                "gift-points/",
                self.admin_site.admin_view(self.gift_points_view),
                name="accounts_account_gift_points",
            ),
        ]
        return custom_urls + super().get_urls()

    @admin.action(description="Gift points to selected accounts")
    def gift_points_action(self, request, queryset):
        """
        Entry point from the changelist "Action" dropdown. Doesn't touch any balances itself --
        just hands the selected account IDs off to gift_points_view's form, same two-step pattern
        Django's built-in delete_selected uses (pick rows -> confirm/detail page -> apply).
        """
        selected_ids = queryset.values_list("pk", flat=True)
        id_list = ",".join(str(pk) for pk in selected_ids)
        return redirect(f"{reverse('admin:accounts_account_gift_points')}?ids={id_list}")

    def gift_points_view(self, request):
        """
        GET: show the selected accounts + a form asking how many points to gift and why.
        POST: apply atomically to every selected account's balance_points, one row-lock per
        account (same select_for_update pattern dashboard/views.py uses for wallet spends), and
        write a real Django admin LogEntry per account so it shows up in that account's own
        "History" button and in the site-wide Recent Actions log -- a genuine audit trail, not
        just a raw balance_points edit with no record of who changed it or why.
        """
        if not self.has_change_permission(request):
            messages.error(request, "You don't have permission to do that.")
            return redirect("admin:accounts_account_changelist")

        id_list = request.GET.get("ids", "") or request.POST.get("ids", "")
        account_ids = [int(i) for i in id_list.split(",") if i.strip().isdigit()]
        accounts = list(Account.objects.filter(pk__in=account_ids))

        if not accounts:
            messages.error(request, "No accounts selected. Select one or more accounts first, "
                                     "then choose \"Gift points to selected accounts\".")
            return redirect("admin:accounts_account_changelist")

        if request.method == "POST":
            form = GiftPointsForm(request.POST)
            if form.is_valid():
                points = form.cleaned_data["points"]
                reason = form.cleaned_data["reason"].strip()
                log_message = f"Gifted {points} points" + (f" ({reason})" if reason else "")

                with db_transaction.atomic():
                    for account in accounts:
                        # Re-fetch and lock so a concurrent balance change (e.g. the operator
                        # spending from their wallet at the same moment) can't be silently
                        # overwritten by a stale read here.
                        locked = Account.objects.select_for_update().get(pk=account.pk)
                        locked.balance_points += points
                        locked.save(update_fields=["balance_points"])

                        LogEntry.objects.log_action(
                            user_id=request.user.pk,
                            content_type_id=get_content_type_for_model(Account).pk,
                            object_id=locked.pk,
                            object_repr=str(locked),
                            action_flag=ADDITION,
                            change_message=log_message,
                        )

                messages.success(
                    request,
                    f"Gifted {points} points to {len(accounts)} account(s)."
                    + (f" Reason: {reason}" if reason else ""),
                )
                return redirect("admin:accounts_account_changelist")
        else:
            form = GiftPointsForm()

        context = {
            **self.admin_site.each_context(request),
            "title": "Gift points",
            "form": form,
            "accounts": accounts,
            "id_list": id_list,
            "opts": self.model._meta,
        }
        return render(request, "admin/gift_points.html", context)


@admin.register(OTPCode)
class OTPCodeAdmin(admin.ModelAdmin):
    """
    Trace/debug view only -- "was a code actually issued, is it expired, how many failed
    attempts" for tracing a "my code isn't working" complaint. Never a control surface: codes
    are only ever created by OTPCode.issue() from the real signup/login flow, so manually adding
    or hand-editing one here (e.g. flipping `used`) could mask a real bug instead of surfacing
    it. List/detail view stays available for debugging; nothing on it is editable.
    """
    list_display = ["phone_number", "code", "used", "failed_attempts", "created_at", "expires_at"]
    list_filter = ["used"]
    search_fields = ["phone_number"]
    readonly_fields = ["phone_number", "code", "used", "failed_attempts", "created_at", "expires_at"]

    def has_add_permission(self, request):
        return False

from django import forms
from django.contrib import admin, messages
from django.contrib.admin.widgets import AutocompleteSelect
from django.contrib.auth import get_user_model
from django.db import IntegrityError
from django.db import transaction as db_transaction
from django.shortcuts import redirect, render
from django.urls import path, reverse

from .models import License, Machine, Payment, Transaction

Account = get_user_model()


class AttachToMachineForm(forms.Form):
    """
    Intermediate form for LicenseAdmin's "Attach to a new Machine" action (see
    LicenseAdmin.attach_to_new_machine / attach_to_machine_view below). Deliberately requires an
    explicit Account choice -- this is admin-side, not self-service, so there is no logged-in
    "claiming account" to default to the way dashboard/views.py::add_machine_view has; guessing
    (e.g. defaulting to the staff member's own account) would misattribute the machine.
    """
    account = forms.ModelChoiceField(
        queryset=Account.objects.all(),
        # License already has a real `account` ForeignKey to Account (see models.py) -- reusing
        # that field here (rather than defining a new one) is what lets this widget hit Django
        # admin's existing /admin/autocomplete/ endpoint and search by phone number/display name
        # via AccountAdmin.search_fields, instead of rendering every Account as one long <select>
        # dropdown. Requires LicenseAdmin.autocomplete_fields = ["account"] below.
        widget=AutocompleteSelect(License._meta.get_field("account"), admin.site),
        label="Attach to Account",
        help_text="Search by phone number or name. Not guessed or defaulted from the logged-in "
                  "staff user -- pick explicitly.",
    )
    nickname = forms.CharField(
        required=False,
        label="Nickname (optional)",
        help_text="Same as the nickname field on the self-service Add Machine form.",
    )
 
 
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
    list_display = ["license_key", "generated_by", "account", "is_claimed", "is_activated", "created_at"]
    search_fields = ["license_key", "account__phone_number", "generated_by__phone_number"]
    readonly_fields = ["license_key", "generated_by", "created_at", "activated_at"]
    actions = ["attach_to_new_machine"]
    # Enables Django admin's built-in searchable widget (search by phone number or display
    # name, via AccountAdmin.search_fields) for the `account` field -- both here on License's
    # own Change form, and reused by AttachToMachineForm above via the same underlying
    # /admin/autocomplete/ endpoint.
    autocomplete_fields = ["account"]

    def get_fields(self, request, obj=None):
        """
        STEP 2.6 (Session 32): a License now starts ownerless -- account is only ever set once a
        Machine actually claims it (see dashboard/views.py::add_machine_view). The raw admin
        "Add" form no longer offers an Account selector at all, so a manually-added License
        starts unclaimed just like every other one. "Change" still shows account (now optional)
        for the rare support case of manually correcting who claimed a key.
 
        Session 36: generated_by only appears on the Change page, not Add -- a License created
        directly through admin has no real "generator" in the app-usage sense this field tracks,
        so there's nothing meaningful to show for it on the Add form.

        Session 86: activated_at only appears on the Change page too, same reasoning as
        generated_by -- a License created directly through admin has never actually been
        activated by a real box, so there's nothing meaningful to show on the Add form either.
        """
        if obj is None:
            return ["license_key", "created_at"]
        return ["license_key", "generated_by", "account", "created_at", "activated_at"]
 
    @admin.display(boolean=True)
    def is_claimed(self, obj):
        return obj.is_claimed

    @admin.display(boolean=True, description="Activated")
    def is_activated(self, obj):
        return obj.is_activated

    def get_urls(self):
        # Extra admin-only URL for "Attach to a new Machine"'s intermediate form -- same
        # confirm/detail-page pattern AccountAdmin's gift_points action uses (and the same
        # pattern Django's own built-in delete_selected uses), rather than applying instantly
        # from the changelist action dropdown.
        custom_urls = [
            path(
                "attach-to-machine/",
                self.admin_site.admin_view(self.attach_to_machine_view),
                name="machines_license_attach_to_machine",
            ),
        ]
        return custom_urls + super().get_urls()

    @admin.action(description="Attach to Dashboard")
    def attach_to_new_machine(self, request, queryset):
        """
        Entry point from the changelist "Action" dropdown. Internal name/URL kept as
        "attach_to_new_machine"/"attach-to-machine" for continuity with existing code and tests
        -- only the user-facing dropdown label changed (PM feedback: "Attach to a new Machine"
        read as confusingly close to "Delete selected license" in the dropdown). This is a
        REGISTRATION/ATTACHMENT mechanism, not license activation -- it sets License.is_claimed the same way clicking
        Add Machine on the dashboard already does (an active Machine row exists for this key).
        It does not prove an operator physically owns a box; that distinction (box-side
        activation vs. dashboard registration) doesn't exist in this codebase yet (see MPD
        Session 64/65/66 notes under STEP 2.6). Doesn't touch anything itself -- just hands the
        selected License IDs off to attach_to_machine_view's form, same two-step pattern
        AccountAdmin.gift_points_action uses.
        """
        selected_ids = queryset.values_list("pk", flat=True)
        id_list = ",".join(str(pk) for pk in selected_ids)
        return redirect(f"{reverse('admin:machines_license_attach_to_machine')}?ids={id_list}")

    def attach_to_machine_view(self, request):
        """
        GET: show the selected licenses + a form asking which Account to attach them to (and an
        optional nickname). POST: apply the same three-state claim logic
        dashboard/views.py::add_machine_view uses (fresh create / reactivate a released Machine /
        reject an already-attached one), per selected License, each in its own atomic block so
        one License failing (already attached, race condition) does not affect the others.

        Mirrors add_machine_view's logic in a new location rather than modifying or calling into
        the original view directly -- see this task's own non-goals.
        """
        if not self.has_change_permission(request):
            messages.error(request, "You don't have permission to do that.")
            return redirect("admin:machines_license_changelist")

        id_list = request.GET.get("ids", "") or request.POST.get("ids", "")
        license_ids = [int(i) for i in id_list.split(",") if i.strip().isdigit()]
        licenses = list(License.objects.filter(pk__in=license_ids))

        if not licenses:
            messages.error(
                request,
                "No licenses selected. Select one or more licenses first, then choose "
                "\"Attach to a new Machine\".",
            )
            return redirect("admin:machines_license_changelist")

        if request.method == "POST":
            form = AttachToMachineForm(request.POST)
            if form.is_valid():
                account = form.cleaned_data["account"]
                nickname = form.cleaned_data["nickname"].strip()

                for license_obj in licenses:
                    # Re-fetch each license individually rather than trusting the `licenses`
                    # list captured at GET time -- guards against a real gap between when the
                    # form was rendered and when it was submitted (another staff member, or a
                    # customer via the real self-service Add Machine flow, may have claimed one
                    # of these keys in the meantime).
                    try:
                        license_obj = License.objects.get(pk=license_obj.pk)
                    except License.DoesNotExist:
                        self.message_user(
                            request,
                            f"{license_obj.license_key}: license no longer exists.",
                            level=messages.ERROR,
                        )
                        continue

                    existing_machine = Machine.objects.filter(
                        license_key=license_obj.license_key
                    ).first()

                    if existing_machine is not None and existing_machine.removed_at is None:
                        self.message_user(
                            request,
                            f"{license_obj.license_key}: already attached to a machine.",
                            level=messages.ERROR,
                        )
                        continue

                    try:
                        with db_transaction.atomic():
                            if existing_machine is not None:
                                # Released machine being reclaimed: reactivate the SAME row so
                                # days_remaining and every existing Transaction row (which FKs to
                                # this Machine's pk) survive completely untouched -- do not
                                # create a second Machine row for this key. Same pattern
                                # add_machine_view uses for this exact case.
                                machine = existing_machine
                                machine.removed_at = None
                                machine.owner = account
                                machine.nickname = nickname
                                machine.save(update_fields=["removed_at", "owner", "nickname"])
                            else:
                                machine = Machine.objects.create(
                                    owner=account,
                                    nickname=nickname,
                                    license_key=license_obj.license_key,
                                )
                            # .update() (not .save()) so this stays inside the same atomic
                            # block as the Machine insert/reactivation without re-running
                            # License.save()'s key-generation logic -- same reasoning as
                            # add_machine_view's own claim step.
                            License.objects.filter(pk=license_obj.pk).update(account=account)
                    except IntegrityError:
                        # Race: another request claimed this exact key between the check above
                        # and this insert. Machine.license_key's DB-level unique constraint is
                        # the real guarantee here, same pattern as add_machine_view's own comment.
                        self.message_user(
                            request,
                            f"{license_obj.license_key}: just claimed by another machine.",
                            level=messages.ERROR,
                        )
                        continue

                    self.message_user(
                        request,
                        f"Attached {license_obj.license_key} to a new Machine for {account}.",
                    )

                return redirect("admin:machines_license_changelist")
        else:
            form = AttachToMachineForm()

        context = {
            **self.admin_site.each_context(request),
            "title": "Attach to Dashboard",
            "form": form,
            "licenses": licenses,
            "id_list": id_list,
            "opts": self.model._meta,
        }
        return render(request, "admin/attach_to_machine.html", context)
 
 
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

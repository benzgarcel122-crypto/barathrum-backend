import logging
from datetime import timedelta
from decimal import Decimal
 
from django.contrib import messages
from django.contrib.auth import get_user_model
from django.contrib.auth.decorators import login_required
from django.contrib.auth.hashers import check_password, make_password
from django.db import IntegrityError
from django.db import models
from django.db import transaction as db_transaction
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.http import require_http_methods
 
from accounts.models import PointTransfer, normalize_phone_number
from machines import paymongo_client
from machines.models import (
    BUNDLE_TYPE_CHOICES,
    MACHINE_ZERO_BALANCE_CLEANUP_DAYS,
    MINIMUM_TOPUP_POINTS,
    UNCLAIMED_LICENSE_LIFETIME_DAYS,
    License,
    Machine,
    Payment,
    Transaction,
    calendar_days_since,
)
from machines.paymongo_client import PayMongoAPIError
 
Account = get_user_model()
logger = logging.getLogger(__name__)
 
# STEP: license generation fee (anti-abuse) -- deducted from the generating operator's own wallet
# balance in generate_license_view below. Named constant, used everywhere the fee amount matters
# so it's never hardcoded a second time.
LICENSE_GENERATION_FEE = 20  # points deducted from the generating operator's wallet balance
 
# Bundle pricing, per the locked design in the STEP 2.2 task -- UNCHANGED by STEP 2.4:
#   bundle_type -> (days, price_pesos)
# Custom top-ups are priced separately at CUSTOM_PRICE_PER_DAY and aren't in this table.
BUNDLE_PRICING = {
    "30day": {"days": 30, "price": Decimal("27")},
    "60day": {"days": 60, "price": Decimal("52")},
    "100day": {"days": 100, "price": Decimal("84")},
    "300day": {"days": 300, "price": Decimal("250")},
    "1000day": {"days": 1000, "price": Decimal("750")},
}
CUSTOM_PRICE_PER_DAY = Decimal("1")
# Bundles that unlock monitoring per Machine.is_monitoring_unlocked.
MONITORING_UNLOCK_BUNDLES = ("300day", "1000day")
 
# A machine "needs a top-up" for Select-All / batch-bar purposes at the same threshold as the
# yellow/red color coding: 7 days or fewer left (including 0/expired). This wasn't spelled out
# explicitly in the task, so flagging the assumption here for the PM to confirm.
NEEDS_TOPUP_THRESHOLD_DAYS = 7
 
# STEP 2.4: quick-tap preset amounts for wallet funding, in whole pesos (== points, flat 1:1).
WALLET_TOPUP_PRESETS = [100, 500, 1000]
 
 
def _bundle_pricing_with_discount():
    """Bundle pricing enriched with % off vs. the custom per-day rate, for template display."""
    enriched = []
    for bundle_type, label in BUNDLE_TYPE_CHOICES:
        if bundle_type not in BUNDLE_PRICING:
            continue
        info = BUNDLE_PRICING[bundle_type]
        full_price = info["days"] * CUSTOM_PRICE_PER_DAY
        pct_off = round((1 - (info["price"] / full_price)) * 100)
        enriched.append(
            {
                "bundle_type": bundle_type,
                "label": label,
                "days": info["days"],
                "price": info["price"],
                "pct_off": pct_off,
                "unlocks_monitoring": bundle_type in MONITORING_UNLOCK_BUNDLES,
            }
        )
    return enriched
 
 
def _status_color(days_remaining):
    if days_remaining <= 0:
        return "red"
    if days_remaining <= NEEDS_TOPUP_THRESHOLD_DAYS:
        return "yellow"
    return "green"
 
 
def _machine_card_context(machine):
    return {
        "machine": machine,
        "color": _status_color(machine.days_remaining),
        "needs_topup": machine.days_remaining <= NEEDS_TOPUP_THRESHOLD_DAYS,
    }
 
 
def _initiate_paymongo_checkout(payment, request):
    """
    Create a PayMongo Checkout Session for a single wallet-funding Payment. Stamps the resulting
    session id onto the Payment row so the webhook can find it later, and returns the
    checkout_url to redirect the operator to.
 
    STEP 2.4: unlike STEP 2.3 (which could bundle several Payments -- one per machine -- into
    one checkout), wallet funding is always exactly one Payment per checkout: the operator is
    topping up their own single wallet, there's no "batch" concept here anymore.
 
    Raises PayMongoAPIError on any failure -- callers are responsible for marking the Payment as
    "failed" and showing the operator an error; this function does not touch Payment.status.
    """
    line_items = [{
        "currency": "PHP",
        "amount": int(payment.amount_pesos * 100),  # PayMongo amounts are centavos, not pesos
        "name": f"Barathrum wallet top-up (₱{payment.amount_pesos})",
        "quantity": 1,
    }]
 
    session_id, checkout_url = paymongo_client.create_checkout_session(
        line_items=line_items,
        payment_method_types=["gcash", "paymaya"],
        success_url=request.build_absolute_uri(
            reverse("dashboard:payment_return") + f"?payment_ids={payment.id}"
        ),
        cancel_url=request.build_absolute_uri(
            reverse("dashboard:payment_cancel") + f"?payment_ids={payment.id}"
        ),
        reference_number=str(payment.id),
        description="Barathrum wallet top-up",
    )
 
    payment.paymongo_checkout_session_id = session_id
    payment.save(update_fields=["paymongo_checkout_session_id"])
 
    return checkout_url
 
 
def home_view(request):
    """
    STEP 2.5 (Session 31): "/" now serves two audiences. Logged-out visitors get the public
    Landing Page (kept as one view + one URL name, "dashboard:home", so every existing
    `redirect("dashboard:home")` call elsewhere in this file keeps working unchanged for
    authenticated users -- no new URL name needed). Logged-in visitors still see the exact same
    Dashboard Home as before -- no @login_required decorator anymore since this view now
    explicitly handles the logged-out case itself instead of redirecting to /login/.
    """
    if not request.user.is_authenticated:
        return render(request, "dashboard/landing.html", {})
 
    machines = Machine.objects.filter(owner=request.user, removed_at__isnull=True).order_by("-created_at")
    cards = [_machine_card_context(m) for m in machines]
    any_needs_topup = any(c["needs_topup"] for c in cards)
 
    return render(
        request,
        "dashboard/home.html",
        {
            "active_nav": "dashboard",
            "cards": cards,
            "any_needs_topup": any_needs_topup,
            "balance_points": request.user.balance_points,
        },
    )
 
 
@login_required
@require_http_methods(["GET", "POST"])
def generate_license_view(request):
    """
    STEP 2.5 (Session 31): standalone license key generation, decoupled from Add Machine.
 
    STEP 2.6 (Session 32) REVERSAL, per explicit Investigator directive: the resulting License
    is created with account=None -- being logged in is still required to reach this view at all
    (only a real Account can click the button), but the License itself has no owner yet. It
    stays ownerless until some Machine successfully claims it (see add_machine_view below).
 
    Session 36: this is now the sidebar's primary nav slot (repurposed from "Add Machine", which
    was redundant with Dashboard Home's own "+ Add Machine" button). active_nav is "generate_license"
    to match, not "add_machine" -- Add Machine itself is no longer in the sidebar at all.

    Anti-abuse fee (this task): generating a license now costs LICENSE_GENERATION_FEE points,
    deducted from the generating operator's own wallet balance -- previously this was free and
    spammable (unlimited ownerless License rows with no balance check). Mirrors the exact
    pre-check + locked re-check discipline topup_view and send_points_view already use.
    """
    if request.method == "GET":
        history = []
        for lic in request.user.generated_licenses.order_by("-created_at"):
            if lic.is_claimed:
                machine = Machine.objects.filter(license_key=lic.license_key).first()
                # STEP 2.7 item 5 follow-up (this task): surface the zero-balance cleanup
                # countdown here too, not just in the DB. None means either the machine still
                # has a positive balance, or it was topped up and the countdown was already
                # cancelled -- the template uses None to decide whether to show anything extra
                # at all, same convention as days_until_expiry below for unclaimed licenses.
                days_until_deletion = None
                if machine is not None and machine.zero_balance_since is not None:
                    days_until_deletion = MACHINE_ZERO_BALANCE_CLEANUP_DAYS - calendar_days_since(
                        machine.zero_balance_since
                    )
                history.append({
                    "license": lic,
                    "status": "claimed",
                    "machine": machine,
                    "days_until_deletion": days_until_deletion,
                })
            else:
                age_days = calendar_days_since(lic.created_at)
                days_until_expiry = UNCLAIMED_LICENSE_LIFETIME_DAYS - age_days
                history.append({
                    "license": lic,
                    "status": "unclaimed",
                    "days_until_expiry": days_until_expiry,
                })
        return render(
            request,
            "dashboard/generate_license.html",
            {
                "active_nav": "generate_license",
                "fee": LICENSE_GENERATION_FEE,
                "balance_points": request.user.balance_points,
                "history": history,
            },
        )

    # STEP 2.7 items 2-4 (Session 48 design): a recovery password is now mandatory before a
    # License can be generated at all -- it gates the new Release License action later. Checked
    # BEFORE any fee deduction or License creation, so a failed password check never costs the
    # operator points or leaves a passwordless License row behind.
    recovery_password = request.POST.get("recovery_password", "")
    recovery_password_confirm = request.POST.get("recovery_password_confirm", "")

    if not recovery_password or not recovery_password_confirm:
        messages.error(request, "Enter and confirm a recovery password.")
        return redirect("dashboard:generate_license")

    if recovery_password != recovery_password_confirm:
        messages.error(request, "Recovery passwords don't match.")
        return redirect("dashboard:generate_license")

    if len(recovery_password) < 6:
        messages.error(request, "Recovery password must be at least 6 characters.")
        return redirect("dashboard:generate_license")

    if request.user.balance_points < LICENSE_GENERATION_FEE:
        messages.error(
            request,
            f"You need at least {LICENSE_GENERATION_FEE} points to generate a license key. "
            f"Your current balance is {request.user.balance_points}.",
        )
        return redirect("dashboard:generate_license")

    with db_transaction.atomic():
        locked_account = Account.objects.select_for_update().get(pk=request.user.pk)
        if locked_account.balance_points < LICENSE_GENERATION_FEE:
            messages.error(
                request,
                f"You need at least {LICENSE_GENERATION_FEE} points to generate a license key. "
                f"Your current balance is {locked_account.balance_points}.",
            )
            return redirect("dashboard:generate_license")

        locked_account.balance_points -= LICENSE_GENERATION_FEE
        locked_account.save(update_fields=["balance_points"])

        license_obj = License.objects.create(
            account=None,
            generated_by=request.user,
            recovery_password_hash=make_password(recovery_password),
        )

    return render(
        request,
        "dashboard/license_generated.html",
        {"active_nav": "generate_license", "license": license_obj},
    )
 
 
@login_required
@require_http_methods(["GET", "POST"])
def add_machine_view(request):
    """
    STEP 2.5 (Session 31): Add Machine no longer generates a license key inline. The operator
    must paste a key that was already generated via generate_license_view.
 
    STEP 2.6 (Session 32) REVERSAL, per explicit Investigator directive: the Session 31 rule
    requiring the pasted key to belong to the claiming account is GONE. A License now has no
    owner until it's claimed, so the only checks are: does this key exist, and is it not already
    attached to a Machine. Any logged-in account can claim any unclaimed license, regardless of
    who (or which account) originally generated it. On a successful claim, License.account is
    set to the claiming account -- repurposing the field from "who generated this" to "who
    claimed this," for audit/record purposes only; it is no longer used as an access check.
    """
    if request.method == "GET":
        return render(request, "dashboard/add_machine.html", {"active_nav": "add_machine"})
 
    license_key_input = request.POST.get("license_key", "").strip().upper()
    nickname = request.POST.get("nickname", "").strip()
    context = {
        "active_nav": "add_machine",
        "license_key_input": license_key_input,
        "nickname": nickname,
    }
 
    if not license_key_input:
        messages.error(request, "Enter a license key.")
        return render(request, "dashboard/add_machine.html", context)
 
    try:
        license_obj = License.objects.get(license_key=license_key_input)
    except License.DoesNotExist:
        messages.error(
            request,
            "That license key wasn't found. Double check it's typed correctly.",
        )
        return render(request, "dashboard/add_machine.html", context)
 
    # STEP 2.7 items 2-4 (Session 48 design): three states instead of two. A Machine row can now
    # exist for this license_key in an ACTIVE state (still rejected outright, same as before), a
    # RELEASED state (this is the new case -- reactivate that exact row instead of rejecting or
    # creating a second row for the same key), or not exist at all (unchanged fresh-claim path).
    existing_machine = Machine.objects.filter(license_key=license_obj.license_key).first()

    if existing_machine is not None and existing_machine.removed_at is None:
        messages.error(request, "This license key is already attached to a machine.")
        return render(request, "dashboard/add_machine.html", context)

    try:
        with db_transaction.atomic():
            if existing_machine is not None:
                # Released machine being reclaimed: reactivate the SAME row so days_remaining
                # and every existing Transaction row (which FKs to this Machine's pk) survive
                # completely untouched -- do not create a second Machine row for this key.
                machine = existing_machine
                machine.removed_at = None
                machine.owner = request.user
                machine.nickname = nickname
                machine.save(update_fields=["removed_at", "owner", "nickname"])
            else:
                machine = Machine.objects.create(
                    owner=request.user, nickname=nickname, license_key=license_obj.license_key
                )
            # Repurpose License.account from "generator" to "claimant" now that this key is
            # spoken for. .update() (not .save()) so this stays inside the same atomic block as
            # the Machine insert/reactivation without re-running License.save()'s key-generation
            # logic. Applies identically to a fresh claim and a reclaim-after-release.
            License.objects.filter(pk=license_obj.pk).update(account=request.user)
    except IntegrityError:
        # Race: another request claimed this exact key between the check above and this insert.
        # Machine.license_key's DB-level unique constraint is the real guarantee here, same
        # pattern as generate_unique_license_key's own comment in machines/models.py.
        messages.error(
            request, "This license key was just claimed by another machine. Try a different key."
        )
        return render(request, "dashboard/add_machine.html", context)
 
    return render(
        request,
        "dashboard/machine_created.html",
        {"active_nav": "add_machine", "machine": machine},
    )


@login_required
@require_http_methods(["GET", "POST"])
def remove_license_view(request, machine_id):
    """
    Dashboard-side unclaim (End Goals closing note's "claimed" axis) -- replaces the old
    password-gated release_license_view. The password-gated box-side deactivation this used to
    also require now lives entirely on the box's own admin panel (End Goal #20,
    machines/views.py::unbind_license_view) -- these are two independent actions, either order,
    per this session's PM decision. No password here: this view is already scoped to
    owner=request.user, so there's no "stranger" risk the old password gate was guarding against
    at the dashboard layer.

    On success: Machine.removed_at is set (row kept, not deleted), so days_remaining and every
    Transaction row survive untouched until either a re-claim via add_machine_view (which
    reactivates this exact row) or the 20-day zero-balance cleanup job eventually sweeps it up.
    Does NOT touch License at all -- no lookup, no field changes -- unlike the old view.
    """
    machine = get_object_or_404(
        Machine, id=machine_id, owner=request.user, removed_at__isnull=True
    )

    if request.method == "GET":
        return render(request, "dashboard/remove_license.html", {"active_nav": "dashboard", "machine": machine})

    machine.removed_at = timezone.now()
    machine.save(update_fields=["removed_at"])
    messages.success(
        request,
        f"{machine.nickname or machine.license_key} removed from your dashboard. Its balance and "
        f"history are kept -- add it back later with the same license key.",
    )
    return redirect("dashboard:home")


def download_placeholder_view(request):
    """
    STEP 2.5 (Session 31): STEP 1's box agent doesn't exist yet, so "Download Box Software" on
    the Landing Page points here instead of a broken/fabricated link. Public (no login required)
    since a logged-out visitor is the primary audience for this CTA.
    """
    return render(request, "dashboard/download_placeholder.html", {})
 
 
@login_required
def machine_detail_view(request, machine_id):
    machine = get_object_or_404(Machine, id=machine_id, owner=request.user, removed_at__isnull=True)
    transactions = machine.transactions.order_by("-created_at")[:20]
    license_obj = License.objects.filter(license_key=machine.license_key).first()
    is_activated = license_obj.is_activated if license_obj else False
    return render(
        request,
        "dashboard/machine_detail.html",
        {
            "active_nav": "dashboard",
            "machine": machine,
            "color": _status_color(machine.days_remaining),
            "transactions": transactions,
            "is_activated": is_activated,
        },
    )
 
 
@login_required
@require_http_methods(["GET", "POST"])
def topup_view(request, machine_id):
    """
    STEP 2.4: per-machine top-up now spends from the operator's own wallet (Account.balance_points)
    instead of creating a Payment/redirecting to PayMongo. No external call, no redirect away from
    the site at all -- this is now a single atomic DB transaction, same as STEP 2.2's original stub,
    just gated on a real balance check instead of being unconditionally free.
    """
    machine = get_object_or_404(Machine, id=machine_id, owner=request.user, removed_at__isnull=True)
    license_obj = License.objects.filter(license_key=machine.license_key).first()
    # No is_activated guard here, deliberately -- Top Up has never required activation for
    # days_remaining (fully box-independent), and license_points funded via Top Up should behave
    # the same way: a license can validly receive points before/without any box ever binding
    # (End Goals closing note explicitly supports "unclaimed but activated" as a real state). A
    # missing License row (should not happen in practice, same posture as every other lookup
    # like this in this codebase) never blocks the days top-up -- it only means the
    # license_points half is silently skipped below, with a logged warning.
 
    if request.method == "GET":
        tab = request.GET.get("tab", "bundles")
        return render(
            request,
            "dashboard/topup.html",
            {
                "active_nav": "dashboard",
                "machine": machine,
                "tab": tab,
                "bundles": _bundle_pricing_with_discount(),
                "custom_price_per_day": CUSTOM_PRICE_PER_DAY,
                "balance_points": request.user.balance_points,
            },
        )
 
    mode = request.POST.get("mode")  # "bundle" or "custom"
 
    if mode == "bundle":
        bundle_type = request.POST.get("bundle_type")
        info = BUNDLE_PRICING.get(bundle_type)
        if info is None:
            messages.error(request, "Pick a valid bundle.")
            return redirect("dashboard:topup", machine_id=machine.id)
        days_added = info["days"]
        price = info["price"]
    elif mode == "custom":
        try:
            days_added = int(request.POST.get("custom_days", "0"))
        except ValueError:
            days_added = 0
        if days_added < MINIMUM_TOPUP_POINTS:
            messages.error(request, f"Minimum custom top-up is {MINIMUM_TOPUP_POINTS} days.")
            return redirect(f"{request.path}?tab=custom")
        bundle_type = "custom"
        price = CUSTOM_PRICE_PER_DAY * days_added
    else:
        messages.error(request, "Choose a bundle or a custom number of days.")
        return redirect("dashboard:topup", machine_id=machine.id)
 
    price_points = int(price)  # wallet balance is in whole points, 1:1 with pesos
 
    if request.user.balance_points < price_points:
        messages.error(
            request,
            f"Not enough wallet balance for this top-up (need ₱{price_points}, you have "
            f"₱{request.user.balance_points}). Top up your wallet first.",
        )
        return redirect("dashboard:topup", machine_id=machine.id)
 
    with db_transaction.atomic():
        # Re-fetch and lock the Account row so two near-simultaneous top-ups from the same
        # operator can't both pass the balance check above against a stale balance.
        account = Account.objects.select_for_update().get(pk=request.user.pk)
        if account.balance_points < price_points:
            messages.error(request, "Not enough wallet balance for this top-up.")
            return redirect("dashboard:topup", machine_id=machine.id)
 
        account.balance_points -= price_points
        account.save(update_fields=["balance_points"])
 
        machine.days_remaining += days_added
        machine.last_topup_bundle_type = bundle_type
        # STEP 2.7 item 5 (Session 48 corrected design): ANY top-up, no minimum threshold,
        # cancels the zero-balance cleanup countdown immediately -- cleared here rather than
        # waiting for the next daily cron pass, so the countdown reset happens the instant the
        # top-up is applied, not up to a day later.
        machine.zero_balance_since = None
        machine.save(update_fields=["days_remaining", "last_topup_bundle_type", "zero_balance_since"])

        # NEW: mirror the exact same days_added amount into License.license_points, same
        # transaction, same payment, no second charge. F()-based increment (not a
        # read-modify-write) for the same lost-update-avoidance reason send_license_points_view
        # (now deleted) used it -- consistent with every other points-mutation in this codebase.
        if license_obj is not None:
            License.objects.filter(pk=license_obj.pk).update(
                license_points=models.F("license_points") + days_added
            )
        else:
            logger.warning(
                "topup_view: no License row found for license_key=%s -- days credited, "
                "license_points NOT credited.", machine.license_key,
            )
 
        Transaction.objects.create(
            machine=machine,
            bundle_type=bundle_type,
            days_added=days_added,
            amount_paid_pesos=price,
        )
 
    messages.success(
        request,
        f"Topped up {machine.nickname or machine.license_key} with {days_added} days "
        f"(₱{price_points} from your wallet).",
    )
    return redirect("dashboard:home")
 
 
@login_required
@require_http_methods(["GET", "POST"])
def bulk_topup_view(request):
    """STEP 2.4: same wallet-balance model as topup_view, applied across every selected machine
    together -- one combined balance check, one atomic deduction, no external payment gateway
    step to coordinate around anymore."""
    query = request.GET if request.method == "GET" else request.POST
 
    machine_ids = [int(v) for v in query.getlist("machine_id") if v.isdigit()]
    if not machine_ids:
        # Fallback: also accept a comma-joined "ids" param for direct/programmatic links.
        ids_param = query.get("ids", "")
        machine_ids = [int(i) for i in ids_param.split(",") if i.strip().isdigit()]
    ids_param = ",".join(str(i) for i in machine_ids)
 
    machines = list(Machine.objects.filter(id__in=machine_ids, owner=request.user, removed_at__isnull=True).order_by("-created_at"))
 
    if not machines:
        messages.error(request, "No machines selected for bulk top-up.")
        return redirect("dashboard:home")
 
    if request.method == "GET":
        return render(
            request,
            "dashboard/bulk_topup.html",
            {
                "active_nav": "dashboard",
                "machines": machines,
                "bundles": _bundle_pricing_with_discount(),
                "ids_param": ids_param,
                "balance_points": request.user.balance_points,
            },
        )
 
    # POST: validate every machine has a bundle chosen, and sum the total cost first.
    updates = []
    for machine in machines:
        bundle_type = request.POST.get(f"bundle_{machine.id}")
        info = BUNDLE_PRICING.get(bundle_type)
        if info is None:
            messages.error(request, f"Pick a bundle for every selected machine ({machine.nickname or machine.license_key} is missing one).")
            return redirect(f"/machines/bulk-topup/?ids={ids_param}")
        updates.append((machine, bundle_type, info["days"], info["price"]))
 
    total_points = int(sum(u[3] for u in updates))
 
    if request.user.balance_points < total_points:
        messages.error(
            request,
            f"Not enough wallet balance for this batch (need ₱{total_points}, you have "
            f"₱{request.user.balance_points}). Top up your wallet first, or select fewer machines.",
        )
        return redirect(f"/machines/bulk-topup/?ids={ids_param}")
 
    with db_transaction.atomic():
        account = Account.objects.select_for_update().get(pk=request.user.pk)
        if account.balance_points < total_points:
            # Same-shape re-check as topup_view -- see that view's comment for why.
            messages.error(request, "Not enough wallet balance for this batch.")
            return redirect(f"/machines/bulk-topup/?ids={ids_param}")
 
        account.balance_points -= total_points
        account.save(update_fields=["balance_points"])
 
        for machine, bundle_type, days_added, price in updates:
            machine.days_remaining += days_added
            machine.last_topup_bundle_type = bundle_type
            # STEP 2.7 item 5 (Session 48 corrected design): same immediate-cancel rule as
            # topup_view above -- applies per-machine within this batch.
            machine.zero_balance_since = None
            machine.save(update_fields=["days_remaining", "last_topup_bundle_type", "zero_balance_since"])

            # NEW: same per-machine mirror as topup_view, same reasoning -- no is_activated
            # guard, missing License row never blocks the days credit above.
            license_obj = License.objects.filter(license_key=machine.license_key).first()
            if license_obj is not None:
                License.objects.filter(pk=license_obj.pk).update(
                    license_points=models.F("license_points") + days_added
                )
            else:
                logger.warning(
                    "bulk_topup_view: no License row found for license_key=%s -- days credited, "
                    "license_points NOT credited.", machine.license_key,
                )

            Transaction.objects.create(
                machine=machine,
                bundle_type=bundle_type,
                days_added=days_added,
                amount_paid_pesos=price,
            )
 
    messages.success(
        request,
        f"Topped up {len(updates)} machines — ₱{total_points} deducted from your wallet.",
    )
    return redirect("dashboard:home")
 
 
@login_required
@require_http_methods(["GET", "POST"])
def wallet_topup_view(request):
    """
    STEP 2.4: fund the wallet itself -- flat 1:1 peso-to-point, no bundle tiers. Reuses the exact
    same PayMongo Checkout Session integration STEP 2.3 built; only what the Payment represents
    has changed (account-level funding, not a specific machine/bundle purchase).
    """
    if request.method == "GET":
        return render(
            request,
            "dashboard/wallet_topup.html",
            {
                "active_nav": "dashboard",
                "balance_points": request.user.balance_points,
                "presets": WALLET_TOPUP_PRESETS,
            },
        )
 
    try:
        amount = int(request.POST.get("amount", "0"))
    except ValueError:
        amount = 0
 
    if amount < MINIMUM_TOPUP_POINTS:
        messages.error(request, f"Minimum top-up is {MINIMUM_TOPUP_POINTS} points.")
        return redirect("dashboard:wallet_topup")
 
    payment = Payment.objects.create(account=request.user, amount_pesos=amount, status="pending")
 
    try:
        checkout_url = _initiate_paymongo_checkout(payment, request)
    except PayMongoAPIError as exc:
        payment.status = "failed"
        payment.save(update_fields=["status"])
        messages.error(request, f"Couldn't start the payment: {exc}")
        return redirect("dashboard:wallet_topup")
 
    return redirect(checkout_url)
 
 
@login_required
def payment_return_view(request):
    """
    Landing page after the operator completes payment on PayMongo's hosted checkout and gets
    redirected back. This does NOT credit the wallet -- that only ever happens from the webhook,
    since redirects aren't guaranteed to fire (closed tab, network blip, etc). This is purely a
    "we're confirming this" message; the dashboard will show the updated balance once the
    webhook has actually landed, which is typically near-instant but not synchronous with this
    redirect.
    """
    payment_ids = [int(i) for i in request.GET.get("payment_ids", "").split(",") if i.isdigit()]
    matched = Payment.objects.filter(id__in=payment_ids, account=request.user).count()
    if matched:
        messages.info(
            request,
            "Payment received — confirming now. Your wallet balance will update automatically "
            "in a few seconds once PayMongo confirms it.",
        )
    else:
        messages.info(request, "Payment step complete.")
    return redirect("dashboard:home")
 
 
@login_required
def payment_cancel_view(request):
    """Operator backed out of PayMongo's checkout page. Mark any still-pending Payments failed."""
    payment_ids = [int(i) for i in request.GET.get("payment_ids", "").split(",") if i.isdigit()]
    Payment.objects.filter(
        id__in=payment_ids, account=request.user, status="pending"
    ).update(status="failed")
    messages.error(request, "Payment was cancelled. Your wallet balance was not changed.")
    return redirect("dashboard:home")
 
 
@login_required
def account_settings_view(request):
    return render(
        request,
        "dashboard/account_settings.html",
        {"active_nav": "account", "balance_points": request.user.balance_points},
    )


@login_required
@require_http_methods(["GET", "POST"])
def send_points_view(request):
    """
    Peer-to-peer wallet transfer: the operator sends some of their own balance_points to another
    operator, identified by phone number. New build (not a bug fix) -- previously the only ways
    Account.balance_points ever moved were the admin's one-directional Gift Points action
    (superuser-only, no balance check) and a Machine top-up spend (single-account debit only).
    """
    if request.method == "GET":
        transfers = sorted(
            list(request.user.sent_transfers.all()) + list(request.user.received_transfers.all()),
            key=lambda t: t.created_at,
            reverse=True,
        )
        return render(
            request,
            "dashboard/send_points.html",
            {
                "active_nav": "send_points",
                "balance_points": request.user.balance_points,
                "transfers": transfers,
            },
        )

    recipient_phone = request.POST.get("recipient_phone", "")
    note = request.POST.get("note", "")

    try:
        amount = int(request.POST.get("amount", "0"))
    except ValueError:
        amount = 0

    if amount < 1:
        messages.error(request, "Enter a number greater than zero.")
        return redirect("dashboard:send_points")

    try:
        normalized_phone = normalize_phone_number(recipient_phone)
    except ValueError:
        messages.error(request, "That doesn't look like a valid PH mobile number.")
        return redirect("dashboard:send_points")

    recipient = Account.objects.filter(phone_number=normalized_phone).first()
    if recipient is None:
        messages.error(request, "No account with this phone number.")
        return redirect("dashboard:send_points")

    if recipient.pk == request.user.pk:
        messages.error(request, "You can't send points to yourself.")
        return redirect("dashboard:send_points")

    if request.user.balance_points < amount:
        messages.error(
            request,
            f"Not enough wallet balance for this transfer (need ₱{amount}, you have "
            f"₱{request.user.balance_points}).",
        )
        return redirect("dashboard:send_points")

    with db_transaction.atomic():
        # Lock both accounts in a fixed (ascending pk) order regardless of who's sending vs.
        # receiving here -- prevents a deadlock if two transfers between the same two accounts
        # cross in opposite directions at nearly the same instant.
        lower_pk, higher_pk = sorted([request.user.pk, recipient.pk])
        first_locked = Account.objects.select_for_update().get(pk=lower_pk)
        second_locked = Account.objects.select_for_update().get(pk=higher_pk)
        sender_locked = first_locked if first_locked.pk == request.user.pk else second_locked
        receiver_locked = second_locked if second_locked.pk == recipient.pk else first_locked

        if sender_locked.balance_points < amount:
            messages.error(request, "Not enough wallet balance for this transfer.")
            return redirect("dashboard:send_points")

        sender_locked.balance_points -= amount
        sender_locked.save(update_fields=["balance_points"])

        receiver_locked.balance_points += amount
        receiver_locked.save(update_fields=["balance_points"])

        PointTransfer.objects.create(
            sender=sender_locked,
            receiver=receiver_locked,
            amount=amount,
            note=note.strip()[:140],
        )

    messages.success(
        request, f"Sent ₱{amount} to {recipient.display_name or recipient.phone_number}."
    )
    return redirect("dashboard:send_points")

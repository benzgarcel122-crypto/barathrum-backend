from django.contrib.auth import logout as django_logout
from django.contrib.auth.decorators import login_required
# ...(rest of imports unchanged)

from django.contrib import messages
from django.contrib.auth import login as django_login
from django.contrib.auth import logout as django_logout
from django.contrib.auth.decorators import login_required
from django.http import JsonResponse
from django.shortcuts import redirect, render
from django.utils import timezone
from django.views.decorators.http import require_http_methods

from .models import Account, OTPCode, normalize_phone_number

# STEP 2.2 update: real HTML templates now exist for signup/verify/login, so @csrf_exempt has
# been removed from all three views (this was flagged as a STEP 2.2 follow-up in the STEP 2.1
# code comments). Real browser form submissions carry {% csrf_token %}; JSON API-style callers
# must now also carry a valid CSRF token (cookie + X-CSRFToken header), same as any other
# Django view — there is no longer a blanket exemption.


def _is_json_request(request):
    """True for JSON API-style calls (existing STEP 2.1 behavior); False for real form posts."""
    return "application/json" in (request.content_type or "")


def _parse_body(request):
    """Accept either JSON body or standard form-encoded POST data."""
    if _is_json_request(request):
        try:
            return json.loads(request.body or b"{}")
        except json.JSONDecodeError:
            return {}
    return request.POST


@require_http_methods(["GET", "POST"])
def signup_view(request):
    """
    STEP 1 of signup: phone_number + display_name -> generate + 'send' (console-log) an OTP.
    Does NOT create the Account yet — that happens in verify_view once the OTP is confirmed.
    GET renders the signup form. POST accepts JSON (existing behavior) or a real form submission.
    """
    if request.method == "GET":
        return render(request, "accounts/signup.html")

    data = _parse_body(request)
    phone_number = data.get("phone_number", "")
    display_name = data.get("display_name", "")
    wants_json = _is_json_request(request)

    if not phone_number:
        if wants_json:
            return JsonResponse({"error": "phone_number is required."}, status=400)
        return render(
            request,
            "accounts/signup.html",
            {"form_errors": {"phone_number": "Phone number is required."}, "form_values": data},
            status=400,
        )

    try:
        phone_number = normalize_phone_number(phone_number)
    except ValueError as exc:
        if wants_json:
            return JsonResponse({"error": str(exc)}, status=400)
        return render(
            request,
            "accounts/signup.html",
            {"form_errors": {"phone_number": "That doesn't look like a valid PH mobile number."},
             "form_values": data},
            status=400,
        )

    if Account.objects.filter(phone_number=phone_number).exists():
        error_msg = "An account with this phone number already exists. Use login instead."
        if wants_json:
            return JsonResponse({"error": error_msg}, status=400)
        return render(
            request,
            "accounts/signup.html",
            {"form_errors": {"non_field": error_msg}, "form_values": data},
            status=400,
        )

    request.session["pending_signup_display_name"] = display_name
    otp = OTPCode.issue(phone_number)

    if wants_json:
        return JsonResponse(
            {
                "status": "otp_sent",
                "phone_number": phone_number,
                "expires_at": otp.expires_at.isoformat(),
            }
        )

    return redirect(f"/verify/?phone={quote(phone_number)}")


@require_http_methods(["GET", "POST"])
def verify_view(request):
    """
    STEP 2 of signup/login: phone_number + code -> validate OTP, create/fetch the Account,
    log the user in. GET renders the verify form (phone number prefilled from ?phone=).
    """
    if request.method == "GET":
        return render(request, "accounts/verify.html", {"phone_number": request.GET.get("phone", "")})

    data = _parse_body(request)
    phone_number = data.get("phone_number", "")
    code = data.get("code", "")
    wants_json = _is_json_request(request)

    if not phone_number or not code:
        error_msg = "phone_number and code are required."
        if wants_json:
            return JsonResponse({"error": error_msg}, status=400)
        return render(
            request,
            "accounts/verify.html",
            {"phone_number": phone_number, "form_errors": {"code": error_msg}},
            status=400,
        )

    try:
        phone_number = normalize_phone_number(phone_number)
    except ValueError as exc:
        if wants_json:
            return JsonResponse({"error": str(exc)}, status=400)
        return render(
            request,
            "accounts/verify.html",
            {"phone_number": phone_number, "form_errors": {"code": str(exc)}},
            status=400,
        )

    # STEP 2.3 security fix: look up the PENDING otp by phone_number alone first (not
    # phone_number+code together). This matters because a wrong-code guess won't match any row
    # if we filter by code too -- we need a row to attach the failed_attempts counter to even
    # when the submitted code is wrong, otherwise brute-forcing is unthrottled by construction.
    pending_otp = (
        OTPCode.objects.filter(phone_number=phone_number, used=False)
        .order_by("-created_at")
        .first()
    )

    if pending_otp is None or timezone.now() >= pending_otp.expires_at:
        error_msg = "Invalid or expired code."
        if wants_json:
            return JsonResponse({"error": error_msg}, status=400)
        return render(
            request,
            "accounts/verify.html",
            {"phone_number": phone_number, "form_errors": {"code": error_msg}},
            status=400,
        )

    if pending_otp.failed_attempts >= OTPCode.MAX_FAILED_ATTEMPTS:
        error_msg = "Too many incorrect attempts. Request a new code."
        if wants_json:
            return JsonResponse({"error": error_msg}, status=429)
        return render(
            request,
            "accounts/verify.html",
            {"phone_number": phone_number, "form_errors": {"code": error_msg}},
            status=429,
        )

    if pending_otp.code != code:
        pending_otp.failed_attempts += 1
        pending_otp.save(update_fields=["failed_attempts"])
        error_msg = "Invalid or expired code."
        if wants_json:
            return JsonResponse({"error": error_msg}, status=400)
        return render(
            request,
            "accounts/verify.html",
            {"phone_number": phone_number, "form_errors": {"code": error_msg}},
            status=400,
        )

    otp = pending_otp
    otp.used = True
    otp.save(update_fields=["used"])

    account, created = Account.objects.get_or_create(
        phone_number=phone_number,
        defaults={
            "display_name": request.session.pop("pending_signup_display_name", ""),
            "is_verified": True,
        },
    )
    if not created and not account.is_verified:
        account.is_verified = True
        account.save(update_fields=["is_verified"])

    django_login(request, account, backend="django.contrib.auth.backends.ModelBackend")

    if wants_json:
        return JsonResponse(
            {
                "status": "verified",
                "account_created": created,
                "phone_number": account.phone_number,
                "display_name": account.display_name,
            }
        )

    messages.success(request, "You're logged in.")
    return redirect("dashboard:home")


@require_http_methods(["GET", "POST"])
def login_view(request):
    """
    Logout, POST-only. Deliberately not GET: a GET-triggered logout is a well-known CSRF/link-
    prefetch footgun (a stray <a href> or an over-eager browser prefetch/crawler can silently log
    a user out). Template side calls this via a small {% csrf_token %} form + button, not a link.
    """
    if request.method == "GET":
        return render(request, "accounts/login.html")

    data = _parse_body(request)
    phone_number = data.get("phone_number", "")
    wants_json = _is_json_request(request)

    if not phone_number:
        if wants_json:
            return JsonResponse({"error": "phone_number is required."}, status=400)
        return render(
            request,
            "accounts/login.html",
            {"form_errors": {"phone_number": "Phone number is required."}, "form_values": data},
            status=400,
        )

    try:
        phone_number = normalize_phone_number(phone_number)
    except ValueError as exc:
        if wants_json:
            return JsonResponse({"error": str(exc)}, status=400)
        return render(
            request,
            "accounts/login.html",
            {"form_errors": {"phone_number": "That doesn't look like a valid PH mobile number."},
             "form_values": data},
            status=400,
        )

    if not Account.objects.filter(phone_number=phone_number).exists():
        error_msg = "No account with this phone number. Use signup instead."
        if wants_json:
            return JsonResponse({"error": error_msg}, status=400)
        return render(
            request,
            "accounts/login.html",
            {"form_errors": {"non_field": error_msg}, "form_values": data},
            status=400,
        )

    otp = OTPCode.issue(phone_number)

    if wants_json:
        return JsonResponse(
            {
                "status": "otp_sent",
                "phone_number": phone_number,
                "expires_at": otp.expires_at.isoformat(),
            }
        )

    return redirect(f"/verify/?phone={quote(phone_number)}")


@login_required
@require_http_methods(["POST"])
def logout_view(request):
    """
    Logout, POST-only. Deliberately not GET: a GET-triggered logout is a well-known CSRF/link-
    prefetch footgun (a stray <a href> or an over-eager browser prefetch/crawler can silently log
    a user out). Template side calls this via a small {% csrf_token %} form + button, not a link.
    """
    django_logout(request)
    messages.info(request, "You've been logged out.")
    return redirect("login")

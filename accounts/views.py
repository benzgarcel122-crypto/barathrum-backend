import json

from django.contrib.auth import login as django_login
from django.http import JsonResponse
from django.utils import timezone
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_POST

from .models import Account, OTPCode, normalize_phone_number

# NOTE on scope: this project has no HTML templates beyond Django's built-in admin (per STEP 2.1
# BUILD item 6 / SCOPE). These views are plain Django views returning JSON so they're directly
# testable with curl/requests without a frontend. STEP 2.2 will add real templates against these
# same underlying flows; STEP 2.5 will add a separate DRF-style API for the box agent specifically.
#
# csrf_exempt is used here deliberately: there is no HTML form/template yet to carry a CSRF token,
# and these endpoints don't exist for the box agent (that's STEP 2.5, out of scope for this task).
# STEP 2.2 should revisit this once real forms exist.


def _parse_body(request):
    """Accept either JSON body or standard form-encoded POST data."""
    if request.content_type == "application/json":
        try:
            return json.loads(request.body or b"{}")
        except json.JSONDecodeError:
            return {}
    return request.POST


@csrf_exempt
@require_POST
def signup_view(request):
    """
    STEP 1 of signup: phone_number + display_name -> generate + 'send' (console-log) an OTP.
    Does NOT create the Account yet — that happens in verify_view once the OTP is confirmed.
    """
    data = _parse_body(request)
    phone_number = data.get("phone_number", "")
    display_name = data.get("display_name", "")

    if not phone_number:
        return JsonResponse({"error": "phone_number is required."}, status=400)

    try:
        phone_number = normalize_phone_number(phone_number)
    except ValueError as exc:
        return JsonResponse({"error": str(exc)}, status=400)

    if Account.objects.filter(phone_number=phone_number).exists():
        return JsonResponse(
            {"error": "An account with this phone number already exists. Use login instead."},
            status=400,
        )

    request.session["pending_signup_display_name"] = display_name
    otp = OTPCode.issue(phone_number)

    return JsonResponse(
        {
            "status": "otp_sent",
            "phone_number": phone_number,
            "expires_at": otp.expires_at.isoformat(),
        }
    )


@csrf_exempt
@require_POST
def verify_view(request):
    """
    STEP 2 of signup: phone_number + code -> validate OTP, create the Account, log the user in.
    """
    data = _parse_body(request)
    phone_number = data.get("phone_number", "")
    code = data.get("code", "")

    if not phone_number or not code:
        return JsonResponse({"error": "phone_number and code are required."}, status=400)

    try:
        phone_number = normalize_phone_number(phone_number)
    except ValueError as exc:
        return JsonResponse({"error": str(exc)}, status=400)

    otp = (
        OTPCode.objects.filter(phone_number=phone_number, code=code, used=False)
        .order_by("-created_at")
        .first()
    )

    if otp is None or not otp.is_valid():
        return JsonResponse({"error": "Invalid or expired code."}, status=400)

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

    return JsonResponse(
        {
            "status": "verified",
            "account_created": created,
            "phone_number": account.phone_number,
            "display_name": account.display_name,
        }
    )


@csrf_exempt
@require_POST
def login_view(request):
    """
    Login for an existing Account: same OTP generate flow as signup, but requires the phone
    number to already be registered. Verification happens via the same verify_view.
    """
    data = _parse_body(request)
    phone_number = data.get("phone_number", "")

    if not phone_number:
        return JsonResponse({"error": "phone_number is required."}, status=400)

    try:
        phone_number = normalize_phone_number(phone_number)
    except ValueError as exc:
        return JsonResponse({"error": str(exc)}, status=400)

    if not Account.objects.filter(phone_number=phone_number).exists():
        return JsonResponse(
            {"error": "No account with this phone number. Use signup instead."}, status=400
        )

    otp = OTPCode.issue(phone_number)

    return JsonResponse(
        {
            "status": "otp_sent",
            "phone_number": phone_number,
            "expires_at": otp.expires_at.isoformat(),
        }
    )

import json
from datetime import timedelta

from django.contrib.auth.hashers import check_password
from django.core.cache import cache
from django.http import JsonResponse
from django.utils import timezone
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_POST

from .models import License

# --- Box-pairing license validation -----------------------------------------------------------
#
# Rate limiting: fixed-window counter keyed by client IP, stored in Django's default cache
# (LocMemCache -- no CACHES override in settings.py, and the Procfile runs a single unthreaded
# gunicorn worker with no --workers/--threads flag, so LocMemCache is a single shared in-process
# dict for the whole app, not per-request-isolated). That makes it a correct, good-enough limiter
# for this MVP without adding a Redis dependency -- but it's worth flagging for whoever eventually
# changes the Procfile to add workers/threads or multiple Railway replicas, since LocMemCache does
# NOT share state across separate processes/machines. At that point this needs a real shared store
# (e.g. Redis via django-redis).
#
# Threshold: 10 attempts per IP per 5-minute window. Chosen to comfortably cover a legitimate
# operator mistyping their 15-character license key a few times during setup, while still making
# brute-forcing the 33^15 keyspace via this endpoint practically useless (an attacker gets at most
# 10 guesses per 5 minutes per IP, i.e. ~2,880/day -- negligible against that keyspace, and each
# extra IP needed to scale the attack is its own cost). No separate per-license_key-attempted
# limit added on top: the per-IP window already caps the guess rate for a single attacker, and the
# license_key itself isn't attacker-controlled input worth keying a second counter on for MVP.
RATE_LIMIT_MAX_ATTEMPTS = 10
RATE_LIMIT_WINDOW_SECONDS = 300


def _client_ip(request):
    """
    Railway's proxy terminates TLS and forwards to the app, setting X-Forwarded-For with the
    real client IP -- same trust boundary already assumed by SECURE_PROXY_SSL_HEADER in
    settings.py for X-Forwarded-Proto. Take the leftmost (original client) entry; fall back to
    REMOTE_ADDR for local dev / the Django test client, neither of which set this header.
    """
    forwarded_for = request.META.get("HTTP_X_FORWARDED_FOR", "")
    if forwarded_for:
        return forwarded_for.split(",")[0].strip()
    return request.META.get("REMOTE_ADDR", "unknown")


def _rate_limited(request):
    """
    True (and increments the counter) if this IP has hit RATE_LIMIT_MAX_ATTEMPTS within the
    current RATE_LIMIT_WINDOW_SECONDS window. Fixed-window (not sliding-window) counter --
    simplest correct implementation for a cache.incr()-backed counter; the worst case (a burst
    right at a window boundary allowing up to ~2x the nominal rate briefly) is an acceptable
    trade for MVP given how far below the actual brute-force-relevant rate this threshold already
    sits.
    """
    cache_key = f"box_validate_license:ratelimit:{_client_ip(request)}"
    try:
        attempts = cache.incr(cache_key)
    except ValueError:
        # Key doesn't exist yet (first request in a new window, or it just expired).
        cache.set(cache_key, 1, timeout=RATE_LIMIT_WINDOW_SECONDS)
        attempts = 1
    return attempts > RATE_LIMIT_MAX_ATTEMPTS


@csrf_exempt
@require_POST
def validate_license_view(request):
    """
    POST /api/box/validate-license/ -- box-side license ACTIVATION endpoint for the box-agent
    Setup Wizard (barathrum-box-agent's portal_app.py::setup_wizard, screen 1).

    Built for real, Session 86 (MPD), replacing the original read-only, claim-gated version from
    Session 62-63. This is the real activation write-path first proposed as an unbuilt design in
    Session 65-66 (tracker row 19) -- PM explicitly chose to build it now rather than wait for
    STEP 1 hardware, since this half of the work (the Django/Python side) needs no physical box to
    build or test correctly, unlike the box-side GPIO/hostapd work, which genuinely does.

    What changed from the original version:
    - No longer requires License.is_claimed to be True. Per the End Goals (MPD, Session 82)
      closing note, activation and claiming are independent events that can happen in either
      order -- a license can be validated/activated at the box before anyone ever clicks Add
      Machine on the dashboard. The old 409 "hasn't been claimed yet" gate is removed entirely.
    - This call now WRITES: the first time a real box successfully validates an existing license
      key, License.activated_at is set to now(). Previously this endpoint was purely read-only.
    - Idempotent by construction: uses .filter(activated_at__isnull=True).update(...) rather than
      a read-then-write pattern, so two near-simultaneous first-calls for the same key can't both
      "win" and produce inconsistent state -- exactly one of them actually sets the timestamp,
      and both still receive the same 200 "valid" response either way (the caller doesn't need to
      know or care which request happened to win the race).

    Known, explicitly accepted limitation -- NOT resolved by this change, still gated on real
    hardware (Session 65 open question #2, unchanged): there is no way today to distinguish "the
    same physical box re-pairing after a reflash" from "a different box presenting a key that was
    already activated elsewhere." Both cases hit this same code path and both succeed. This
    endpoint does not attempt to guess a box-identity signal (MAC address, install token, etc.)
    that doesn't exist yet -- building a wrong guess now would be worse than leaving this
    honestly open, per this project's own standing preference for flagging real forks rather than
    inventing an answer. This means, today, any device that knows a real, previously-activated
    license key can "re-validate" successfully -- acceptable for now because the license_key
    itself is already the sole credential this whole flow relies on (same trust model the
    endpoint has always used), and because closing this gap requires information (real box
    identity) that simply doesn't exist until STEP 1 hardware does.

    Rate limiting and normalization unchanged from the original version -- still mirrors
    machines/webhooks.py's paymongo_webhook_view pattern (csrf_exempt + require_POST), still the
    same license_key normalization as add_machine_view's, still the same 400 (malformed/empty) and
    404 (nonexistent key) handling.
    """
    if _rate_limited(request):
        return JsonResponse(
            {"valid": False, "message": "Too many attempts. Please wait a few minutes and try again."},
            status=429,
        )

    try:
        payload = json.loads(request.body or b"{}")
    except json.JSONDecodeError:
        return JsonResponse({"valid": False, "message": "Malformed request body."}, status=400)

    # Same normalization as dashboard/views.py::add_machine_view's license_key_input handling --
    # one normalization rule, not a second slightly-different one for this endpoint.
    license_key = (payload.get("license_key") or "").strip().upper()

    if not license_key:
        return JsonResponse({"valid": False, "message": "Enter your license key."}, status=400)

    try:
        license_obj = License.objects.get(license_key=license_key)
    except License.DoesNotExist:
        # Deliberately the same generic message/shape as any other failure case's structure
        # (a `valid`/`message` pair, no extra fields) -- only the status code and wording differ,
        # so a timing or response-shape difference beyond the status code itself doesn't leak
        # which case actually happened, per this task's explicit requirement.
        return JsonResponse({"valid": False, "message": "License key not recognized."}, status=404)

    # Activation write: idempotent, race-safe first-activation. Matches (and updates) exactly one
    # row only the first time this key is ever validated -- a second, third, Nth call for an
    # already-activated key matches zero rows here (activated_at is no longer NULL) and simply
    # falls through to the same 200 response below, since re-validating an already-activated key
    # is a normal, expected, successful case, not an error.
    License.objects.filter(pk=license_obj.pk, activated_at__isnull=True).update(
        activated_at=timezone.now()
    )

    return JsonResponse(
        {"valid": True, "message": "License validated.", "license_points": license_obj.license_points},
        status=200,
    )


# --- Box-side license points sync (End Goals #17) ---------------------------------------------
#
# Separate, dedicated rate-limit cache key from validate_license_view's -- box-agent polls this
# endpoint periodically (every LICENSE_POINTS_SYNC_INTERVAL_SECONDS, box-side), which is a
# distinct usage pattern from the one-shot Setup Wizard bind call, so the two counters must never
# share or interfere with each other's remaining budget for the same IP.
def _license_points_rate_limited(request):
    cache_key = f"box_license_points:ratelimit:{_client_ip(request)}"
    try:
        attempts = cache.incr(cache_key)
    except ValueError:
        cache.set(cache_key, 1, timeout=RATE_LIMIT_WINDOW_SECONDS)
        attempts = 1
    return attempts > RATE_LIMIT_MAX_ATTEMPTS


@csrf_exempt
@require_POST
def license_points_view(request):
    """
    POST /api/box/license-points/ -- box-side periodic poll target (End Goals #17). Read-only:
    returns the current license_points balance for an already-known license_key. Same trust
    model as validate_license_view (the key itself is the credential) and the same normalization/
    error shape, deliberately -- this is a peer endpoint, not a new pattern.
    """
    if _license_points_rate_limited(request):
        return JsonResponse(
            {"valid": False, "message": "Too many attempts. Please wait a few minutes and try again."},
            status=429,
        )
    try:
        payload = json.loads(request.body or b"{}")
    except json.JSONDecodeError:
        return JsonResponse({"valid": False, "message": "Malformed request body."}, status=400)

    license_key = (payload.get("license_key") or "").strip().upper()
    if not license_key:
        return JsonResponse({"valid": False, "message": "Enter your license key."}, status=400)

    try:
        license_obj = License.objects.get(license_key=license_key)
    except License.DoesNotExist:
        return JsonResponse({"valid": False, "message": "License key not recognized."}, status=404)

    return JsonResponse(
        {"valid": True, "license_points": license_obj.license_points}, status=200
    )


# --- Box-side license unbind (End Goals #20) ---------------------------------------------------
#
# Separate, dedicated rate-limit cache key from every other box-facing endpoint's -- same
# reasoning as license_points_view's own counter above: a distinct usage pattern (a rare,
# deliberate operator action, not a periodic poll) deserves its own budget that can never be
# exhausted by, or exhaust, any other endpoint's counter for the same IP.
def _unbind_license_rate_limited(request):
    cache_key = f"box_unbind_license:ratelimit:{_client_ip(request)}"
    try:
        attempts = cache.incr(cache_key)
    except ValueError:
        cache.set(cache_key, 1, timeout=RATE_LIMIT_WINDOW_SECONDS)
        attempts = 1
    return attempts > RATE_LIMIT_MAX_ATTEMPTS


@csrf_exempt
@require_POST
def unbind_license_view(request):
    """
    POST /api/box/unbind-license/ -- End Goals #20. Box-side deactivation, gated by the
    license's own recovery password (set once at generation time, dashboard/views.py's old
    release_license_view used to check this; that check moves here, box-side, since the box
    is the thing actually being deactivated). Reuses License.release_failed_attempts /
    release_locked_until -- same 5-attempt / 15-minute lockout semantics as before, just a
    different caller.

    On success: License.activated_at is cleared (set to None) -- the license's status becomes
    "deactivated but still claimed" (End Goal #20's own wording) if a Machine still claims it,
    or simply fully inert if it was already unclaimed. This does NOT touch license_points or
    account/claim status at all -- those are independent axes (End Goals closing note).
    """
    if _unbind_license_rate_limited(request):
        return JsonResponse(
            {"valid": False, "message": "Too many attempts. Please wait a few minutes and try again."},
            status=429,
        )
    try:
        payload = json.loads(request.body or b"{}")
    except json.JSONDecodeError:
        return JsonResponse({"valid": False, "message": "Malformed request body."}, status=400)

    license_key = (payload.get("license_key") or "").strip().upper()
    password = payload.get("password", "")
    if not license_key:
        return JsonResponse({"valid": False, "message": "Enter your license key."}, status=400)

    try:
        license_obj = License.objects.get(license_key=license_key)
    except License.DoesNotExist:
        return JsonResponse({"valid": False, "message": "License key not recognized."}, status=404)

    now = timezone.now()
    if license_obj.release_locked_until and now < license_obj.release_locked_until:
        remaining = int((license_obj.release_locked_until - now).total_seconds())
        return JsonResponse(
            {"valid": False, "message": f"Too many incorrect attempts. Try again in {remaining} seconds."},
            status=423,
        )

    if not check_password(password, license_obj.recovery_password_hash):
        license_obj.release_failed_attempts += 1
        if license_obj.release_failed_attempts >= License.RELEASE_MAX_FAILED_ATTEMPTS:
            license_obj.release_locked_until = now + timedelta(minutes=15)
            license_obj.release_failed_attempts = 0
        license_obj.save(update_fields=["release_failed_attempts", "release_locked_until"])
        return JsonResponse({"valid": False, "message": "Incorrect recovery password."}, status=403)

    License.objects.filter(pk=license_obj.pk).update(
        activated_at=None, release_failed_attempts=0, release_locked_until=None
    )
    return JsonResponse({"valid": True, "message": "License unbound."}, status=200)

import json

from django.core.cache import cache
from django.http import JsonResponse
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_POST

from .models import License, Machine

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
    POST /api/box/validate-license/ -- read-only license-key validation for the box-agent Setup
    Wizard (barathrum-box-agent's portal_app.py::setup_wizard, screen 1). Mirrors
    machines/webhooks.py's paymongo_webhook_view pattern (csrf_exempt + require_POST) since, like
    that endpoint, the caller is another machine (a box on the operator's LAN) with no Django
    session/CSRF token to present -- the license_key itself is the only credential, per this
    task's explicit non-goal of not building a separate box auth system.

    This does NOT create or modify any License/Machine row -- it only reads state that
    dashboard/views.py::add_machine_view already establishes via the one real claim flow. "Claimed"
    here is defined identically to License.is_claimed (an ACTIVE Machine, removed_at IS NULL,
    exists for this license_key) -- deliberately not re-implemented as a second definition.

    Response shape: as of this task, barathrum-box-agent's setup_wizard() has this call fully
    stubbed (accepts any non-empty key, comment literally says "STUB: real validation call... goes
    here") and its setup.html template displays nothing beyond the raw input field -- confirmed by
    reading both files directly. There is no existing response contract on the box side to match
    yet, so the shape below is a forward-looking, deliberately minimal design (a `valid` boolean
    the wizard can branch on, plus a `message` it can show verbatim on failure) rather than a
    guess at fields nothing currently consumes. Wiring the box side to actually call this endpoint
    is explicitly a separate follow-up task per this task's own scope.
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
        # Deliberately the same generic message/shape as the "not yet claimed" case's structure
        # (a `valid`/`message` pair, no extra fields) -- only the status code and wording differ,
        # so a timing or response-shape difference beyond the status code itself doesn't leak
        # which case actually happened, per this task's explicit requirement.
        return JsonResponse({"valid": False, "message": "License key not recognized."}, status=404)

    # Same query add_machine_view already runs (via License.is_claimed, defined identically in
    # machines/models.py) -- not a second definition of "claimed."
    is_claimed = Machine.objects.filter(
        license_key=license_obj.license_key, removed_at__isnull=True
    ).exists()

    if not is_claimed:
        # 409 Conflict: the license_key resource exists (so 404 would be wrong), but its current
        # state conflicts with what box-pairing requires of it (an active claim) -- 409 is the
        # standard status for "request is valid, but the resource's current state doesn't allow
        # it," which is exactly this case. (410 Gone was considered and rejected: this isn't a
        # permanently-dead resource, it's expected to transition to claimed via a normal dashboard
        # action, which 410's semantics don't fit.)
        return JsonResponse(
            {
                "valid": False,
                "message": (
                    "This license hasn't been claimed yet — log into your Barathrum dashboard "
                    "and claim it before finishing box setup."
                ),
            },
            status=409,
        )

    return JsonResponse({"valid": True, "message": "License validated."}, status=200)

import json

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

    return JsonResponse({"valid": True, "message": "License validated."}, status=200)

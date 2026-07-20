import hashlib
import hmac
import json
import os
import time

from django.db import transaction as db_transaction
from django.http import HttpResponse, HttpResponseBadRequest
from django.utils import timezone
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_POST

from .models import Payment, Transaction

# Set in Railway's Variables tab -- this is the "Signing Secret" PayMongo shows when you create
# the webhook endpoint in their dashboard, NOT the same as PAYMONGO_SECRET_KEY (the API key).
PAYMONGO_WEBHOOK_SECRET = os.environ.get("PAYMONGO_WEBHOOK_SECRET", "")

# Reject signatures older than this, as defense-in-depth against replaying a captured (but
# validly-signed) request. PayMongo's docs don't mandate a specific tolerance; this mirrors the
# window Stripe recommends for the same HMAC-over-timestamp.payload scheme.
SIGNATURE_TOLERANCE_SECONDS = 300


def verify_paymongo_signature(raw_body: bytes, signature_header: str, secret: str) -> bool:
    """
    Verify PayMongo's `Paymongo-Signature` header.

    Header format: "t=<unix_timestamp>,te=<test_mode_hex_hmac>,li=<live_mode_hex_hmac>"
    The signed value is HMAC-SHA256(secret, f"{timestamp}.{raw_body}"), hex-encoded.

    This checks the `te=` (test mode) signature only -- this integration is sandbox/test-mode
    only per STEP 2.3 scope, so `li=` (live mode) is deliberately never read here. Wiring up live
    mode is a separate, explicit decision for a future task, not something to silently support
    by accident.

    CRITICAL: raw_body must be the exact, unparsed request bytes. Any re-serialization (e.g.
    json.loads then json.dumps) will very likely change whitespace/key-ordering and break the
    HMAC even for a legitimate request -- see PayMongo's own troubleshooting docs on this.
    """
    if not signature_header or not secret:
        return False

    parts = {}
    for chunk in signature_header.split(","):
        if "=" not in chunk:
            continue
        key, _, value = chunk.partition("=")
        parts[key.strip()] = value.strip()

    timestamp_str = parts.get("t")
    provided_signature = parts.get("te")
    if not timestamp_str or not provided_signature:
        return False

    try:
        timestamp = int(timestamp_str)
    except ValueError:
        return False

    if abs(time.time() - timestamp) > SIGNATURE_TOLERANCE_SECONDS:
        return False

    signed_payload = f"{timestamp_str}.{raw_body.decode('utf-8')}".encode("utf-8")
    expected_signature = hmac.new(secret.encode("utf-8"), signed_payload, hashlib.sha256).hexdigest()

    # Constant-time comparison -- a naive `==` leaks timing information an attacker could use to
    # forge a valid signature one byte at a time.
    return hmac.compare_digest(expected_signature, provided_signature)


@csrf_exempt
@require_POST
def paymongo_webhook_view(request):
    """
    PayMongo calls this after a checkout session's payment completes. We only ever act on
    `checkout_session.payment.paid` -- every other event type is acknowledged (200) and ignored.

    NOTE for whoever tests this against a real PayMongo sandbox delivery: the exact shape of
    `data.attributes.data` below is built from PayMongo's documented event examples, but I could
    not send myself a real webhook to confirm byte-for-byte (no outbound network access to
    PayMongo from this sandbox -- see the STEP 2.3 report). If field names don't match on a real
    delivery, check the raw payload in PayMongo's dashboard webhook logs first.
    """
    raw_body = request.body  # MUST read raw bytes before any parsing -- see verify function above
    signature_header = request.headers.get("Paymongo-Signature", "")

    if not verify_paymongo_signature(raw_body, signature_header, PAYMONGO_WEBHOOK_SECRET):
        return HttpResponseBadRequest("Invalid signature")

    try:
        event = json.loads(raw_body)
    except json.JSONDecodeError:
        return HttpResponseBadRequest("Invalid JSON")

    event_attributes = event.get("data", {}).get("attributes", {})
    event_type = event_attributes.get("type", "")

    if event_type != "checkout_session.payment.paid":
        # Acknowledge but no-op for event types we're not subscribed to / don't act on, per
        # PayMongo's guidance to always return 2xx once the request is authenticated.
        return HttpResponse(status=200)

    checkout_session = event_attributes.get("data", {})
    checkout_session_id = checkout_session.get("id")
    if not checkout_session_id:
        return HttpResponseBadRequest("Missing checkout session id in webhook payload")

    with db_transaction.atomic():
        # select_for_update: real row-level locking on Postgres (production) against a
        # concurrent duplicate delivery arriving at nearly the same moment; a no-op on SQLite
        # (local dev), which is fine since SQLite serializes writers at the database level anyway.
        pending_payments = list(
            Payment.objects.select_for_update()
            .filter(paymongo_checkout_session_id=checkout_session_id, status="pending")
        )

        # Idempotency guard: PayMongo can and does redeliver the same event. On a redelivery,
        # every Payment row for this session is already "paid" by the first delivery, so this
        # comes back empty and we no-op instead of double-applying days_remaining/Transactions.
        if not pending_payments:
            return HttpResponse(status=200)

        for payment in pending_payments:
            machine = payment.machine
            machine.days_remaining += payment.days
            machine.last_topup_bundle_type = payment.bundle_type
            machine.save(update_fields=["days_remaining", "last_topup_bundle_type"])

            Transaction.objects.create(
                machine=machine,
                bundle_type=payment.bundle_type,
                days_added=payment.days,
                amount_paid_pesos=payment.amount_pesos,
            )

            payment.status = "paid"
            payment.paid_at = timezone.now()
            payment.save(update_fields=["status", "paid_at"])

    return HttpResponse(status=200)

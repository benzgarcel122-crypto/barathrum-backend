import base64
import os

import requests

# Sandbox/test key only, per STEP 2.3 scope — never a live key. Set in Railway's Variables tab,
# never pasted into chat/committed to the repo.
PAYMONGO_SECRET_KEY = os.environ.get("PAYMONGO_SECRET_KEY", "")
PAYMONGO_API_BASE = "https://api.paymongo.com/v1"


class PayMongoAPIError(Exception):
    """Raised for any failure talking to PayMongo — missing key, network error, or a 4xx/5xx."""


def _auth_header():
    if not PAYMONGO_SECRET_KEY:
        raise PayMongoAPIError(
            "PAYMONGO_SECRET_KEY is not set. Add it in Railway's Variables tab (test key only, "
            "e.g. sk_test_...)."
        )
    # PayMongo uses HTTP Basic auth with the secret key as the username and an empty password.
    token = base64.b64encode(f"{PAYMONGO_SECRET_KEY}:".encode()).decode()
    return {"Authorization": f"Basic {token}", "Content-Type": "application/json"}


def create_checkout_session(
    line_items, payment_method_types, success_url, cancel_url, reference_number, description,
    timeout=15,
):
    """
    Create a PayMongo Checkout Session. Returns (session_id, checkout_url).

    line_items: list of {"currency": "PHP", "amount": <centavos:int>, "name": str, "quantity": int}
    PayMongo amounts are always centavos (pesos * 100) -- callers must convert before calling this.

    Docs: https://developers.paymongo.com/docs/checkout-api
    """
    payload = {
        "data": {
            "attributes": {
                "send_email_receipt": False,
                "show_description": True,
                "show_line_items": True,
                "description": description,
                "line_items": line_items,
                "payment_method_types": payment_method_types,
                "success_url": success_url,
                "cancel_url": cancel_url,
                "reference_number": reference_number,
            }
        }
    }

    try:
        response = requests.post(
            f"{PAYMONGO_API_BASE}/checkout_sessions",
            json=payload,
            headers=_auth_header(),
            timeout=timeout,
        )
    except requests.RequestException as exc:
        raise PayMongoAPIError(f"Could not reach PayMongo: {exc}") from exc

    if response.status_code >= 400:
        raise PayMongoAPIError(f"PayMongo returned {response.status_code}: {response.text}")

    try:
        body = response.json()
        data = body["data"]
        session_id = data["id"]
        checkout_url = data["attributes"]["checkout_url"]
    except (ValueError, KeyError) as exc:
        raise PayMongoAPIError(f"Unexpected PayMongo response shape: {response.text}") from exc

    return session_id, checkout_url

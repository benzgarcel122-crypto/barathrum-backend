import os

import requests

# Real key only, set in Railway's Variables tab -- never hardcoded, never committed. Same pattern
# as PAYMONGO_SECRET_KEY in machines/paymongo_client.py.
SEMAPHORE_API_KEY = os.environ.get("SEMAPHORE_API_KEY", "")

# Semaphore's dedicated OTP-traffic endpoint (STEP 2.1a, Session 50 decision) -- NOT the regular
# /api/v4/messages endpoint. Confirmed against Semaphore's own docs (https://www.semaphore.co/docs,
# "OTP Messages" section) this session: this endpoint is explicitly for OTP traffic, is not rate
# limited (unlike /messages, which is capped at 120/min), and accepts a caller-supplied "code"
# parameter so Semaphore sends OUR already-generated code instead of minting its own.
SEMAPHORE_OTP_URL = "https://api.semaphore.co/api/v4/otp"

# "{otp}" is Semaphore's documented placeholder -- it gets replaced with whatever `code` value we
# pass, not an auto-generated one, since we pass `code` explicitly below.
OTP_MESSAGE_TEMPLATE = (
    "Your Barathrum verification code is {otp}. It expires in 5 minutes. "
    "Don't share this code with anyone."
)


class SemaphoreAPIError(Exception):
    """Raised for any failure talking to Semaphore -- missing key, network error, or a 4xx/5xx."""


def send_otp(phone_number, code, timeout=15):
    """
    Send `phone_number` an SMS carrying OUR already-generated `code` via Semaphore's OTP endpoint.

    `code` is OTPCode.generate_code()'s output (see accounts/models.py::OTPCode.issue), passed
    through Semaphore's "code" override parameter so Semaphore sends OUR code rather than minting
    its own -- the two must stay in sync, since OTPCode.is_valid() only ever checks the code we
    stored, never anything Semaphore might generate independently.

    Raises SemaphoreAPIError immediately if SEMAPHORE_API_KEY is unset, on any network failure, or
    on any 4xx/5xx response -- never silently no-ops. Fail-closed by construction: the caller
    (OTPCode.issue) does not persist an OTPCode row unless this function returns without raising.

    Docs confirmed this session at https://www.semaphore.co/docs ("OTP Messages" section):
      POST https://api.semaphore.co/api/v4/otp
      Body is FORM-ENCODED, not JSON -- every example in Semaphore's own docs (curl --data,
      PHP http_build_query, the Python code sample) posts apikey/number/message/code as
      x-www-form-urlencoded fields, never a JSON body. This deviates from paymongo_client.py's
      json= pattern for that reason.
      Response: a JSON array (even for a single recipient), each element carrying the delivery
      status and the "code" that was actually sent.

    ASSUMPTION FLAGGED (not verified against a live Semaphore account/real send this session --
    see REPORT BACK): the leading "+" is stripped from phone_number before sending. Semaphore's
    docs never show a "+"-prefixed example, and their own OTP response sample shows the recipient
    echoed back as "639998887777" (no plus), so this seemed like the safer default, but it was not
    confirmed end-to-end against a real Semaphore delivery.
    """
    if not SEMAPHORE_API_KEY:
        raise SemaphoreAPIError(
            "SEMAPHORE_API_KEY is not set. Add it in Railway's Variables tab."
        )

    payload = {
        "apikey": SEMAPHORE_API_KEY,
        "number": phone_number.lstrip("+"),
        "message": OTP_MESSAGE_TEMPLATE,
        "code": code,
    }

    try:
        response = requests.post(SEMAPHORE_OTP_URL, data=payload, timeout=timeout)
    except requests.RequestException as exc:
        raise SemaphoreAPIError(f"Could not reach Semaphore: {exc}") from exc

    if response.status_code >= 400:
        raise SemaphoreAPIError(f"Semaphore returned {response.status_code}: {response.text}")

    return response.json()

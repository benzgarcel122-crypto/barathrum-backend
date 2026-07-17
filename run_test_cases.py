"""
One-off verification script for STEP 2.1 test cases 2-6.
Run with: python manage.py shell < run_test_cases.py
(Test case 1 — migrate on fresh DB — and createsuperuser were already run/reported separately.)
"""
import django
from django.test import Client
from django.contrib.auth import get_user_model
from django.db import IntegrityError, transaction as db_transaction

Account = get_user_model()
from accounts.models import OTPCode
from machines.models import Machine, Transaction, generate_unique_license_key, LICENSE_KEY_ALPHABET

client = Client()

print("=" * 70)
print("TEST CASE 2: /admin/ shows Account, OTPCode, Machine, Transaction")
print("=" * 70)
from django.contrib import admin as django_admin
registered = [m.__name__ for m in django_admin.site._registry.keys()]
print("Registered admin models:", registered)
for expected in ["Account", "OTPCode", "Machine", "Transaction"]:
    print(f"  {expected} registered: {expected in registered}")

print()
print("=" * 70)
print("TEST CASE 3: signup view issues an OTP (printed to console)")
print("=" * 70)
resp = client.post("/signup/", {"phone_number": "09171112222", "display_name": "Juan Dela Cruz"})
print("status:", resp.status_code, "body:", resp.json())
signup_phone = resp.json()["phone_number"]
otp = OTPCode.objects.filter(phone_number=signup_phone).order_by("-created_at").first()
print("OTP row in DB -> phone:", otp.phone_number, "code:", otp.code, "used:", otp.used)

print()
print("=" * 70)
print("TEST CASE 4: verify view creates Account + authenticates session")
print("=" * 70)
resp = client.post("/verify/", {"phone_number": signup_phone, "code": otp.code})
print("status:", resp.status_code, "body:", resp.json())
acct = Account.objects.filter(phone_number=signup_phone).first()
print("Account created:", acct is not None, "| phone:", acct.phone_number, "| is_verified:", acct.is_verified)
print("Session authenticated (_auth_user_id in client session):", "_auth_user_id" in client.session)

# Wrong-code / reuse should fail now
resp_reuse = client.post("/verify/", {"phone_number": signup_phone, "code": otp.code})
print("Reusing same OTP again -> status:", resp_reuse.status_code, "body:", resp_reuse.json())

print()
print("=" * 70)
print("TEST CASE 5: auto-generated license_key is 15 chars, excludes 0/O/1/I/l")
print("=" * 70)
m1 = Machine.objects.create(owner=acct, nickname="Sari-Sari Store A")
print("Generated license_key:", m1.license_key)
print("Length == 15:", len(m1.license_key) == 15)
forbidden = set("0O1Il")
print("Contains none of 0/O/1/I/l:", not (set(m1.license_key) & forbidden))
print("Full allowed alphabet used by generator:", LICENSE_KEY_ALPHABET)

print()
print("=" * 70)
print("TEST CASE 6: license_key uniqueness enforced")
print("=" * 70)
m2 = Machine.objects.create(owner=acct, nickname="Sari-Sari Store B")
print("Second machine key:", m2.license_key, "| different from first:", m2.license_key != m1.license_key)

# Force an actual collision to prove the DB constraint (not just the pre-check) holds.
try:
    with db_transaction.atomic():
        Machine.objects.create(owner=acct, nickname="Forced collision attempt", license_key=m1.license_key)
    print("FAIL: forced-collision Machine was created — uniqueness NOT enforced!")
except IntegrityError as e:
    print("PASS: forced collision correctly rejected by DB unique constraint.")
    print("  IntegrityError:", str(e).splitlines()[0])

print()
print("Final counts -> Accounts:", Account.objects.count(), "Machines:", Machine.objects.count(),
      "OTPCodes:", OTPCode.objects.count())

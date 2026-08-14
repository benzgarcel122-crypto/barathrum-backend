import math
import re
import secrets
from datetime import timedelta

from django.conf import settings
from django.contrib.auth.base_user import AbstractBaseUser, BaseUserManager
from django.contrib.auth.models import PermissionsMixin
from django.db import models
from django.utils import timezone

from . import semaphore_client


def normalize_phone_number(raw_phone_number):
    """
    Normalize a PH phone number to a consistent +63XXXXXXXXXX format.

    Accepts common input variants an operator might type:
      - "09171234567"      -> "+639171234567"
      - "9171234567"       -> "+639171234567"
      - "639171234567"     -> "+639171234567"
      - "+639171234567"    -> "+639171234567" (unchanged)

    Raises ValueError if the digits don't resolve to a plausible PH mobile number.
    """
    digits = re.sub(r"\D", "", raw_phone_number or "")

    if digits.startswith("63") and len(digits) == 12:
        national = digits[2:]
    elif digits.startswith("0") and len(digits) == 11:
        national = digits[1:]
    elif len(digits) == 10:
        national = digits
    else:
        raise ValueError(f"Could not normalize phone number: {raw_phone_number!r}")

    if not national.startswith("9") or len(national) != 10:
        raise ValueError(f"Not a plausible PH mobile number: {raw_phone_number!r}")

    return f"+63{national}"


class AccountManager(BaseUserManager):
    def create_user(self, phone_number, display_name="", password=None, **extra_fields):
        if not phone_number:
            raise ValueError("Accounts must have a phone_number.")
        phone_number = normalize_phone_number(phone_number)
        user = self.model(phone_number=phone_number, display_name=display_name, **extra_fields)
        if password:
            user.set_password(password)
        else:
            # OTP-authenticated accounts have no usable password.
            user.set_unusable_password()
        user.save(using=self._db)
        return user

    def create_superuser(self, phone_number, display_name="", password=None, **extra_fields):
        extra_fields.setdefault("is_staff", True)
        extra_fields.setdefault("is_superuser", True)
        extra_fields.setdefault("is_verified", True)

        if not password:
            raise ValueError("Superusers must have a password (used for /admin/ login).")
        if extra_fields.get("is_staff") is not True:
            raise ValueError("Superuser must have is_staff=True.")
        if extra_fields.get("is_superuser") is not True:
            raise ValueError("Superuser must have is_superuser=True.")

        return self.create_user(
            phone_number, display_name=display_name, password=password, **extra_fields
        )


class Account(AbstractBaseUser, PermissionsMixin):
    """
    Custom user model for pisowifi operators.
    Phone number + OTP is the only auth path for regular accounts; superusers created via
    createsuperuser get a real password so they can log into /admin/.
    """

    phone_number = models.CharField(max_length=20, unique=True)
    display_name = models.CharField(max_length=100, blank=True)
    is_verified = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)

    # STEP 2.4: account-level wallet balance, in points (1 point = ₱1, flat 1:1 at funding time).
    # Machine top-ups now spend from this instead of each machine paying PayMongo individually.
    balance_points = models.IntegerField(default=0)

    # Required by Django's admin/auth machinery.
    is_active = models.BooleanField(default=True)
    is_staff = models.BooleanField(default=False)

    objects = AccountManager()

    USERNAME_FIELD = "phone_number"
    REQUIRED_FIELDS = []  # display_name intentionally not required at the createsuperuser prompt

    def __str__(self):
        return f"{self.display_name or 'Unnamed'} ({self.phone_number})"


def default_otp_expiry():
    return timezone.now() + timedelta(minutes=5)


class OTPCode(models.Model):
    """
    One-time password issued for signup or login. Not tied to an Account FK, since signup needs
    to issue an OTP before an Account exists yet.
    """

    phone_number = models.CharField(max_length=20)
    code = models.CharField(max_length=6)
    created_at = models.DateTimeField(auto_now_add=True)
    expires_at = models.DateTimeField(default=default_otp_expiry)
    used = models.BooleanField(default=False)
    # STEP 2.3 security fix: counts failed verification attempts against THIS OTP specifically —
    # not a per-account or per-phone-number lockout. A fresh OTPCode (e.g. a new login/signup
    # attempt) starts its own counter at 0, so this only throttles brute-forcing one still-valid
    # code, never locks the account/phone number itself out of requesting a new one.
    failed_attempts = models.PositiveSmallIntegerField(default=0)

    MAX_FAILED_ATTEMPTS = 5

    # STEP 2.1a: minimum gap between two OTP ISSUANCES (not verifications) for the same phone
    # number, regardless of signup vs. login path. Folds in Security Reviewer finding #5
    # (Session 38) -- neither signup_view nor login_view previously throttled how often a fresh
    # OTP (and therefore a fresh Semaphore SMS) could be requested.
    # Raised 60s -> 180s (post-launch session): each issuance is a paid Semaphore SMS
    # (~PHP 0.56-1.12/send depending on package); a longer cooldown reduces exposure to
    # accidental or abusive rapid-fire "Send code" clicks driving up the bill, at the cost of
    # legitimate users waiting longer for a resend. PM/billing decision, not a security fix.
    OTP_ISSUANCE_COOLDOWN_SECONDS = 180

    class Meta:
        indexes = [models.Index(fields=["phone_number", "code", "used"])]

    def __str__(self):
        return f"OTP for {self.phone_number} (used={self.used})"

    def is_valid(self):
        return (
            not self.used
            and timezone.now() < self.expires_at
            and self.failed_attempts < self.MAX_FAILED_ATTEMPTS
        )

    @staticmethod
    def generate_code():
        # STEP 2.3 security fix: `random` is not cryptographically secure (its output is
        # predictable if an attacker recovers/guesses internal state), which matters here since
        # this code gates account access. Switched to `secrets`, consistent with how
        # Machine.license_key already uses secrets.choice for the same reason.
        return f"{secrets.randbelow(1_000_000):06d}"

    @classmethod
    def seconds_until_next_issue_allowed(cls, phone_number):
        """
        Pure read, no side effects, no writes. Returns 0 if a fresh OTP may be issued for
        phone_number right now; otherwise the whole number of seconds remaining (rounded up to
        the nearest second) before the next issue is allowed.

        Looks at the most recently CREATED OTPCode row for phone_number regardless of `used`
        status -- this cooldown gates ISSUANCE (how often a new SMS goes out), not verification,
        so an already-used-up code from 10 seconds ago still blocks a fresh send just as much as
        a still-pending one would.
        """
        last = cls.objects.filter(phone_number=phone_number).order_by("-created_at").first()
        if last is None:
            return 0
        elapsed_seconds = (timezone.now() - last.created_at).total_seconds()
        remaining = cls.OTP_ISSUANCE_COOLDOWN_SECONDS - elapsed_seconds
        if remaining <= 0:
            return 0
        return math.ceil(remaining)

    @classmethod
    def issue(cls, phone_number):
        """
        Create a fresh OTP for phone_number and send it via Semaphore (STEP 2.1a).

        Fail-closed by construction: the Semaphore send happens BEFORE the OTPCode row is
        persisted. If semaphore_client.send_otp() raises SemaphoreAPIError, that exception
        propagates uncaught out of this method (the calling view is responsible for catching it
        and returning the fail-closed error) and .objects.create(...) is never reached -- so a
        failed send can never leave a "valid" OTPCode sitting in the DB for a code the phone never
        actually received.
        """
        phone_number = normalize_phone_number(phone_number)
        code = cls.generate_code()
        semaphore_client.send_otp(phone_number, code)
        otp = cls.objects.create(phone_number=phone_number, code=code)
        # Supplementary debug log alongside the real Semaphore send, not instead of it.
        print(f"[BARATHRUM OTP] phone_number={phone_number} code={otp.code} expires_at={otp.expires_at.isoformat()}")
        return otp


class PointTransfer(models.Model):
    """
    Peer-to-peer wallet transfer: one operator sending some of their own balance_points to
    another operator, identified by phone number. Distinct from the admin's one-directional
    "Gift Points" action (accounts/admin.py::gift_points_view, superuser-only, no balance check)
    and from a Machine top-up spend (dashboard/views.py, single-account debit only) -- this is
    the first two-account balance movement in the app, so both sides of the transfer are recorded
    on this single row rather than as two separate ledger entries.

    ASSUMPTION FLAGGED: the optional `note` field was not yet confirmed by the PM at build time
    (asked, no answer yet as of this task). Built in since it's low-cost and matches the Gift
    Points precedent (which also has an optional reason/note field) -- trivial to drop via a
    follow-up migration if the PM says no.

    WARNING (End Goal #23, MPD): this model moves Account.balance_points ONLY. Per End Goal #23,
    machines.models.License.license_points must NEVER be read, written, or referenced by this
    model, by dashboard/views.py::send_points_view, or by anything else that transfers funds
    between two operator accounts -- license points are a per-license, per-machine balance with a
    different lifecycle (Top Up credits it, the daily decrement_license_points cron debits it, per
    MPD tracker rows 30/32) and are never eligible for peer-to-peer transfer under any
    circumstance. If a future change ever adds a relationship from this model to License or
    Machine, that is a violation of #23, not an intended extension.
    """

    sender = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="sent_transfers"
    )
    receiver = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="received_transfers"
    )
    amount = models.PositiveIntegerField()
    note = models.CharField(max_length=140, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return f"{self.sender.phone_number} -> {self.receiver.phone_number}: ₱{self.amount}"

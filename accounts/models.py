import re
import secrets
from datetime import timedelta

from django.contrib.auth.base_user import AbstractBaseUser, BaseUserManager
from django.contrib.auth.models import PermissionsMixin
from django.db import models
from django.utils import timezone


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
    def issue(cls, phone_number):
        """Create a fresh OTP for phone_number and 'send' it (console-log only, per STEP 2.1 scope)."""
        phone_number = normalize_phone_number(phone_number)
        otp = cls.objects.create(phone_number=phone_number, code=cls.generate_code())
        # DO NOT wire this to a real SMS gateway here — that's an explicit out-of-scope item for
        # STEP 2.1. This print is the intentional stand-in the task asked for.
        print(f"[BARATHRUM OTP] phone_number={phone_number} code={otp.code} expires_at={otp.expires_at.isoformat()}")
        return otp

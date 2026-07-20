import secrets

from django.conf import settings
from django.db import models

# Character set for license keys: uppercase A-Z and digits 2-9, excluding characters that are
# visually ambiguous when hand-typed: 0/O, 1/I/l. (Digit '1' and lowercase 'l' are excluded by
# construction since we only draw from uppercase letters + digits; 'I' and 'O' are excluded below.)
LICENSE_KEY_ALPHABET = "".join(
    ch for ch in "ABCDEFGHIJKLMNOPQRSTUVWXYZ23456789" if ch not in ("I", "O")
)
LICENSE_KEY_LENGTH = 15

BUNDLE_TYPE_CHOICES = [
    ("custom", "Custom amount"),
    ("30day", "30-day bundle"),
    ("60day", "60-day bundle"),
    ("100day", "100-day bundle"),
    ("300day", "300-day bundle"),
    ("1000day", "1000-day bundle"),
]


def generate_license_key():
    """Generate a 15-char license key from LICENSE_KEY_ALPHABET using a CSPRNG."""
    return "".join(secrets.choice(LICENSE_KEY_ALPHABET) for _ in range(LICENSE_KEY_LENGTH))


def generate_unique_license_key(model_cls, max_attempts=10):
    """
    Generate a license key guaranteed unique against model_cls at call time.
    A DB-level unique constraint on the field is still the real guarantee against races;
    this just avoids an unnecessary IntegrityError round-trip in the common case.
    """
    for _ in range(max_attempts):
        candidate = generate_license_key()
        if not model_cls.objects.filter(license_key=candidate).exists():
            return candidate
    # Astronomically unlikely with a 15-char, 33-symbol alphabet (33^15 possibilities), but don't
    # silently loop forever if it ever happens.
    raise RuntimeError("Could not generate a unique license_key after several attempts.")


class Machine(models.Model):
    """A single pisowifi box registered by an operator (Account)."""

    license_key = models.CharField(max_length=LICENSE_KEY_LENGTH, unique=True, editable=False)
    owner = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="machines"
    )
    nickname = models.CharField(max_length=100, blank=True)
    days_remaining = models.IntegerField(default=0)
    last_topup_bundle_type = models.CharField(
        max_length=10, choices=BUNDLE_TYPE_CHOICES, null=True, blank=True
    )
    created_at = models.DateTimeField(auto_now_add=True)
    last_checkin_at = models.DateTimeField(null=True, blank=True)

    def save(self, *args, **kwargs):
        if not self.license_key:
            self.license_key = generate_unique_license_key(Machine)
        super().save(*args, **kwargs)

    @property
    def is_monitoring_unlocked(self):
        """
        Display-only logic for STEP 2.2's dashboard badge — NOT the real box check-in system
        (that's STEP 2.4/2.5). True only when the machine has time left AND its most recent
        top-up was a 300-day or 1000-day bundle. A later top-up with a smaller bundle re-locks it.
        """
        return self.days_remaining > 0 and self.last_topup_bundle_type in ("300day", "1000day")

    def __str__(self):
        return f"{self.nickname or self.license_key} ({self.owner.phone_number})"


class Transaction(models.Model):
    """A single top-up payment applied to a Machine's days_remaining balance."""

    machine = models.ForeignKey(Machine, on_delete=models.CASCADE, related_name="transactions")
    bundle_type = models.CharField(max_length=10, choices=BUNDLE_TYPE_CHOICES)
    days_added = models.IntegerField()
    amount_paid_pesos = models.DecimalField(max_digits=10, decimal_places=2)
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return f"{self.machine.license_key}: +{self.days_added}d ({self.bundle_type})"


PAYMENT_STATUS_CHOICES = [
    ("pending", "Pending"),
    ("paid", "Paid"),
    ("failed", "Failed"),
    ("expired", "Expired"),
]


class Payment(models.Model):
    """
    A single PayMongo checkout attempt for one Machine's top-up.

    STEP 2.3: this replaces instantly trusting the client's POST data. bundle_type/days/
    amount_pesos are locked in here, server-side, at the moment the operator confirms a top-up
    choice -- BEFORE any redirect to PayMongo. The webhook handler re-reads these locked-in
    values when applying the top-up; it never re-derives amount/days from anything in the
    webhook payload itself; it only trusts this row's own status transition.

    Design choice for Bulk Top-Up (documented per STEP 2.3 task's "your call" note): one Payment
    row PER MACHINE, with all Payments in one bulk batch sharing the same
    paymongo_checkout_session_id (a single PayMongo Checkout Session covers the whole batch as
    one combined charge, using PayMongo's multi-line-item support). This was chosen over a
    separate "batch" table because: (a) it keeps Payment always mapped 1:1 to exactly one
    Machine, so the webhook's "apply this top-up" logic is identical for single and bulk cases;
    (b) the atomic all-or-nothing requirement falls out naturally -- the webhook looks up every
    Payment row sharing a session id and applies them together in one DB transaction, so a
    single PayMongo charge succeeding means the whole batch is confirmed at once, with no
    partial-batch state possible.
    """

    machine = models.ForeignKey(Machine, on_delete=models.CASCADE, related_name="payments")
    bundle_type = models.CharField(max_length=10, choices=BUNDLE_TYPE_CHOICES)
    days = models.IntegerField()
    amount_pesos = models.DecimalField(max_digits=10, decimal_places=2)
    # Not unique: multiple Payment rows (one per machine) share one session id in a bulk top-up.
    paymongo_checkout_session_id = models.CharField(max_length=64, blank=True, db_index=True)
    status = models.CharField(max_length=10, choices=PAYMENT_STATUS_CHOICES, default="pending")
    created_at = models.DateTimeField(auto_now_add=True)
    paid_at = models.DateTimeField(null=True, blank=True)

    def __str__(self):
        return f"Payment({self.machine.license_key}, {self.bundle_type}, {self.status})"

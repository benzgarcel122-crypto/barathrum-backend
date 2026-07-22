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


def generate_unique_license_key(model_classes, max_attempts=10):
    """
    Generate a license key guaranteed unique against every model in model_classes at call time.
    A DB-level unique constraint on the field is still the real guarantee against races;
    this just avoids an unnecessary IntegrityError round-trip in the common case.

    model_classes can be a single model class or a list/tuple of them. STEP 2.5 (Session 31)
    added the License table as a second source of license_key values, so both Machine and
    License now check against each other here -- a freshly generated License key must never
    collide with an existing Machine's key (or vice versa), even though they're separate tables.
    """
    if not isinstance(model_classes, (list, tuple)):
        model_classes = [model_classes]
    for _ in range(max_attempts):
        candidate = generate_license_key()
        if not any(m.objects.filter(license_key=candidate).exists() for m in model_classes):
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
        # STEP 2.5 (Session 31): this auto-generate-on-save path is no longer how normal Add
        # Machine flow creates keys -- that now always passes an existing, claimed License's
        # key in explicitly (see dashboard/views.py::add_machine_view). Left in place only as a
        # safety net so Machine can never end up with a blank license_key by accident; checks
        # against License too so a fallback-generated key can't collide with an unclaimed one.
        if not self.license_key:
            self.license_key = generate_unique_license_key([Machine, License])
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


class License(models.Model):
    """
    STEP 2.5 (Session 31): a license key generated standalone, independent of any Machine.

    Previously (STEP 2.1 through Session 30), Add Machine generated a fresh license_key inline
    at Machine-creation time -- generation and claiming were the same action. This decouples
    them: generate_license_view mints a License row, and add_machine_view now requires pasting
    an existing License's key rather than generating one.

    STEP 2.6 (Session 32) REVERSAL, per explicit Investigator directive: ownership is no longer
    assigned at generation time. A License is created with account=None and stays ownerless
    until some Machine successfully claims it -- at that point add_machine_view sets `account`
    to the claiming Account, repurposing the field from "who generated this" to "who claimed
    this." Any logged-in account can claim any License that exists and isn't already attached to
    a Machine; there is no longer an ownership-match requirement between generator and claimant.

    Machine.license_key remains the field that actually identifies a box (unchanged shape) --
    this table only tracks which keys have been minted and lets add_machine_view look one up
    instead of generating fresh. A License is considered "claimed" once some Machine row exists
    with a matching license_key; there's no FK from Machine back to License, since the two
    tables are linked purely by the license_key string matching (see is_claimed below and
    add_machine_view's claim logic in dashboard/views.py).
    """

    license_key = models.CharField(max_length=LICENSE_KEY_LENGTH, unique=True, editable=False)
    account = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="licenses",
        null=True,
        blank=True,
    )
    created_at = models.DateTimeField(auto_now_add=True)

    def save(self, *args, **kwargs):
        if not self.license_key:
            self.license_key = generate_unique_license_key([License, Machine])
        super().save(*args, **kwargs)

    @property
    def is_claimed(self):
        """True once some Machine has been created using this License's key."""
        return Machine.objects.filter(license_key=self.license_key).exists()

    def __str__(self):
        claimed = " (claimed)" if self.is_claimed else " (unclaimed)"
        owner = f" — claimed by {self.account.phone_number}" if self.account_id else ""
        return f"{self.license_key}{claimed}{owner}"


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
    STEP 2.4: a wallet-funding transaction via PayMongo. Funding the wallet is flat 1:1
    (1 peso paid = 1 balance_points credited) -- this Payment model no longer represents a
    specific machine/bundle purchase at all. Per-machine top-ups are now a fully-internal step
    (deduct Account.balance_points, no external call) handled in dashboard/views.py.

    STEP 2.3's Payment used to FK to Machine and carry bundle_type/days for a specific top-up.
    That's gone: this Payment FKs to Account (the wallet owner) and just carries the peso amount
    being funded. See the STEP 2.4 migration for how existing STEP 2.3 Payment rows were handled
    (backfilled to the owning Account, bundle_type/days dropped since they don't apply to a
    wallet-funding transaction).
    """

    account = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="payments")
    amount_pesos = models.DecimalField(max_digits=10, decimal_places=2)
    paymongo_checkout_session_id = models.CharField(max_length=64, blank=True, db_index=True)
    status = models.CharField(max_length=10, choices=PAYMENT_STATUS_CHOICES, default="pending")
    created_at = models.DateTimeField(auto_now_add=True)
    paid_at = models.DateTimeField(null=True, blank=True)

    def __str__(self):
        return f"Payment({self.account.phone_number}, ₱{self.amount_pesos}, {self.status})"

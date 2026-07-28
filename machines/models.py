import secrets
 
from django.conf import settings
from django.db import models
from django.utils import timezone
 
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

# Shared between machines/management/commands/cleanup_unclaimed_licenses.py (which deletes any
# License unclaimed past this age) and dashboard/views.py's generate_license_view (which shows
# each operator "expires in N days" for their own unclaimed licenses) -- defined once here so
# both stay in sync rather than hardcoding 20 in two separate files.
UNCLAIMED_LICENSE_LIFETIME_DAYS = 20

# STEP 2.7 item 5 (Session 48 corrected design): separate 20-day rule from the one above --
# this one applies to a MACHINE sitting at days_remaining == 0, regardless of claimed/released
# status (claim status is explicitly NOT a factor, per the Session 48 correction). Shared
# between machines/management/commands/cleanup_zero_balance_machines.py (the actual deletion
# job) and Machine.zero_balance_since below. Deliberately a separate constant from
# UNCLAIMED_LICENSE_LIFETIME_DAYS even though both happen to be 20 today -- they are two
# unrelated rules about two different object types that coincidentally share a number.
MACHINE_ZERO_BALANCE_CLEANUP_DAYS = 20

# STEP 2.7 follow-up (this task): shared floor for both the wallet PayMongo top-up
# (dashboard/views.py wallet_topup_view) and the per-machine custom/per-day top-up
# (dashboard/views.py topup_view's "custom" branch) -- previously each only rejected amounts
# <= 0, with no minimum above that. One named constant so both validations stay in sync rather
# than hardcoding 10 in two separate views.
MINIMUM_TOPUP_POINTS = 10


def calendar_days_since(created_at):
    """
    Calendar-day age of a datetime, measured in PH (Asia/Manila) calendar DATES rather than
    exact elapsed hours -- ticks over at PH midnight, not 24 hours after the exact minute the
    row was created. E.g. a License created at 11:59 PM PH time is already "1 day old" one
    minute later, at 12:00 AM PH time the next calendar date -- it does NOT need a full 24 hours
    to elapse first. This is intentionally simpler to reason about (a single date subtraction,
    no hour/minute precision) and gives operators a countdown that only changes once per day, at
    a predictable moment, rather than ticking continuously.

    Used by BOTH the unclaimed-license expiry countdown (dashboard) and the
    cleanup_unclaimed_licenses command's actual deletion decision, so the displayed countdown
    and the real deletion trigger a license eventually hits always agree with each other.

    settings.TIME_ZONE is already 'Asia/Manila' (with USE_TZ=True), so timezone.localtime()
    below correctly converts the UTC-stored created_at into PH local time before taking .date().
    """
    today_ph_date = timezone.localtime(timezone.now()).date()
    created_ph_date = timezone.localtime(created_at).date()
    return (today_ph_date - created_ph_date).days
 
 
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
    # STEP 2.7 items 2-4 (Session 48 design): NULL means active/claimed (today's behavior,
    # unchanged). Non-NULL means this machine was released via the recovery-password-gated
    # Release License flow (dashboard/views.py::release_license_view) -- the row is kept, not
    # deleted, so days_remaining and Transaction history survive until either a re-claim
    # (add_machine_view reactivates this exact row) or the 20-day zero-balance cleanup job
    # eventually sweeps it up.
    removed_at = models.DateTimeField(null=True, blank=True)
    # STEP 2.7 item 5 (Session 48 corrected design): set the moment days_remaining reaches
    # exactly 0 (by either the daily decrement or a top-up leaving it at 0); NULL means the
    # machine currently has a positive balance / has never hit zero. This is the anchor the
    # cleanup_zero_balance_machines cron job measures its 20-day countdown from, via
    # calendar_days_since() below -- claim/release status (Machine.removed_at) is explicitly NOT
    # a factor in this countdown, only this field. Cleared back to NULL the moment ANY top-up is
    # applied (topup_view / bulk_topup_view), no minimum amount threshold, cancelling the
    # countdown immediately -- if the balance later returns to 0, a fresh countdown starts from
    # a fresh timestamp.
    zero_balance_since = models.DateTimeField(null=True, blank=True)
 
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
 
    Session 36: added `generated_by`, a SEPARATE field from `account`. Before this, there was no
    record anywhere of which account actually clicked "Generate License Key" -- `account` starts
    as None at creation and only ever gets filled in later by whoever claims the key, so the
    identity of the generator was never captured at all, not even transiently. `generated_by` is
    set once, at creation, and is never touched again by the claim flow in add_machine_view --
    it answers "who minted this key" as a permanent fact, while `account` continues to answer
    "who has claimed it," which may be a different person entirely (pre-generated keys handed to
    someone else, bulk-generated keys, etc. -- see STEP 2.5's original real-world-workflow
    rationale). No retroactive backfill for existing rows created before this field existed,
    consistent with how the Session 30 wallet rework and Session 32 ownership reversal both
    handled their own historical rows -- existing License rows will simply show `generated_by`
    as blank/unknown, which is accurate: that information genuinely was never recorded for them.
 
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
    generated_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        related_name="generated_licenses",
        null=True,
        blank=True,
        editable=False,
        help_text="Who actually clicked 'Generate License Key.' Set once at creation, never "
                  "changed afterward -- distinct from `account`, which tracks who later claimed "
                  "the key on a Machine and may be a different person entirely. SET_NULL (not "
                  "CASCADE) so deleting an Account doesn't erase the historical fact that a "
                  "license was generated -- only who generated it becomes unknown.",
    )
    created_at = models.DateTimeField(auto_now_add=True)
    # STEP 2.7 items 2-4 (Session 48 design): a recovery password, set once at generation time
    # and never displayed again, that gates the new Release License action (see
    # dashboard/views.py::release_license_view). Stored as a django.contrib.auth.hashers
    # make_password() hash, never plaintext. blank=True/default="" is a migration-safety choice
    # only, for License rows that existed before this field did -- an empty hash can never match
    # any real password via check_password(), so those old rows simply can never be released
    # through this feature (no backfill, no special-case code needed elsewhere).
    recovery_password_hash = models.CharField(max_length=128, blank=True, default="")
    # Wrong-guess counter for the Release License password check, same pattern as
    # accounts.models.OTPCode.failed_attempts / MAX_FAILED_ATTEMPTS.
    release_failed_attempts = models.PositiveSmallIntegerField(default=0)
    # Set to now() + 15 minutes once release_failed_attempts hits RELEASE_MAX_FAILED_ATTEMPTS;
    # NULL means no active lockout. Never a permanent lockout -- there is no way to reset a
    # forgotten recovery password, so a permanent lock would strand the license forever instead
    # of letting it eventually exit via the 20-day zero-balance cleanup job.
    release_locked_until = models.DateTimeField(null=True, blank=True)

    RELEASE_MAX_FAILED_ATTEMPTS = 5

    def save(self, *args, **kwargs):
        if not self.license_key:
            self.license_key = generate_unique_license_key([License, Machine])
        super().save(*args, **kwargs)
 
    @property
    def is_claimed(self):
        """
        True only while an ACTIVE Machine (not released) exists using this License's key.

        STEP 2.7 items 2-4 (Session 48 design): previously this checked for any Machine row's
        existence at all. Now that a released Machine row is kept (not deleted) so its
        days_remaining/history survive, "claimed" must specifically mean "an active Machine
        exists" -- otherwise a released-but-not-yet-reclaimed license would incorrectly still
        show as claimed/unavailable.
        """
        return Machine.objects.filter(license_key=self.license_key, removed_at__isnull=True).exists()
 
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
 

class CronJobRun(models.Model):
    """
    Minimal idempotency tracker for daily cron jobs. STEP 2.7's decrement_machine_days command
    is the first (and, as of this task, only) user of this model -- one row per named job,
    recording the last PH-calendar-date (Asia/Manila, via timezone.localtime().date() -- same
    convention as calendar_days_since() above, deliberately not a second date convention) that
    job actually completed a run.

    Why a dedicated model rather than something more elaborate: this only needs to answer one
    question per run -- "has today's decrement already happened?" -- and a single unique-keyed
    row per job_name answers that with a plain get_or_create, no separate migration data fixture,
    no cross-job coupling. If more cron jobs need the same guard later, they reuse this same
    model with their own job_name; nothing about this model is specific to machine-day decrementing.
    """

    job_name = models.CharField(max_length=100, unique=True)
    last_run_date = models.DateField()

    def __str__(self):
        return f"{self.job_name}: last ran {self.last_run_date}"

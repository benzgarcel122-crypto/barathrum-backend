from django.core.management.base import BaseCommand
from django.db import transaction
from django.utils import timezone

from machines.models import (
    MACHINE_ZERO_BALANCE_CLEANUP_DAYS,
    CronJobRun,
    License,
    Machine,
    calendar_days_since,
)

JOB_NAME = "cleanup_zero_balance_machines"


class Command(BaseCommand):
    """
    STEP 2.7 item 5 (Session 48 corrected design): the 20-day zero-balance auto-cleanup.

    Claim/release status (Machine.removed_at) is explicitly NOT a factor in this rule -- the
    only trigger is Machine.days_remaining sitting at exactly 0 for MACHINE_ZERO_BALANCE_CLEANUP_DAYS
    consecutive PH-calendar-days, regardless of whether the machine is currently claimed
    (active) or released at that moment. This is a SEPARATE rule/object-type from
    cleanup_unclaimed_licenses.py (which deletes never-claimed License rows based on License
    existence, not balance) -- deliberately its own command/cron entry, never merged.

    Anchor field: Machine.zero_balance_since (machines/models.py). ANY top-up at all (even a
    single day, no minimum threshold) clears this field immediately in dashboard/views.py's
    topup_view/bulk_topup_view -- not here, and not on a delay -- so this command never needs to
    "undo" a countdown; it only ever needs to (a) stamp newly-zero machines that don't have a
    timestamp yet, and (b) delete machines whose stamp is old enough.

    Three passes, in order, each a separate bulk step:

    1. STAMP: any Machine at days_remaining <= 0 with zero_balance_since still NULL gets stamped
       to now(). This covers both a machine that hit zero today (via the daily decrement job)
       and, on this command's very first-ever run, any machine that was ALREADY sitting at zero
       balance before this cleanup job existed -- those get stamped as of today, not backdated,
       since their real zero-since date was never recorded. Same "first-run risk" acknowledged
       explicitly in cleanup_unclaimed_licenses.py's own docstring: whether to audit already-zero
       machines before enabling this cron is a PM decision, not something this command decides.

    2. SELF-HEAL: any Machine at days_remaining > 0 that still somehow has zero_balance_since set
       gets it cleared. This should be a no-op in normal operation, since topup_view/
       bulk_topup_view already clear it immediately at top-up time -- this pass exists purely as
       a defense-in-depth safety net (e.g. a days_remaining edit made some other way that this
       command doesn't know about), not as the primary mechanism for cancelling the countdown.

    3. DELETE: any Machine whose zero_balance_since is set and calendar_days_since(...) >=
       MACHINE_ZERO_BALANCE_CLEANUP_DAYS gets fully deleted -- the Machine row itself, its
       matching License row (looked up by the license_key string, same as everywhere else in
       this codebase -- there is no FK between the two tables), and (via Django's CASCADE on
       Transaction.machine, unchanged/untouched by this task) every Transaction row tied to that
       Machine. Payment rows are deliberately NEVER touched here -- they belong to the Account's
       wallet, not to any specific machine, per the Session 48 correction note.

    IDEMPOTENCY: same CronJobRun + PH-calendar-date guard pattern as decrement_machine_days.py
    (see that file's own docstring for the full rationale) -- this command is safe to be
    accidentally triggered more than once on the same PH calendar date.
    """

    help = (
        "Delete Machine/License/Transaction rows that have sat at zero balance for "
        f"{MACHINE_ZERO_BALANCE_CLEANUP_DAYS}+ PH-calendar-days with no top-up, regardless of "
        "claimed/released status. Run once daily via Railway Cron."
    )

    def handle(self, *args, **options):
        today_ph = timezone.localtime(timezone.now()).date()

        with transaction.atomic():
            run_record, created = CronJobRun.objects.select_for_update().get_or_create(
                job_name=JOB_NAME, defaults={"last_run_date": today_ph}
            )

            if not created and run_record.last_run_date == today_ph:
                self.stdout.write(
                    self.style.WARNING(
                        f"[{JOB_NAME}] Already ran today ({today_ph}) -- skipping."
                    )
                )
                return

            # Pass 1: stamp newly (or historically) zero-balance machines.
            stamped_count = Machine.objects.filter(
                days_remaining__lte=0, zero_balance_since__isnull=True
            ).update(zero_balance_since=timezone.now())

            # Pass 2: self-healing reset for any machine that has a positive balance but still
            # somehow carries a stamp (should be a no-op in normal operation).
            healed_count = Machine.objects.filter(
                days_remaining__gt=0, zero_balance_since__isnull=False
            ).update(zero_balance_since=None)

            # Pass 3: delete anything that's been stamped for 20+ PH-calendar-days.
            candidates = list(Machine.objects.filter(zero_balance_since__isnull=False))
            deleted_count = 0
            for machine in candidates:
                if calendar_days_since(machine.zero_balance_since) >= MACHINE_ZERO_BALANCE_CLEANUP_DAYS:
                    License.objects.filter(license_key=machine.license_key).delete()
                    machine.delete()  # CASCADEs to Transaction rows for this machine
                    deleted_count += 1

            if not created:
                run_record.last_run_date = today_ph
                run_record.save(update_fields=["last_run_date"])

        self.stdout.write(
            self.style.SUCCESS(
                f"[{JOB_NAME}] Stamped {stamped_count} newly-zero machine(s), healed "
                f"{healed_count} stale stamp(s), deleted {deleted_count} machine(s) "
                f"{MACHINE_ZERO_BALANCE_CLEANUP_DAYS}+ PH-calendar-days at zero balance "
                f"(checked {len(candidates)} stamped candidate(s) total)."
            )
        )

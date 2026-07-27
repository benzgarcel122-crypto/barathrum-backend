from django.core.management.base import BaseCommand
from django.db import transaction
from django.db.models import F
from django.utils import timezone

from machines.models import CronJobRun, Machine

JOB_NAME = "decrement_machine_days"


class Command(BaseCommand):
    """
    STEP 2.7: the global daily decrement for Machine.days_remaining. Once per day, every machine
    with days_remaining > 0 loses exactly one day -- uniformly, regardless of check-in status or
    when it was last topped up. Intentionally simple: no per-machine elapsed-time math, no
    partial-day proration, a single bulk UPDATE.

    SEPARATE from cleanup_unclaimed_licenses.py (which already lives in this same
    machines/management/commands/ directory) -- that command deletes never-claimed License rows
    based on License existence; this one only ever decrements Machine.days_remaining. Different
    field, different object type, different trigger condition. Not merged, per that command's own
    docstring and this task's explicit instruction -- these two files must never be combined.

    CRITICAL SCOPE BOUNDARY: this command touches Machine.days_remaining ONLY. It never reads or
    writes Account.balance_points (the wallet) -- the wallet doesn't expire or decay on its own,
    it only ever converts into machine-days when an operator spends it on a top-up (a completely
    separate, explicit action in dashboard/views.py). It also never reads or requires
    last_checkin_at -- that field is a monitoring-only signal (STEP 2.2); this decrement applies
    uniformly whether or not a machine has ever checked in.

    IDEMPOTENCY GUARD -- chosen mechanism and why: a dedicated CronJobRun model (machines/models.py),
    one row per job_name, storing the last PH-calendar-date this job actually completed a run.
    Chosen over alternatives considered:
      - A cache key (e.g. Django's cache framework) was rejected -- Railway's cache backend
        configuration isn't guaranteed to persist reliably across cron container restarts, and a
        guard whose entire purpose is correctness under double-firing shouldn't itself depend on
        an ephemeral store.
      - A flag file on local disk was rejected -- Railway cron runs may not share a persistent
        filesystem across invocations, so this would silently fail to guard anything.
      - A real DB row was chosen because it's the one thing guaranteed to be consistent and
        durable across every environment this app already runs in, and this project already has
        a real Postgres connection available in every context this command could run in.
    The guard uses the SAME PH-calendar-date convention as calendar_days_since() in this same
    app (Asia/Manila, via timezone.localtime().date()) -- deliberately not a second, different
    date convention living alongside the first.

    Locking: the CronJobRun row is selected with select_for_update() inside an atomic block, so
    two genuinely simultaneous invocations (not just back-to-back ones) still can't both pass the
    guard and decrement twice -- the second one blocks until the first commits, then correctly
    sees "already ran today" and skips.
    """

    help = (
        "Decrement Machine.days_remaining by 1 for every machine with days remaining > 0. "
        "Run once daily at 12:00 AM Philippine Time via Railway Cron (schedule: 0 16 * * * UTC)."
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
                        f"[decrement_machine_days] Already ran today ({today_ph}) -- "
                        f"skipping to avoid double-decrementing."
                    )
                )
                return

            updated_count = Machine.objects.filter(days_remaining__gt=0).update(
                days_remaining=F("days_remaining") - 1
            )

            if not created:
                run_record.last_run_date = today_ph
                run_record.save(update_fields=["last_run_date"])

        self.stdout.write(
            self.style.SUCCESS(
                f"[decrement_machine_days] Decremented {updated_count} machine(s) for {today_ph}."
            )
        )

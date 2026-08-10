from django.core.management.base import BaseCommand
from django.db import transaction
from django.db.models import F
from django.utils import timezone

from machines.models import CronJobRun, License

JOB_NAME = "decrement_license_points"


class Command(BaseCommand):
    """
    End Goals #24: the parallel daily decrement for License.license_points, copying
    decrement_machine_days.py's exact pattern (see that file's own docstring for the full
    rationale behind each design choice below -- this is deliberately not a novel pattern).
    Once per day, every license with license_points > 0 loses exactly one point -- uniformly,
    regardless of which machine it's bound to or how many concurrent users it's currently
    serving. Intentionally simple: no per-license elapsed-time math, no partial-day proration,
    a single bulk UPDATE.

    SEPARATE from decrement_machine_days.py -- that command touches Machine.days_remaining only
    (day-to-day operating time); this one touches License.license_points only (the completely
    different balance that unlocks the concurrent-user cap, per End Goals #14/#17). Different
    field, different object, different purpose. Not merged, same posture
    decrement_machine_days.py's own docstring already takes toward cleanup_unclaimed_licenses.py.

    CRITICAL SCOPE BOUNDARY: this command touches License.license_points ONLY. It never reads or
    writes Machine.days_remaining or Account.balance_points -- an operator sending points into a
    license (dashboard:send_license_points) is a completely separate, explicit action; this
    command only ever drains what's already there, one point per PH-calendar-day.

    IDEMPOTENCY GUARD: the same CronJobRun + PH-calendar-date guard pattern as
    decrement_machine_days.py, under its own job_name ("decrement_license_points") so the two
    jobs' guard rows never collide or interfere with each other's last-run tracking.

    Locking: same select_for_update()-inside-atomic() pattern as decrement_machine_days.py, so
    two genuinely simultaneous invocations still can't both pass the guard and decrement twice.

    Box-side effect: a license's points hitting 0 here does not immediately re-cap any box --
    the box only learns of the new value on its next periodic sync poll (up to
    LICENSE_POINTS_SYNC_INTERVAL_SECONDS later, box-agent side), and even then only refuses NEW
    concurrent slots going forward; customers already connected are never disconnected by this.
    """

    help = (
        "Decrement License.license_points by 1 for every license with points remaining > 0. "
        "Run once daily via Railway Cron, same shared schedule as decrement_machine_days."
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
                        f"[{JOB_NAME}] Already ran today ({today_ph}) -- "
                        f"skipping to avoid double-decrementing."
                    )
                )
                return

            updated_count = License.objects.filter(license_points__gt=0).update(
                license_points=F("license_points") - 1
            )

            if not created:
                run_record.last_run_date = today_ph
                run_record.save(update_fields=["last_run_date"])

        self.stdout.write(
            self.style.SUCCESS(
                f"[{JOB_NAME}] Decremented {updated_count} license(s) for {today_ph}."
            )
        )

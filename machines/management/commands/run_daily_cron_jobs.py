from django.core.management import call_command
from django.core.management.base import BaseCommand, CommandError


class Command(BaseCommand):
    """
    Thin wrapper so a single Railway Cron service can trigger cleanup_unclaimed_licenses,
    decrement_machine_days, AND cleanup_zero_balance_machines (STEP 2.7 item 5, added this task)
    on the same daily schedule, without merging their actual logic -- each remains its own
    independent, separately-testable Command class in its own file. This wrapper exists purely to
    work around a Railway free-tier service-count limit (one cron service instead of three), NOT
    because the jobs are related -- they touch completely different models/fields for completely
    different reasons. cleanup_zero_balance_machines was deliberately added to this SAME shared
    wrapper (rather than requesting its own separate Railway cron service) for that identical
    free-tier reason, consistent with why the first two jobs were already sharing one here.

    FAILURE ISOLATION: each job runs inside its own try/except, so a failure in one does NOT
    prevent the others from running. This was the main risk of just chaining them with `&&` in a
    Railway start command -- a crash in an earlier job would silently skip the rest entirely.
    Here, if any job raises, this wrapper still attempts all of them, then raises CommandError at
    the very end (after every job has had its chance) so Railway's own deployment/run status
    still correctly shows a failure and can be noticed -- it just won't let one job's crash
    silently hide whether the others even got a chance to run.

    LOG DISTINGUISHABILITY: cleanup_unclaimed_licenses, decrement_machine_days, and
    cleanup_zero_balance_machines (STEP 2.7 item 5, added this task) already prefix their own
    stdout output with their own job name in brackets (e.g. "[decrement_machine_days] Decremented
    4 machine(s)..."), so even sharing one Railway log stream, it's always clear which line came
    from which job.

    NOT SOLVED by this wrapper (a genuine, remaining tradeoff of sharing one Railway service):
    both jobs are still forced onto the exact same cron schedule, and you can't use Railway's
    "Run Now" to fire just one of them in isolation THROUGH THIS SERVICE specifically. That said,
    both underlying commands remain fully runnable independently, any time, from the Console tab
    of any already-running service in this project (e.g. the main dev web service) -- this
    wrapper only affects how the SCHEDULED daily run is triggered, not manual/debugging access.
    """

    help = (
        "Run cleanup_unclaimed_licenses, decrement_machine_days, and "
        "cleanup_zero_balance_machines back-to-back, independently (a failure in one does not "
        "skip the others). Intended for a single shared Railway Cron service; each job remains "
        "fully independent and separately runnable on its own."
    )

    JOB_NAMES = ["cleanup_unclaimed_licenses", "decrement_machine_days", "cleanup_zero_balance_machines"]

    def handle(self, *args, **options):
        failures = []

        for job_name in self.JOB_NAMES:
            self.stdout.write(f"[run_daily_cron_jobs] --- starting {job_name} ---")
            try:
                call_command(job_name)
            except Exception as exc:
                failures.append((job_name, exc))
                self.stderr.write(
                    self.style.ERROR(f"[run_daily_cron_jobs] {job_name} FAILED: {exc}")
                )

        if failures:
            failed_names = ", ".join(name for name, _ in failures)
            raise CommandError(
                f"[run_daily_cron_jobs] One or more daily jobs failed: {failed_names}"
            )

        self.stdout.write(
            self.style.SUCCESS("[run_daily_cron_jobs] All jobs completed successfully.")
        )

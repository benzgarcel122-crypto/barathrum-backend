from django.core.management import call_command
from django.core.management.base import BaseCommand, CommandError


class Command(BaseCommand):
    """
    Thin wrapper so a single Railway Cron service can trigger BOTH cleanup_unclaimed_licenses
    and decrement_machine_days on the same daily schedule, without merging their actual logic --
    each remains its own independent, separately-testable Command class in its own file. This
    wrapper exists purely to work around a Railway free-tier service-count limit (one cron
    service instead of two), NOT because the two jobs are related -- they still touch completely
    different models (License vs. Machine) for completely different reasons.

    FAILURE ISOLATION: each job runs inside its own try/except, so a failure in one does NOT
    prevent the other from running. This was the main risk of just chaining them with `&&` in a
    Railway start command -- a crash in the first job would silently skip the second entirely.
    Here, if either job raises, this wrapper still attempts both, then raises CommandError at the
    very end (after both have had their chance) so Railway's own deployment/run status still
    correctly shows a failure and can be noticed -- it just won't let one job's crash silently
    hide whether the other job even got a chance to run.

    LOG DISTINGUISHABILITY: both cleanup_unclaimed_licenses and decrement_machine_days already
    prefix their own stdout output with their own job name in brackets (e.g.
    "[decrement_machine_days] Decremented 4 machine(s)..."), so even sharing one Railway log
    stream, it's always clear which line came from which job.

    NOT SOLVED by this wrapper (a genuine, remaining tradeoff of sharing one Railway service):
    both jobs are still forced onto the exact same cron schedule, and you can't use Railway's
    "Run Now" to fire just one of them in isolation THROUGH THIS SERVICE specifically. That said,
    both underlying commands remain fully runnable independently, any time, from the Console tab
    of any already-running service in this project (e.g. the main dev web service) -- this
    wrapper only affects how the SCHEDULED daily run is triggered, not manual/debugging access.
    """

    help = (
        "Run cleanup_unclaimed_licenses and decrement_machine_days back-to-back, independently "
        "(a failure in one does not skip the other). Intended for a single shared Railway Cron "
        "service; each job remains fully independent and separately runnable on its own."
    )

    JOB_NAMES = ["cleanup_unclaimed_licenses", "decrement_machine_days"]

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
            self.style.SUCCESS("[run_daily_cron_jobs] Both jobs completed successfully.")
        )

from datetime import timedelta

from django.core.management.base import BaseCommand
from django.utils import timezone

from machines.models import UNCLAIMED_LICENSE_LIFETIME_DAYS, License


class Command(BaseCommand):
    """
    Deletes any License row that has sat unclaimed (no Machine exists with a matching
    license_key) for UNCLAIMED_LICENSE_LIFETIME_DAYS days or more since its created_at.

    This is a SEPARATE rule from STEP 2.7's (designed but not yet built, as of this task)
    unclaimed+zero-balance MACHINE cleanup -- that rule (once built) will apply to previously
    claimed machines that were removed and hit zero balance; this one applies to never-claimed
    LICENSE rows. Different object types, different trigger conditions -- deliberately kept as
    a separate command/cron entry rather than merged into one job or one code path, per the
    task's explicit instruction.

    NOTE: there is no existing "decrement_machine_days" command anywhere in this repo to mirror
    the structure of, despite STEP 2.7 being described as already built in some planning notes --
    it was designed but never actually implemented (no management/ directory existed in any app
    before this command). This command was written fresh, following this project's own general
    conventions (explicit docstrings, print-based logging for Railway's log capture) rather than
    an existing pattern. Flagged to the Investigator separately -- if/when STEP 2.7's machine
    cleanup command is eventually built, it should live alongside this one as its own separate
    Command class, never merged into this file or vice versa.

    The 20-point license generation fee is FORFEITED (not refunded) when a License is deleted
    this way -- confirmed by the PM as the same precedent as STEP 2.7's own forfeiture rule.

    IMPORTANT -- FIRST-RUN RISK: any License rows already sitting unclaimed in production that
    are ALREADY older than UNCLAIMED_LICENSE_LIFETIME_DAYS at the moment this cron is first
    enabled will ALL be deleted the very first time this command runs -- there is no grace
    period or gradual rollout built into this command itself. Whether to run a one-time manual
    audit of existing unclaimed License rows (and decide what, if anything, to do about old ones)
    before turning this cron on is a PM decision -- this command does not make that decision for
    you, and does not distinguish "old because the cron was late to exist" from "old because it
    should really be deleted."
    """

    help = (
        "Delete License rows that have been unclaimed for "
        f"{UNCLAIMED_LICENSE_LIFETIME_DAYS}+ days. Run once daily via Railway Cron."
    )

    def handle(self, *args, **options):
        cutoff = timezone.now() - timedelta(days=UNCLAIMED_LICENSE_LIFETIME_DAYS)
        candidates = list(License.objects.filter(created_at__lte=cutoff))
        total_candidates = len(candidates)

        deleted_count = 0
        for lic in candidates:
            # is_claimed is computed live from the Machine table (license_key string match, no
            # FK) -- re-checked here per-row rather than trusting a stale queryset, since a
            # Machine could claim this exact key between the query above and this loop.
            if not lic.is_claimed:
                lic.delete()
                deleted_count += 1

        self.stdout.write(
            self.style.SUCCESS(
                f"[cleanup_unclaimed_licenses] Deleted {deleted_count} unclaimed license(s) "
                f"older than {UNCLAIMED_LICENSE_LIFETIME_DAYS} days "
                f"(checked {total_candidates} candidate row(s) total)."
            )
        )

from datetime import timedelta

from django.core.management.base import BaseCommand
from django.utils import timezone

from machines.models import UNCLAIMED_LICENSE_LIFETIME_DAYS, License, calendar_days_since


class Command(BaseCommand):
    """
    Deletes any License row that has sat unclaimed (no Machine exists with a matching
    license_key) for UNCLAIMED_LICENSE_LIFETIME_DAYS PH-calendar-days or more, measured from its
    created_at. "PH-calendar-days" (not exact elapsed hours) means age ticks over at PH midnight
    -- see machines.models.calendar_days_since() for the exact logic, shared with the dashboard
    countdown so both always agree on when a given License will actually be deleted.

    This is a SEPARATE rule from STEP 2.7's zero-balance MACHINE cleanup
    (machines/management/commands/cleanup_zero_balance_machines.py, added the same task as
    Release License) -- that rule applies to previously-claimed machines that hit zero balance
    (claim/release status irrelevant to that rule); this one applies to never-claimed LICENSE
    rows. Different object types, different trigger conditions -- deliberately kept as a
    separate command/cron entry rather than merged into one job or one code path.

    NOTE: for a period, this project's own MPD referred to the machine-balance cleanup job as
    "STEP 2.7, designed but not yet built" -- that note is now stale as of the session that added
    cleanup_zero_balance_machines.py; both cleanup jobs exist side by side as of this file's
    current version.

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
        # Loose DB-level pre-filter, one day earlier than the real cutoff -- exact-hours here is
        # only used to shrink the candidate set queried from the DB; the real decision below is
        # calendar_days_since(), which measures PH calendar dates, not elapsed hours. Being one
        # day looser than UNCLAIMED_LICENSE_LIFETIME_DAYS guarantees this pre-filter can never
        # exclude a row that the PH-calendar-date check below would consider eligible.
        loose_cutoff = timezone.now() - timedelta(days=UNCLAIMED_LICENSE_LIFETIME_DAYS - 1)
        candidates = list(License.objects.filter(created_at__lte=loose_cutoff))
        total_candidates = len(candidates)

        deleted_count = 0
        for lic in candidates:
            # is_claimed is computed live from the Machine table (license_key string match, no
            # FK) -- re-checked here per-row rather than trusting a stale queryset, since a
            # Machine could claim this exact key between the query above and this loop.
            if calendar_days_since(lic.created_at) >= UNCLAIMED_LICENSE_LIFETIME_DAYS and not lic.is_claimed:
                lic.delete()
                deleted_count += 1

        self.stdout.write(
            self.style.SUCCESS(
                f"[cleanup_unclaimed_licenses] Deleted {deleted_count} unclaimed license(s) "
                f"{UNCLAIMED_LICENSE_LIFETIME_DAYS}+ PH-calendar-days old "
                f"(checked {total_candidates} candidate row(s) total)."
            )
        )

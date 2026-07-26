from datetime import timedelta
from io import StringIO

from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.test import TestCase
from django.utils import timezone

from machines.models import License, Machine

Account = get_user_model()


class CleanupUnclaimedLicensesTests(TestCase):
    """cleanup_unclaimed_licenses management command, test cases per the task spec. Separate
    command/test file from any future STEP 2.7 Machine cleanup job -- this only ever touches
    License rows, never Machine rows, and the two must stay independent."""

    def setUp(self):
        self.acc1 = Account.objects.create_user(phone_number="09171234567", display_name="Op One")

    def _backdate(self, license_obj, days_ago):
        """auto_now_add ignores created_at passed at .create() time -- backdate via .update()
        after the fact, which bypasses auto_now_add's save()-time override."""
        License.objects.filter(pk=license_obj.pk).update(
            created_at=timezone.now() - timedelta(days=days_ago)
        )
        license_obj.refresh_from_db()

    def test_tc1_unclaimed_license_older_than_20_days_is_deleted(self):
        lic = License.objects.create(account=None, generated_by=self.acc1)
        self._backdate(lic, 21)
        out = StringIO()
        call_command("cleanup_unclaimed_licenses", stdout=out)
        self.assertFalse(License.objects.filter(pk=lic.pk).exists())
        self.assertIn("Deleted 1 unclaimed license", out.getvalue())

    def test_tc2_unclaimed_license_under_20_days_not_deleted(self):
        lic = License.objects.create(account=None, generated_by=self.acc1)
        self._backdate(lic, 19)
        call_command("cleanup_unclaimed_licenses", stdout=StringIO())
        self.assertTrue(License.objects.filter(pk=lic.pk).exists())

    def test_tc3_claimed_license_older_than_20_days_not_deleted(self):
        lic = License.objects.create(account=None, generated_by=self.acc1)
        self._backdate(lic, 25)
        # Claim it via a Machine sharing the same license_key -- is_claimed is computed live
        # from this string match, no FK, per the model's own docstring.
        Machine.objects.create(license_key=lic.license_key, owner=self.acc1)
        call_command("cleanup_unclaimed_licenses", stdout=StringIO())
        self.assertTrue(License.objects.filter(pk=lic.pk).exists())

    def test_cleanup_leaves_recent_unclaimed_licenses_alone(self):
        """Sanity check: a freshly generated license (0 days old) is never touched."""
        lic = License.objects.create(account=None, generated_by=self.acc1)
        call_command("cleanup_unclaimed_licenses", stdout=StringIO())
        self.assertTrue(License.objects.filter(pk=lic.pk).exists())


class CalendarDaysSinceTests(TestCase):
    """calendar_days_since() ticks over at PH midnight, not after a full 24 elapsed hours --
    this is the actual behavior change requested (countdown/deletion based on PH calendar dates,
    not exact elapsed-hours math)."""

    def test_ticks_over_at_midnight_not_after_24_full_hours(self):
        from machines.models import calendar_days_since

        acc = Account.objects.create_user(phone_number="09171234567", display_name="Op One")
        lic = License.objects.create(account=None, generated_by=acc)

        # Simulate: license was created at 11:59 PM PH time yesterday. Less than 24 hours will
        # have elapsed by "now" (a few minutes), but it's already a different PH calendar date --
        # so calendar_days_since should report 1, not 0.
        ph_now = timezone.localtime(timezone.now())
        yesterday_2359_ph = (ph_now - timedelta(days=1)).replace(
            hour=23, minute=59, second=0, microsecond=0
        )
        License.objects.filter(pk=lic.pk).update(created_at=yesterday_2359_ph)
        lic.refresh_from_db()

        self.assertEqual(calendar_days_since(lic.created_at), 1)

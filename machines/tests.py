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


class DecrementMachineDaysTests(TestCase):
    """decrement_machine_days management command, test cases per the STEP 2.7 task spec."""

    def setUp(self):
        self.acc1 = Account.objects.create_user(phone_number="09171234567", display_name="Op One")

    def test_tc1_machine_with_days_remaining_is_decremented_by_one(self):
        m = Machine.objects.create(owner=self.acc1, days_remaining=10)
        call_command("decrement_machine_days", stdout=StringIO())
        m.refresh_from_db()
        self.assertEqual(m.days_remaining, 9)

    def test_tc2_machine_at_zero_stays_at_zero(self):
        m = Machine.objects.create(owner=self.acc1, days_remaining=0)
        call_command("decrement_machine_days", stdout=StringIO())
        m.refresh_from_db()
        self.assertEqual(m.days_remaining, 0)

    def test_tc3_double_fire_same_day_does_not_double_decrement(self):
        m = Machine.objects.create(owner=self.acc1, days_remaining=10)
        call_command("decrement_machine_days", stdout=StringIO())
        m.refresh_from_db()
        self.assertEqual(m.days_remaining, 9)

        # Immediate second run, same PH calendar day -- must NOT decrement again.
        out = StringIO()
        call_command("decrement_machine_days", stdout=out)
        m.refresh_from_db()
        self.assertEqual(m.days_remaining, 9)
        self.assertIn("Already ran today", out.getvalue())

    def test_tc4_new_ph_calendar_day_decrements_again(self):
        from machines.models import CronJobRun

        m = Machine.objects.create(owner=self.acc1, days_remaining=10)
        call_command("decrement_machine_days", stdout=StringIO())
        m.refresh_from_db()
        self.assertEqual(m.days_remaining, 9)

        # Simulate "yesterday" by rolling the tracking row's last_run_date back one day --
        # the guard should then allow today's run to proceed normally.
        run_record = CronJobRun.objects.get(job_name="decrement_machine_days")
        run_record.last_run_date -= timedelta(days=1)
        run_record.save(update_fields=["last_run_date"])

        call_command("decrement_machine_days", stdout=StringIO())
        m.refresh_from_db()
        self.assertEqual(m.days_remaining, 8)

    def test_tc5_never_references_balance_points_in_command_code(self):
        """Direct code check, not just docstring inspection: confirm the actual command file's
        handle() logic never reads/writes Account.balance_points."""
        import inspect

        from machines.management.commands.decrement_machine_days import Command

        source = inspect.getsource(Command.handle)
        self.assertNotIn("balance_points", source)

    def test_multiple_machines_and_mixed_zero_balances_all_handled_correctly(self):
        m1 = Machine.objects.create(owner=self.acc1, days_remaining=5)
        m2 = Machine.objects.create(
            owner=Account.objects.create_user(phone_number="09179876543"), days_remaining=0
        )
        m3 = Machine.objects.create(
            owner=Account.objects.create_user(phone_number="09171112222"), days_remaining=1
        )
        call_command("decrement_machine_days", stdout=StringIO())
        m1.refresh_from_db()
        m2.refresh_from_db()
        m3.refresh_from_db()
        self.assertEqual(m1.days_remaining, 4)
        self.assertEqual(m2.days_remaining, 0)
        self.assertEqual(m3.days_remaining, 0)


class RunDailyCronJobsTests(TestCase):
    """run_daily_cron_jobs wrapper: both jobs run independently, and one failing doesn't skip
    the other -- the actual mitigation for the shared-Railway-service tradeoff, not just claimed
    in the docstring."""

    def setUp(self):
        self.acc1 = Account.objects.create_user(phone_number="09171234567", display_name="Op One")

    def test_both_jobs_run_successfully_in_normal_conditions(self):
        m = Machine.objects.create(owner=self.acc1, days_remaining=5)
        lic = License.objects.create(account=None, generated_by=self.acc1)
        License.objects.filter(pk=lic.pk).update(
            created_at=timezone.now() - timedelta(days=21)
        )

        out = StringIO()
        call_command("run_daily_cron_jobs", stdout=out)

        m.refresh_from_db()
        self.assertEqual(m.days_remaining, 4)
        self.assertFalse(License.objects.filter(pk=lic.pk).exists())
        self.assertIn("Both jobs completed successfully", out.getvalue())

    def test_failure_in_one_job_does_not_skip_the_other(self):
        """If cleanup_unclaimed_licenses raises, decrement_machine_days must still run."""
        from django.core.management.base import CommandError
        from unittest.mock import patch

        m = Machine.objects.create(owner=self.acc1, days_remaining=5)

        def fake_call_command(name, *args, **kwargs):
            if name == "cleanup_unclaimed_licenses":
                raise RuntimeError("simulated crash")
            # Let decrement_machine_days actually run for real.
            from django.core.management import call_command as real_call_command
            return real_call_command(name, *args, **kwargs)

        with patch(
            "machines.management.commands.run_daily_cron_jobs.call_command",
            side_effect=fake_call_command,
        ):
            with self.assertRaises(CommandError):
                call_command("run_daily_cron_jobs")

        # The failing job didn't block the other one from running.
        m.refresh_from_db()
        self.assertEqual(m.days_remaining, 4)

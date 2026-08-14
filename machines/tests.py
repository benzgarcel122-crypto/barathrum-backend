import json
from datetime import timedelta
from io import StringIO
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.contrib.auth.hashers import make_password
from django.core.cache import cache
from django.core.management import call_command
from django.db import IntegrityError
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from machines.models import License, Machine

from dashboard.views import BUNDLE_PRICING

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

    def test_unclaimed_license_with_positive_points_is_not_deleted(self):
        """End Goal #19: the new precondition -- an unclaimed, 20+ day old license that still
        holds a positive license_points balance is spared, since those points represent real
        money already spent that deletion would destroy with no recovery path."""
        lic = License.objects.create(account=None, generated_by=self.acc1, license_points=5)
        self._backdate(lic, 25)
        call_command("cleanup_unclaimed_licenses", stdout=StringIO())
        self.assertTrue(License.objects.filter(pk=lic.pk).exists())

    def test_unclaimed_license_with_zero_points_still_deleted_as_before(self):
        """Confirms the new precondition doesn't accidentally spare the normal zero-points
        case -- unchanged behavior from before this task."""
        lic = License.objects.create(account=None, generated_by=self.acc1, license_points=0)
        self._backdate(lic, 25)
        call_command("cleanup_unclaimed_licenses", stdout=StringIO())
        self.assertFalse(License.objects.filter(pk=lic.pk).exists())


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


class DecrementLicensePointsTests(TestCase):
    """
    decrement_license_points management command -- End Goals #24, mirroring
    DecrementMachineDaysTests' own test cases exactly (same pattern applied to License.license_points
    instead of Machine.days_remaining).
    """

    def setUp(self):
        self.acc1 = Account.objects.create_user(phone_number="09171234567", display_name="Op One")

    def test_license_with_points_is_decremented_by_one(self):
        lic = License.objects.create(account=None, generated_by=self.acc1, license_points=10)
        call_command("decrement_license_points", stdout=StringIO())
        lic.refresh_from_db()
        self.assertEqual(lic.license_points, 9)

    def test_still_drains_an_unactivated_license(self):
        """PM sign-off: points sent before a box is ever bound still lose value on the normal
        daily schedule -- this command has NO activation check of its own, intentionally."""
        lic = License.objects.create(
            account=self.acc1, generated_by=self.acc1, activated_at=None, license_points=10
        )
        call_command("decrement_license_points", stdout=StringIO())
        lic.refresh_from_db()
        self.assertEqual(lic.license_points, 9)

    def test_license_at_zero_stays_at_zero(self):
        lic = License.objects.create(account=None, generated_by=self.acc1, license_points=0)
        call_command("decrement_license_points", stdout=StringIO())
        lic.refresh_from_db()
        self.assertEqual(lic.license_points, 0)

    def test_double_fire_same_day_does_not_double_decrement(self):
        lic = License.objects.create(account=None, generated_by=self.acc1, license_points=10)
        call_command("decrement_license_points", stdout=StringIO())
        lic.refresh_from_db()
        self.assertEqual(lic.license_points, 9)

        out = StringIO()
        call_command("decrement_license_points", stdout=out)
        lic.refresh_from_db()
        self.assertEqual(lic.license_points, 9)
        self.assertIn("Already ran today", out.getvalue())

    def test_new_ph_calendar_day_decrements_again(self):
        from machines.models import CronJobRun

        lic = License.objects.create(account=None, generated_by=self.acc1, license_points=10)
        call_command("decrement_license_points", stdout=StringIO())
        lic.refresh_from_db()
        self.assertEqual(lic.license_points, 9)

        run_record = CronJobRun.objects.get(job_name="decrement_license_points")
        run_record.last_run_date -= timedelta(days=1)
        run_record.save(update_fields=["last_run_date"])

        call_command("decrement_license_points", stdout=StringIO())
        lic.refresh_from_db()
        self.assertEqual(lic.license_points, 8)

    def test_never_references_days_remaining_or_balance_points_in_command_code(self):
        """Direct code check: confirm this command's handle() logic never reads/writes
        Machine.days_remaining or Account.balance_points -- it only ever touches
        License.license_points."""
        import inspect

        from machines.management.commands.decrement_license_points import Command

        source = inspect.getsource(Command.handle)
        self.assertNotIn("days_remaining", source)
        self.assertNotIn("balance_points", source)

    def test_multiple_licenses_and_mixed_zero_balances_all_handled_correctly(self):
        lic1 = License.objects.create(account=None, generated_by=self.acc1, license_points=5)
        lic2 = License.objects.create(
            account=None,
            generated_by=Account.objects.create_user(phone_number="09179876543"),
            license_points=0,
        )
        lic3 = License.objects.create(
            account=None,
            generated_by=Account.objects.create_user(phone_number="09171112222"),
            license_points=1,
        )
        call_command("decrement_license_points", stdout=StringIO())
        lic1.refresh_from_db()
        lic2.refresh_from_db()
        lic3.refresh_from_db()
        self.assertEqual(lic1.license_points, 4)
        self.assertEqual(lic2.license_points, 0)
        self.assertEqual(lic3.license_points, 0)

    def test_decrement_license_points_guard_is_independent_of_decrement_machine_days_guard(self):
        """The two jobs' CronJobRun rows must never collide or share a last-run-date -- confirms
        they're keyed by distinct job_name values."""
        from machines.models import CronJobRun

        m = Machine.objects.create(owner=self.acc1, days_remaining=5)
        lic = License.objects.create(account=None, generated_by=self.acc1, license_points=5)

        call_command("decrement_machine_days", stdout=StringIO())
        # decrement_license_points hasn't run yet -- must still be able to run today, unaffected
        # by decrement_machine_days already having run today under its own job_name.
        call_command("decrement_license_points", stdout=StringIO())

        m.refresh_from_db()
        lic.refresh_from_db()
        self.assertEqual(m.days_remaining, 4)
        self.assertEqual(lic.license_points, 4)
        self.assertEqual(
            set(CronJobRun.objects.values_list("job_name", flat=True)),
            {"decrement_machine_days", "decrement_license_points"},
        )


class RunDailyCronJobsTests(TestCase):
    """run_daily_cron_jobs wrapper: all four jobs run independently, and any one failing doesn't
    skip the others -- the actual mitigation for the shared-Railway-service tradeoff, not just
    claimed in the docstring."""

    def setUp(self):
        self.acc1 = Account.objects.create_user(phone_number="09171234567", display_name="Op One")

    def test_all_four_jobs_run_successfully_in_normal_conditions(self):
        m = Machine.objects.create(owner=self.acc1, days_remaining=5)
        lic = License.objects.create(account=None, generated_by=self.acc1)
        License.objects.filter(pk=lic.pk).update(
            created_at=timezone.now() - timedelta(days=21)
        )
        lic2 = License.objects.create(account=None, generated_by=self.acc1, license_points=5)

        out = StringIO()
        call_command("run_daily_cron_jobs", stdout=out)

        m.refresh_from_db()
        lic2.refresh_from_db()
        self.assertEqual(m.days_remaining, 4)
        self.assertFalse(License.objects.filter(pk=lic.pk).exists())
        self.assertEqual(lic2.license_points, 4)
        self.assertIn("All jobs completed successfully", out.getvalue())

    def test_failure_in_decrement_license_points_does_not_skip_the_other_three(self):
        from django.core.management.base import CommandError
        from unittest.mock import patch

        m = Machine.objects.create(owner=self.acc1, days_remaining=5)
        lic = License.objects.create(account=None, generated_by=self.acc1)
        License.objects.filter(pk=lic.pk).update(
            created_at=timezone.now() - timedelta(days=21)
        )

        def fake_call_command(name, *args, **kwargs):
            if name == "decrement_license_points":
                raise RuntimeError("simulated crash")
            from django.core.management import call_command as real_call_command
            return real_call_command(name, *args, **kwargs)

        with patch(
            "machines.management.commands.run_daily_cron_jobs.call_command",
            side_effect=fake_call_command,
        ):
            with self.assertRaises(CommandError):
                call_command("run_daily_cron_jobs")

        # The other three jobs still completed despite decrement_license_points crashing.
        m.refresh_from_db()
        self.assertEqual(m.days_remaining, 4)
        self.assertFalse(License.objects.filter(pk=lic.pk).exists())

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

    def test_failure_in_third_job_does_not_skip_the_other_two(self):
        """Prompt's TC7: simulate a failure specifically in cleanup_zero_balance_machines (the
        newly-added third job) and confirm the other two still run to completion."""
        from django.core.management.base import CommandError
        from unittest.mock import patch

        m = Machine.objects.create(owner=self.acc1, days_remaining=5)
        lic = License.objects.create(account=None, generated_by=self.acc1)
        License.objects.filter(pk=lic.pk).update(
            created_at=timezone.now() - timedelta(days=21)
        )

        def fake_call_command(name, *args, **kwargs):
            if name == "cleanup_zero_balance_machines":
                raise RuntimeError("simulated crash")
            from django.core.management import call_command as real_call_command
            return real_call_command(name, *args, **kwargs)

        with patch(
            "machines.management.commands.run_daily_cron_jobs.call_command",
            side_effect=fake_call_command,
        ):
            with self.assertRaises(CommandError):
                call_command("run_daily_cron_jobs")

        # Both of the other two jobs still completed despite the third one crashing.
        m.refresh_from_db()
        self.assertEqual(m.days_remaining, 4)  # decrement_machine_days still ran
        self.assertFalse(License.objects.filter(pk=lic.pk).exists())  # cleanup_unclaimed_licenses still ran


class CleanupZeroBalanceMachinesTests(TestCase):
    """
    STEP 2.7 item 5 (Session 48 corrected design): cleanup_zero_balance_machines management
    command. Separate command/test class from CleanupUnclaimedLicensesTests -- this only ever
    touches Machine rows at days_remaining == 0, regardless of License/claim status.
    """

    def setUp(self):
        self.acc1 = Account.objects.create_user(phone_number="09171234567", display_name="Op One")

    def _backdate_zero_balance(self, machine, days_ago):
        Machine.objects.filter(pk=machine.pk).update(
            zero_balance_since=timezone.now() - timedelta(days=days_ago)
        )
        machine.refresh_from_db()

    def test_stamps_newly_zero_machine(self):
        m = Machine.objects.create(owner=self.acc1, days_remaining=0)
        self.assertIsNone(m.zero_balance_since)

        call_command("cleanup_zero_balance_machines")

        m.refresh_from_db()
        self.assertIsNotNone(m.zero_balance_since)

    def test_positive_balance_machine_not_stamped(self):
        m = Machine.objects.create(owner=self.acc1, days_remaining=5)

        call_command("cleanup_zero_balance_machines")

        m.refresh_from_db()
        self.assertIsNone(m.zero_balance_since)

    def test_self_heal_clears_stale_stamp_on_positive_balance(self):
        m = Machine.objects.create(owner=self.acc1, days_remaining=3)
        Machine.objects.filter(pk=m.pk).update(zero_balance_since=timezone.now())
        m.refresh_from_db()
        self.assertIsNotNone(m.zero_balance_since)

        call_command("cleanup_zero_balance_machines")

        m.refresh_from_db()
        self.assertIsNone(m.zero_balance_since)

    def test_deletes_machine_license_and_transactions_after_20_days(self):
        from machines.models import Transaction

        lic = License.objects.create(account=self.acc1, generated_by=self.acc1)
        m = Machine.objects.create(owner=self.acc1, license_key=lic.license_key, days_remaining=0)
        Transaction.objects.create(machine=m, bundle_type="30day", days_added=30, amount_paid_pesos=27)
        self._backdate_zero_balance(m, 20)

        call_command("cleanup_zero_balance_machines")

        self.assertFalse(Machine.objects.filter(pk=m.pk).exists())
        self.assertFalse(License.objects.filter(pk=lic.pk).exists())
        self.assertFalse(Transaction.objects.filter(machine_id=m.pk).exists())

    def test_does_not_delete_before_20_days(self):
        m = Machine.objects.create(owner=self.acc1, days_remaining=0)
        self._backdate_zero_balance(m, 19)

        call_command("cleanup_zero_balance_machines")

        self.assertTrue(Machine.objects.filter(pk=m.pk).exists())

    def test_claim_status_irrelevant_to_deletion_trigger(self):
        """A RELEASED machine at zero balance for 20+ days is deleted exactly the same as a
        still-claimed one -- claim status is explicitly not a factor (Session 48 correction)."""
        m = Machine.objects.create(
            owner=self.acc1, days_remaining=0, removed_at=timezone.now() - timedelta(days=25)
        )
        self._backdate_zero_balance(m, 20)

        call_command("cleanup_zero_balance_machines")

        self.assertFalse(Machine.objects.filter(pk=m.pk).exists())

    def test_idempotency_guard_skips_second_run_same_day(self):
        m = Machine.objects.create(owner=self.acc1, days_remaining=0)
        self._backdate_zero_balance(m, 20)

        call_command("cleanup_zero_balance_machines")
        self.assertFalse(Machine.objects.filter(pk=m.pk).exists())

        # A second machine, also 20+ days at zero, created AFTER the first run -- since the
        # guard blocks the whole command from running again today, this one should survive
        # until tomorrow's run, proving the guard actually blocks re-execution.
        m2 = Machine.objects.create(owner=self.acc1, days_remaining=0)
        self._backdate_zero_balance(m2, 20)

        out = StringIO()
        call_command("cleanup_zero_balance_machines", stdout=out)
        self.assertIn("Already ran today", out.getvalue())
        self.assertTrue(Machine.objects.filter(pk=m2.pk).exists())

    def test_minimum_allowed_topup_cancels_countdown(self):
        """Originally proved a 1-day top-up cancels the countdown with no separate minimum of
        its own; a later task added a MINIMUM_TOPUP_POINTS=10 floor on what can be submitted at
        all via this view, so 10 is now the smallest top-up actually reachable here -- updated
        accordingly. The zero-balance-cancels-on-any-successful-top-up rule itself is unchanged;
        only the smallest amount that can reach it changed."""
        self.acc1.balance_points = 10
        self.acc1.save(update_fields=["balance_points"])
        m = Machine.objects.create(owner=self.acc1, days_remaining=0)
        self._backdate_zero_balance(m, 5)

        self.client.force_login(self.acc1)
        self.client.post(
            f"/machines/{m.id}/topup/",
            {"mode": "custom", "custom_days": "10"},
        )

        m.refresh_from_db()
        self.assertEqual(m.days_remaining, 10)
        self.assertIsNone(m.zero_balance_since)

        # Confirmed on the next cron run too, not just immediately after the top-up.
        call_command("cleanup_zero_balance_machines")
        self.assertTrue(Machine.objects.filter(pk=m.pk).exists())

    def test_both_claimed_and_released_machines_deleted_in_same_run(self):
        """Prompt's TC5: claim status must be irrelevant -- a still-claimed machine and a
        released machine, both 20+ days at zero balance, must BOTH be deleted by the same run,
        not just the released one."""
        claimed_lic = License.objects.create(account=self.acc1, generated_by=self.acc1)
        claimed_machine = Machine.objects.create(
            owner=self.acc1, license_key=claimed_lic.license_key, days_remaining=0
        )  # removed_at defaults to NULL -- still actively claimed
        self._backdate_zero_balance(claimed_machine, 20)

        released_lic = License.objects.create(account=self.acc1, generated_by=self.acc1)
        released_machine = Machine.objects.create(
            owner=self.acc1,
            license_key=released_lic.license_key,
            days_remaining=0,
            removed_at=timezone.now() - timedelta(days=25),
        )
        self._backdate_zero_balance(released_machine, 20)

        call_command("cleanup_zero_balance_machines")

        self.assertFalse(Machine.objects.filter(pk=claimed_machine.pk).exists())
        self.assertFalse(License.objects.filter(pk=claimed_lic.pk).exists())
        self.assertFalse(Machine.objects.filter(pk=released_machine.pk).exists())
        self.assertFalse(License.objects.filter(pk=released_lic.pk).exists())

    def test_payment_rows_completely_untouched(self):
        """Prompt's TC6: Payment has no relationship to Machine at all (Account-only FK) and
        must never be read or written by this command."""
        from machines.models import Payment

        payment = Payment.objects.create(account=self.acc1, amount_pesos=100, status="paid")

        m = Machine.objects.create(owner=self.acc1, days_remaining=0)
        self._backdate_zero_balance(m, 20)

        call_command("cleanup_zero_balance_machines")

        self.assertFalse(Machine.objects.filter(pk=m.pk).exists())
        payment.refresh_from_db()  # would raise DoesNotExist if it had somehow been deleted
        self.assertEqual(payment.status, "paid")
        self.assertEqual(payment.amount_pesos, 100)


class TopupClearsZeroBalanceCountdownTests(TestCase):
    """Any top-up (dashboard/views.py topup_view / bulk_topup_view) must immediately clear
    Machine.zero_balance_since, per STEP 2.7 item 5's 'no minimum threshold' rule."""

    def setUp(self):
        self.acc1 = Account.objects.create_user(
            phone_number="09171234567", display_name="Op One", balance_points=1000
        )

    def test_single_topup_clears_zero_balance_since(self):
        m = Machine.objects.create(owner=self.acc1, days_remaining=0)
        Machine.objects.filter(pk=m.pk).update(zero_balance_since=timezone.now())
        m.refresh_from_db()
        self.assertIsNotNone(m.zero_balance_since)

        self.client.force_login(self.acc1)
        self.client.post(f"/machines/{m.id}/topup/", {"mode": "bundle", "bundle_type": "30day"})

        m.refresh_from_db()
        self.assertIsNone(m.zero_balance_since)
        self.assertEqual(m.days_remaining, 30)

    def test_bulk_topup_clears_zero_balance_since(self):
        m = Machine.objects.create(owner=self.acc1, days_remaining=0)
        Machine.objects.filter(pk=m.pk).update(zero_balance_since=timezone.now())
        m.refresh_from_db()

        self.client.force_login(self.acc1)
        self.client.post(
            "/machines/bulk-topup/",
            {"machine_id": [m.id], f"bundle_{m.id}": "30day"},
        )

        m.refresh_from_db()
        self.assertIsNone(m.zero_balance_since)


class ValidateLicenseViewTests(TestCase):
    """
    POST /api/box/validate-license/ -- box-side license ACTIVATION endpoint.

    Rewritten Session 86 (MPD): this endpoint used to be read-only and required is_claimed=True
    (409 otherwise). It now WRITES License.activated_at on first successful validation, and no
    longer requires the license to be claimed first -- activation and claiming are independent
    events (End Goals, MPD Session 82 closing note). The five original required cases (claimed /
    unclaimed / nonexistent / empty / rate-limited) are kept, but "unclaimed" now expects 200 +
    activation, not 409 -- that's the actual behavior change this rewrite makes.

    IMPORTANT: this endpoint's rate limiter is keyed by client IP and stored in Django's default
    cache, which persists across tests within the same test-process run (it's not reset by
    TestCase's DB-transaction rollback, since it isn't the DB). Every test explicitly clears the
    relevant cache key in setUp so tests don't leak rate-limit state into each other and produce
    order-dependent failures.
    """

    url = "/api/box/validate-license/"

    def setUp(self):
        cache.clear()
        self.acc1 = Account.objects.create_user(phone_number="09171234567", display_name="Op One")

    def _post(self, license_key):
        return self.client.post(
            self.url, data=json.dumps({"license_key": license_key}), content_type="application/json"
        )

    def test_reverse_resolves_to_the_documented_path(self):
        self.assertEqual(reverse("machines:validate_license"), self.url)

    def test_tc1_valid_claimed_license_key_returns_200_and_activates(self):
        lic = License.objects.create(account=self.acc1, generated_by=self.acc1)
        Machine.objects.create(owner=self.acc1, license_key=lic.license_key)
        self.assertIsNone(lic.activated_at)

        response = self._post(lic.license_key)

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["valid"], True)
        lic.refresh_from_db()
        self.assertIsNotNone(lic.activated_at)

    def test_tc1b_lowercase_and_whitespace_input_normalized_same_as_claim_view(self):
        """Same .strip().upper() normalization as add_machine_view's license_key_input --
        confirms this endpoint doesn't silently diverge from that normalization rule."""
        lic = License.objects.create(account=self.acc1, generated_by=self.acc1)
        Machine.objects.create(owner=self.acc1, license_key=lic.license_key)

        response = self._post(f"  {lic.license_key.lower()}  ")

        self.assertEqual(response.status_code, 200)

    def test_tc2_valid_unclaimed_license_key_now_returns_200_and_activates(self):
        """
        BEHAVIOR CHANGE, Session 86: previously this was a 409 ("hasn't been claimed yet").
        Per the End Goals (MPD, Session 82), activation and claiming are independent -- a license
        can be activated at a box before anyone ever clicks Add Machine on the dashboard. This is
        exactly the scenario a Developer session (Session 85) correctly flagged as a real tension
        between the old behavior and the End Goals -- this rewrite is the actual resolution.
        """
        lic = License.objects.create(account=None, generated_by=self.acc1)
        self.assertIsNone(lic.activated_at)

        response = self._post(lic.license_key)

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["valid"], True)
        lic.refresh_from_db()
        self.assertIsNotNone(lic.activated_at)
        # Still genuinely unclaimed -- activation must not have touched `account`/is_claimed.
        self.assertIsNone(lic.account)
        self.assertFalse(lic.is_claimed)

    def test_tc2b_released_machine_license_also_activates_successfully(self):
        """A released (removed_at set) Machine means is_claimed is False -- and per the same
        behavior change as tc2 above, that no longer blocks activation."""
        lic = License.objects.create(account=self.acc1, generated_by=self.acc1)
        Machine.objects.create(
            owner=self.acc1, license_key=lic.license_key, removed_at=timezone.now()
        )

        response = self._post(lic.license_key)

        self.assertEqual(response.status_code, 200)
        lic.refresh_from_db()
        self.assertIsNotNone(lic.activated_at)

    def test_second_call_to_already_active_license_returns_409_and_does_not_change_timestamp(self):
        """
        BEHAVIOR CHANGE, End Goal #21: previously a second call for an already-activated key
        silently returned 200 (the old accepted gap -- Session 65 open question #2). Now it
        returns 409 with already_active: true, forcing the caller through the password-gated
        Unbind -> Bind recovery flow instead of silently re-succeeding with zero proof of
        ownership.
        """
        lic = License.objects.create(account=None, generated_by=self.acc1)

        first_response = self._post(lic.license_key)
        self.assertEqual(first_response.status_code, 200)
        lic.refresh_from_db()
        first_activated_at = lic.activated_at
        self.assertIsNotNone(first_activated_at)

        second_response = self._post(lic.license_key)
        self.assertEqual(second_response.status_code, 409)
        self.assertEqual(second_response.json()["valid"], False)
        self.assertEqual(second_response.json()["already_active"], True)
        lic.refresh_from_db()
        self.assertEqual(lic.activated_at, first_activated_at)

    def test_tc3_nonexistent_license_key_returns_404(self):
        response = self._post("ZZZZZZZZZZZZZZZ")

        self.assertEqual(response.status_code, 404)
        self.assertEqual(response.json()["valid"], False)

    def test_tc4_empty_license_key_returns_400(self):
        response = self._post("")

        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()["valid"], False)

    def test_tc4b_missing_license_key_field_returns_400(self):
        response = self.client.post(self.url, data=json.dumps({}), content_type="application/json")

        self.assertEqual(response.status_code, 400)

    def test_tc4c_malformed_json_body_returns_400(self):
        response = self.client.post(self.url, data="not json", content_type="application/json")

        self.assertEqual(response.status_code, 400)

    def test_tc5_rate_limit_triggers_after_threshold_exceeded(self):
        """Proves the limiter actually fires, not just that it's wired up: hammer the endpoint
        past RATE_LIMIT_MAX_ATTEMPTS from the same client IP and confirm a 429 shows up, with
        requests under the threshold all still getting a normal (non-429) response."""
        from machines.views import RATE_LIMIT_MAX_ATTEMPTS

        responses = [self._post("NONEXISTENTKEYXX") for _ in range(RATE_LIMIT_MAX_ATTEMPTS)]
        for response in responses:
            self.assertNotEqual(response.status_code, 429)

        limited_response = self._post("NONEXISTENTKEYXX")
        self.assertEqual(limited_response.status_code, 429)
        self.assertEqual(limited_response.json()["valid"], False)

    def test_tc5b_rate_limit_is_keyed_per_ip_not_global(self):
        """A different client IP must get its own, independent counter."""
        from machines.views import RATE_LIMIT_MAX_ATTEMPTS

        for _ in range(RATE_LIMIT_MAX_ATTEMPTS):
            self._post("NONEXISTENTKEYXX")
        exhausted_response = self._post("NONEXISTENTKEYXX")
        self.assertEqual(exhausted_response.status_code, 429)

        fresh_ip_response = self.client.post(
            self.url,
            data=json.dumps({"license_key": "NONEXISTENTKEYXX"}),
            content_type="application/json",
            REMOTE_ADDR="10.0.0.99",
        )
        self.assertNotEqual(fresh_ip_response.status_code, 429)

    def test_get_method_rejected(self):
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, 405)

    def test_endpoint_never_creates_or_modifies_machine_rows_or_license_account(self):
        """
        Updated Session 86: this endpoint DOES now modify License.activated_at (that's the whole
        point of this rewrite) -- but it must still never touch Machine rows or License.account.
        Activation is not a second claim path; it answers a completely different question.
        """
        lic = License.objects.create(account=None, generated_by=self.acc1)
        machine_count_before = Machine.objects.count()
        license_count_before = License.objects.count()

        self._post(lic.license_key)  # unclaimed, now activates -> 200
        self._post("SOMENONEXISTENTKEY")  # -> 404

        self.assertEqual(Machine.objects.count(), machine_count_before)
        self.assertEqual(License.objects.count(), license_count_before)
        lic.refresh_from_db()
        self.assertIsNone(lic.account)  # claim status untouched
        self.assertIsNotNone(lic.activated_at)  # activation status IS touched, by design

    def test_csrf_exempt_like_the_paymongo_webhook(self):
        """Confirms the box (no Django session/CSRF token) can actually call this without a 403,
        same as the existing paymongo_webhook_view pattern this endpoint mirrors."""
        csrf_client = self.client_class(enforce_csrf_checks=True)
        lic = License.objects.create(account=self.acc1, generated_by=self.acc1)
        Machine.objects.create(owner=self.acc1, license_key=lic.license_key)

        response = csrf_client.post(
            self.url, data=json.dumps({"license_key": lic.license_key}), content_type="application/json"
        )

        self.assertEqual(response.status_code, 200)

    def test_license_points_defaults_to_zero_on_a_freshly_created_license(self):
        lic = License.objects.create(account=None, generated_by=self.acc1)
        self.assertEqual(lic.license_points, 0)

    def test_success_response_includes_license_points_matching_db_nonzero_case(self):
        """End Goals #14/#17: not just testing the coincidental default-0 case -- set a real
        nonzero value directly and confirm the response actually reflects it."""
        lic = License.objects.create(account=self.acc1, generated_by=self.acc1, license_points=42)
        Machine.objects.create(owner=self.acc1, license_key=lic.license_key)

        response = self._post(lic.license_key)

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["license_points"], 42)


class LicenseReflashRecoveryTests(TestCase):
    """
    End Goal #21: validate-license and unbind-license working together as the enforced two-step
    reflash-recovery flow. Closes the gap ValidateLicenseViewTests' old
    test_activation_is_idempotent... test used to accept as a known limitation -- a caller who
    only knows the license key (not the recovery password) can no longer "reactivate" an
    already-active license. Recovery now requires Unbind (password-gated, End Goal #20, reused
    unmodified here) followed by Bind again.
    """

    validate_url = "/api/box/validate-license/"
    unbind_url = "/api/box/unbind-license/"

    def setUp(self):
        cache.clear()
        self.acc1 = Account.objects.create_user(phone_number="09171234567", display_name="Op One")

    def _validate(self, license_key):
        return self.client.post(
            self.validate_url,
            data=json.dumps({"license_key": license_key}),
            content_type="application/json",
        )

    def _unbind(self, license_key, password):
        return self.client.post(
            self.unbind_url,
            data=json.dumps({"license_key": license_key, "password": password}),
            content_type="application/json",
        )

    def test_already_active_license_returns_409_with_already_active_flag(self):
        lic = License.objects.create(
            account=self.acc1, generated_by=self.acc1, activated_at=timezone.now()
        )
        activated_before = lic.activated_at

        response = self._validate(lic.license_key)

        self.assertEqual(response.status_code, 409)
        self.assertFalse(response.json()["valid"])
        self.assertTrue(response.json()["already_active"])
        lic.refresh_from_db()
        self.assertEqual(lic.activated_at, activated_before)

    def test_reactivation_after_proper_unbind_succeeds_as_fresh_activation(self):
        lic = License.objects.create(
            account=self.acc1,
            generated_by=self.acc1,
            recovery_password_hash=make_password("correctpw"),
            activated_at=timezone.now(),
        )

        unbind_response = self._unbind(lic.license_key, "correctpw")
        self.assertEqual(unbind_response.status_code, 200)
        lic.refresh_from_db()
        self.assertIsNone(lic.activated_at)

        validate_response = self._validate(lic.license_key)

        self.assertEqual(validate_response.status_code, 200)
        self.assertTrue(validate_response.json()["valid"])
        self.assertNotIn("already_active", validate_response.json())
        lic.refresh_from_db()
        self.assertIsNotNone(lic.activated_at)

    def test_license_points_survive_full_unbind_rebind_cycle(self):
        lic = License.objects.create(
            account=self.acc1,
            generated_by=self.acc1,
            recovery_password_hash=make_password("correctpw"),
            activated_at=timezone.now(),
            license_points=75,
        )

        unbind_response = self._unbind(lic.license_key, "correctpw")
        self.assertEqual(unbind_response.status_code, 200)
        lic.refresh_from_db()
        self.assertEqual(lic.license_points, 75)

        validate_response = self._validate(lic.license_key)
        self.assertEqual(validate_response.status_code, 200)
        self.assertEqual(validate_response.json()["license_points"], 75)
        lic.refresh_from_db()
        self.assertEqual(lic.license_points, 75)

    def test_wrong_password_on_unbind_leaves_license_still_active_and_reactivation_still_blocked(self):
        lic = License.objects.create(
            account=self.acc1,
            generated_by=self.acc1,
            recovery_password_hash=make_password("correctpw"),
            activated_at=timezone.now(),
        )

        unbind_response = self._unbind(lic.license_key, "wrongpw")
        self.assertEqual(unbind_response.status_code, 403)
        lic.refresh_from_db()
        self.assertIsNotNone(lic.activated_at)

        validate_response = self._validate(lic.license_key)
        self.assertEqual(validate_response.status_code, 409)
        self.assertTrue(validate_response.json()["already_active"])


class LicensePointsViewTests(TestCase):
    """POST /api/box/license-points/ -- End Goals #17 box-side periodic poll target."""

    url = "/api/box/license-points/"

    def setUp(self):
        cache.clear()
        self.acc1 = Account.objects.create_user(phone_number="09171234567", display_name="Op One")

    def _post(self, license_key):
        return self.client.post(
            self.url, data=json.dumps({"license_key": license_key}), content_type="application/json"
        )

    def test_reverse_resolves_to_the_documented_path(self):
        self.assertEqual(reverse("machines:license_points"), self.url)

    def test_valid_key_returns_200_with_license_points_matching_db(self):
        lic = License.objects.create(account=self.acc1, generated_by=self.acc1, license_points=17)

        response = self._post(lic.license_key)

        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertTrue(data["valid"])
        self.assertEqual(data["license_points"], 17)

    def test_nonexistent_license_key_returns_404(self):
        response = self._post("ZZZZZZZZZZZZZZZ")
        self.assertEqual(response.status_code, 404)
        self.assertFalse(response.json()["valid"])

    def test_empty_license_key_returns_400(self):
        response = self._post("")
        self.assertEqual(response.status_code, 400)

    def test_malformed_json_body_returns_400(self):
        response = self.client.post(self.url, data="not json", content_type="application/json")
        self.assertEqual(response.status_code, 400)

    def test_get_method_rejected(self):
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, 405)

    def test_endpoint_is_read_only_never_modifies_license_points(self):
        lic = License.objects.create(account=self.acc1, generated_by=self.acc1, license_points=5)
        self._post(lic.license_key)
        lic.refresh_from_db()
        self.assertEqual(lic.license_points, 5)

    def test_rate_limit_triggers_after_threshold_exceeded(self):
        from machines.views import RATE_LIMIT_MAX_ATTEMPTS

        responses = [self._post("NONEXISTENTKEYXX") for _ in range(RATE_LIMIT_MAX_ATTEMPTS)]
        for response in responses:
            self.assertNotEqual(response.status_code, 429)

        limited_response = self._post("NONEXISTENTKEYXX")
        self.assertEqual(limited_response.status_code, 429)

    def test_rate_limit_counter_is_separate_from_validate_license_views(self):
        """
        End Goals #17 design requirement: this endpoint's rate-limit counter must be
        independently namespaced from validate_license_view's -- hammering one endpoint's limit
        must not affect the other's remaining budget for the same client IP.
        """
        from machines.views import RATE_LIMIT_MAX_ATTEMPTS

        validate_url = "/api/box/validate-license/"
        for _ in range(RATE_LIMIT_MAX_ATTEMPTS):
            self.client.post(
                validate_url,
                data=json.dumps({"license_key": "NONEXISTENTKEYXX"}),
                content_type="application/json",
            )
        exhausted_validate_response = self.client.post(
            validate_url,
            data=json.dumps({"license_key": "NONEXISTENTKEYXX"}),
            content_type="application/json",
        )
        self.assertEqual(exhausted_validate_response.status_code, 429)

        # This endpoint's own counter must still be fresh -- unaffected by validate-license's
        # exhausted budget for the same IP.
        fresh_response = self._post("NONEXISTENTKEYXX")
        self.assertNotEqual(fresh_response.status_code, 429)


class UnbindLicenseViewTests(TestCase):
    """
    POST /api/box/unbind-license/ -- End Goals #20, box-side deactivation gated by the license's
    recovery password. Reuses release_failed_attempts/release_locked_until -- same lockout
    semantics dashboard's old release_license_view used to enforce, just a different caller.
    """

    url = "/api/box/unbind-license/"

    def setUp(self):
        cache.clear()
        self.acc1 = Account.objects.create_user(phone_number="09171234567", display_name="Op One")

    def _post(self, license_key, password):
        return self.client.post(
            self.url,
            data=json.dumps({"license_key": license_key, "password": password}),
            content_type="application/json",
        )

    def test_reverse_resolves_to_the_documented_path(self):
        self.assertEqual(reverse("machines:unbind_license"), self.url)

    def test_correct_password_clears_activated_at_and_resets_lockout_fields(self):
        lic = License.objects.create(
            account=self.acc1,
            generated_by=self.acc1,
            recovery_password_hash=make_password("correctpw"),
            activated_at=timezone.now(),
            release_failed_attempts=2,
        )

        response = self._post(lic.license_key, "correctpw")

        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()["valid"])
        lic.refresh_from_db()
        self.assertIsNone(lic.activated_at)
        self.assertEqual(lic.release_failed_attempts, 0)
        self.assertIsNone(lic.release_locked_until)

    def test_wrong_password_returns_403_increments_counter_leaves_activated_at_untouched(self):
        lic = License.objects.create(
            account=self.acc1,
            generated_by=self.acc1,
            recovery_password_hash=make_password("correctpw"),
            activated_at=timezone.now(),
        )
        activated_before = lic.activated_at

        response = self._post(lic.license_key, "wrongpw")

        self.assertEqual(response.status_code, 403)
        self.assertFalse(response.json()["valid"])
        lic.refresh_from_db()
        self.assertEqual(lic.activated_at, activated_before)
        self.assertEqual(lic.release_failed_attempts, 1)

    def test_fifth_wrong_attempt_locks_for_15_minutes(self):
        lic = License.objects.create(
            account=self.acc1,
            generated_by=self.acc1,
            recovery_password_hash=make_password("correctpw"),
        )
        for _ in range(5):
            self._post(lic.license_key, "wrongpw")

        lic.refresh_from_db()
        self.assertEqual(lic.release_failed_attempts, 0)  # reset when lockout triggers
        self.assertIsNotNone(lic.release_locked_until)
        self.assertGreater(lic.release_locked_until, timezone.now())

    def test_locked_license_rejects_even_the_correct_password(self):
        """Confirms the lockout check runs BEFORE the password check -- same pattern
        box-agent's own admin_login lockout already uses."""
        lic = License.objects.create(
            account=self.acc1,
            generated_by=self.acc1,
            recovery_password_hash=make_password("correctpw"),
            release_locked_until=timezone.now() + timedelta(minutes=10),
        )

        response = self._post(lic.license_key, "correctpw")

        self.assertEqual(response.status_code, 423)
        self.assertFalse(response.json()["valid"])
        lic.refresh_from_db()
        self.assertIsNone(lic.activated_at)

    def test_nonexistent_license_key_returns_404(self):
        response = self._post("ZZZZZZZZZZZZZZZ", "anypw")
        self.assertEqual(response.status_code, 404)

    def test_empty_license_key_returns_400(self):
        response = self._post("", "anypw")
        self.assertEqual(response.status_code, 400)

    def test_malformed_json_body_returns_400(self):
        response = self.client.post(self.url, data="not json", content_type="application/json")
        self.assertEqual(response.status_code, 400)

    def test_does_not_touch_machine_account_or_license_points(self):
        lic = License.objects.create(
            account=self.acc1,
            generated_by=self.acc1,
            recovery_password_hash=make_password("correctpw"),
            activated_at=timezone.now(),
            license_points=7,
        )
        machine = Machine.objects.create(owner=self.acc1, license_key=lic.license_key)
        removed_before = machine.removed_at
        balance_before = self.acc1.balance_points

        self._post(lic.license_key, "correctpw")

        lic.refresh_from_db()
        machine.refresh_from_db()
        self.acc1.refresh_from_db()
        self.assertEqual(lic.license_points, 7)
        self.assertEqual(machine.removed_at, removed_before)
        self.assertEqual(self.acc1.balance_points, balance_before)

    def test_rate_limit_counter_is_separate_from_other_box_endpoints(self):
        from machines.views import RATE_LIMIT_MAX_ATTEMPTS

        validate_url = "/api/box/validate-license/"
        for _ in range(RATE_LIMIT_MAX_ATTEMPTS):
            self.client.post(
                validate_url,
                data=json.dumps({"license_key": "NONEXISTENTKEYXX"}),
                content_type="application/json",
            )
        exhausted_validate_response = self.client.post(
            validate_url,
            data=json.dumps({"license_key": "NONEXISTENTKEYXX"}),
            content_type="application/json",
        )
        self.assertEqual(exhausted_validate_response.status_code, 429)

        fresh_response = self._post("NONEXISTENTKEYXX", "anypw")
        self.assertNotEqual(fresh_response.status_code, 429)


class TopupMirrorsLicensePointsTests(TestCase):
    """
    dashboard:topup / dashboard:bulk_topup -- PM decision, session following End Goal #20's
    build: the standalone "Send License Points" view/URL/template is deleted entirely. Every
    top-up (bundle or custom, single or bulk) now credits License.license_points by the exact
    same days_added amount it credits to Machine.days_remaining, same transaction, same single
    wallet debit, no is_activated guard.
    """

    def setUp(self):
        self.acc1 = Account.objects.create_user(
            phone_number="09171234567", display_name="Op One", balance_points=1000
        )
        self.lic = License.objects.create(account=self.acc1, generated_by=self.acc1)
        self.machine = Machine.objects.create(owner=self.acc1, license_key=self.lic.license_key)
        self.client.force_login(self.acc1)

    # -- test case 1: bundle mode mirrors into license_points, from a nonzero start ------------

    def test_bundle_topup_mirrors_days_added_into_license_points_from_nonzero_start(self):
        self.lic.license_points = 20
        self.lic.save(update_fields=["license_points"])
        wallet_before = self.acc1.balance_points

        resp = self.client.post(
            f"/machines/{self.machine.id}/topup/", {"mode": "bundle", "bundle_type": "30day"}
        )
        self.assertEqual(resp.status_code, 302)

        self.machine.refresh_from_db()
        self.lic.refresh_from_db()
        self.acc1.refresh_from_db()
        self.assertEqual(self.machine.days_remaining, 30)
        self.assertEqual(self.lic.license_points, 50)  # 20 + 30, genuinely an F() increment
        # Single wallet debit -- not double-charged for funding two balances.
        self.assertLess(self.acc1.balance_points, wallet_before)
        expected_price = int(BUNDLE_PRICING["30day"]["price"])
        self.assertEqual(self.acc1.balance_points, wallet_before - expected_price)

    # -- test case 2: custom mode mirrors into license_points -----------------------------------

    def test_custom_topup_mirrors_days_added_into_license_points(self):
        resp = self.client.post(
            f"/machines/{self.machine.id}/topup/",
            {"mode": "custom", "custom_days": "15"},
        )
        self.assertEqual(resp.status_code, 302)

        self.machine.refresh_from_db()
        self.lic.refresh_from_db()
        self.assertEqual(self.machine.days_remaining, 15)
        self.assertEqual(self.lic.license_points, 15)

    # -- test case 3: missing License row never blocks the days credit --------------------------

    def test_missing_license_row_still_credits_days_logs_warning_no_crash(self):
        orphan_machine = Machine.objects.create(owner=self.acc1, license_key="NOMATCHINGKEY99")

        with self.assertLogs("dashboard.views", level="WARNING") as cm:
            resp = self.client.post(
                f"/machines/{orphan_machine.id}/topup/", {"mode": "bundle", "bundle_type": "30day"}
            )
        self.assertEqual(resp.status_code, 302)

        orphan_machine.refresh_from_db()
        self.assertEqual(orphan_machine.days_remaining, 30)  # days credit never blocked
        self.assertTrue(any("no License row found" in msg for msg in cm.output))

    # -- test case 4: bulk topup mirrors per-machine, not shared/summed -------------------------

    def test_bulk_topup_mirrors_each_machines_own_bundle_days_not_a_shared_total(self):
        lic2 = License.objects.create(account=self.acc1, generated_by=self.acc1)
        m2 = Machine.objects.create(owner=self.acc1, license_key=lic2.license_key)
        lic3 = License.objects.create(account=self.acc1, generated_by=self.acc1)
        m3 = Machine.objects.create(owner=self.acc1, license_key=lic3.license_key)

        resp = self.client.post(
            "/machines/bulk-topup/",
            {
                "machine_id": [self.machine.id, m2.id, m3.id],
                f"bundle_{self.machine.id}": "30day",
                f"bundle_{m2.id}": "60day",
                f"bundle_{m3.id}": "30day",
            },
        )
        self.assertEqual(resp.status_code, 302)

        self.lic.refresh_from_db()
        lic2.refresh_from_db()
        lic3.refresh_from_db()
        self.assertEqual(self.lic.license_points, 30)
        self.assertEqual(lic2.license_points, 60)
        self.assertEqual(lic3.license_points, 30)

    # -- test case 5: bulk topup, one machine missing a License row, others still credited ------

    def test_bulk_topup_missing_license_on_one_machine_does_not_block_others(self):
        orphan_machine = Machine.objects.create(owner=self.acc1, license_key="NOMATCHINGKEY88")

        with self.assertLogs("dashboard.views", level="WARNING"):
            resp = self.client.post(
                "/machines/bulk-topup/",
                {
                    "machine_id": [self.machine.id, orphan_machine.id],
                    f"bundle_{self.machine.id}": "30day",
                    f"bundle_{orphan_machine.id}": "30day",
                },
            )
        self.assertEqual(resp.status_code, 302)

        self.machine.refresh_from_db()
        orphan_machine.refresh_from_db()
        self.lic.refresh_from_db()
        self.assertEqual(self.machine.days_remaining, 30)
        self.assertEqual(self.lic.license_points, 30)
        self.assertEqual(orphan_machine.days_remaining, 30)  # days credit unaffected

    # -- test case 6: the deleted URL is genuinely gone ------------------------------------------

    def test_send_license_points_url_no_longer_exists(self):
        from django.urls import NoReverseMatch

        with self.assertRaises(NoReverseMatch):
            reverse("dashboard:send_license_points", args=[self.machine.id])

    # -- test case 9: rejected top-ups touch neither balance -------------------------------------

    def test_below_minimum_custom_topup_touches_neither_balance(self):
        days_before = self.machine.days_remaining
        points_before = self.lic.license_points
        wallet_before = self.acc1.balance_points

        resp = self.client.post(
            f"/machines/{self.machine.id}/topup/", {"mode": "custom", "custom_days": "1"}
        )
        self.assertEqual(resp.status_code, 302)

        self.machine.refresh_from_db()
        self.lic.refresh_from_db()
        self.acc1.refresh_from_db()
        self.assertEqual(self.machine.days_remaining, days_before)
        self.assertEqual(self.lic.license_points, points_before)
        self.assertEqual(self.acc1.balance_points, wallet_before)

    def test_insufficient_wallet_balance_touches_neither_balance(self):
        poor_acc = Account.objects.create_user(phone_number="09179876543", balance_points=1)
        lic2 = License.objects.create(account=poor_acc, generated_by=poor_acc)
        m2 = Machine.objects.create(owner=poor_acc, license_key=lic2.license_key)
        self.client.force_login(poor_acc)

        resp = self.client.post(
            f"/machines/{m2.id}/topup/", {"mode": "bundle", "bundle_type": "30day"}
        )
        self.assertEqual(resp.status_code, 302)

        m2.refresh_from_db()
        lic2.refresh_from_db()
        poor_acc.refresh_from_db()
        self.assertEqual(m2.days_remaining, 0)
        self.assertEqual(lic2.license_points, 0)
        self.assertEqual(poor_acc.balance_points, 1)

    # -- topup_view remains fully box-independent, unchanged in that respect --------------------

    def test_topup_works_regardless_of_activation_status(self):
        self.assertFalse(self.lic.is_activated)
        resp = self.client.post(
            f"/machines/{self.machine.id}/topup/", {"mode": "bundle", "bundle_type": "30day"}
        )
        self.assertEqual(resp.status_code, 302)
        self.machine.refresh_from_db()
        self.lic.refresh_from_db()
        self.assertEqual(self.machine.days_remaining, 30)
        self.assertEqual(self.lic.license_points, 30)


class BackfillLicensePointsMigrationTests(TestCase):
    """
    machines/migrations/0012_backfill_license_points_from_days_remaining.py -- one-time,
    PM-requested data migration aligning every existing claimed License's license_points to
    match its Machine's days_remaining, once, at deploy time. Test cases 11-15.
    """

    def setUp(self):
        self.acc1 = Account.objects.create_user(phone_number="09171234567", display_name="Op One")

    def _run_backfill(self):
        import importlib

        module = importlib.import_module(
            "machines.migrations.0012_backfill_license_points_from_days_remaining"
        )
        from django.apps import apps as real_apps

        module.backfill_license_points(real_apps, None)

    def test_mismatched_license_points_gets_force_corrected_to_match_days_remaining(self):
        lic = License.objects.create(account=self.acc1, generated_by=self.acc1, license_points=7)
        machine = Machine.objects.create(owner=self.acc1, license_key=lic.license_key, days_remaining=42)

        self._run_backfill()

        lic.refresh_from_db()
        self.assertEqual(lic.license_points, 42)  # before: 7, after: 42 -- force-corrected

    def test_already_matching_license_points_left_correctly_unchanged(self):
        lic = License.objects.create(account=self.acc1, generated_by=self.acc1, license_points=10)
        Machine.objects.create(owner=self.acc1, license_key=lic.license_key, days_remaining=10)

        self._run_backfill()

        lic.refresh_from_db()
        self.assertEqual(lic.license_points, 10)

    def test_released_machine_still_gets_its_license_backfilled(self):
        """Claim status is not a factor -- same standing principle this codebase already
        applies elsewhere (e.g. cleanup_zero_balance_machines)."""
        lic = License.objects.create(account=self.acc1, generated_by=self.acc1, license_points=0)
        machine = Machine.objects.create(
            owner=self.acc1, license_key=lic.license_key, days_remaining=99,
            removed_at=timezone.now(),
        )

        self._run_backfill()

        lic.refresh_from_db()
        self.assertEqual(lic.license_points, 99)

    def test_never_claimed_license_left_completely_untouched(self):
        lic = License.objects.create(account=None, generated_by=self.acc1, license_points=3)
        points_before = lic.license_points

        self._run_backfill()

        lic.refresh_from_db()
        self.assertEqual(lic.license_points, points_before)

    def test_migration_is_genuinely_wired_and_reachable_via_migrate_command(self):
        """
        Confirms this is a real migration reachable via the normal migrate pathway -- not just
        a standalone function that happens to exist. Uses Django's own MigrationLoader (which
        `migrate` itself uses to discover migrations) rather than actually running `migrate`
        backward+forward here -- SQLite's schema editor can't run schema-touching migrations
        backward inside a TestCase's wrapping transaction (a SQLite/test-harness limitation, not
        a real migration bug; this migration already applied forward cleanly as part of the
        normal test-DB setup, visible in the test runner's own setup output).
        """
        from django.db import connection
        from django.db.migrations.loader import MigrationLoader

        loader = MigrationLoader(connection)
        key = ("machines", "0012_backfill_license_points_from_days_remaining")
        self.assertIn(key, loader.disk_migrations)

        migration = loader.disk_migrations[key]
        self.assertEqual(migration.dependencies, [("machines", "0011_license_license_points")])

        import importlib

        module = importlib.import_module(
            "machines.migrations.0012_backfill_license_points_from_days_remaining"
        )
        # The migration's own RunPython operation must point to the exact same function this
        # test file calls directly elsewhere -- proves it's genuinely wired into the real
        # migration graph, not a lookalike standalone function.
        self.assertIs(migration.operations[0].code, module.backfill_license_points)


class MachineDetailActivationBannerTests(TestCase):
    """machine_detail_view/machine_detail.html -- End Goal #20's dashboard rendering of the
    'claimed but not activated' status."""

    def setUp(self):
        self.acc1 = Account.objects.create_user(
            phone_number="09171234567", display_name="Op One", balance_points=100
        )
        self.client.force_login(self.acc1)

    def test_not_activated_renders_banner_top_up_link_present_no_send_points_link(self):
        """PM decision (this session): 'Send License Points' as a separate link is removed
        entirely, folded into Top Up. The banner still shows for visibility, and Top Up remains
        fully present/unguarded regardless of activation status."""
        lic = License.objects.create(account=self.acc1, generated_by=self.acc1)  # activated_at=None
        machine = Machine.objects.create(owner=self.acc1, license_key=lic.license_key)

        response = self.client.get(reverse("dashboard:machine_detail", args=[machine.id]))

        self.assertEqual(response.status_code, 200)
        self.assertFalse(response.context["is_activated"])
        self.assertContains(response, "Not activated on any box")
        self.assertNotContains(response, "Send License Points")
        self.assertContains(response, reverse("dashboard:topup", args=[machine.id]))

    def test_activated_renders_normally_banner_absent(self):
        lic = License.objects.create(
            account=self.acc1, generated_by=self.acc1, activated_at=timezone.now()
        )
        machine = Machine.objects.create(owner=self.acc1, license_key=lic.license_key)

        response = self.client.get(reverse("dashboard:machine_detail", args=[machine.id]))

        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.context["is_activated"])
        self.assertNotContains(response, "Not activated on any box")


class AttachToNewMachineAdminActionTests(TestCase):
    """
    LicenseAdmin's "Attach to a new Machine" action -- mirrors
    dashboard/views.py::add_machine_view's three-state claim logic (fresh create / reactivate a
    released Machine / reject an already-attached one), applied from the admin side where an
    explicit Account must be chosen rather than assumed from a logged-in session. Test pattern
    (force_login as a superuser, POST to the changelist to get the action redirect, then POST to
    the intermediate form URL) mirrors accounts/tests.py's AccountAdmin gift_points_action tests.
    """

    def setUp(self):
        self.admin_account = Account.objects.create_superuser(
            phone_number="09170000001", display_name="Admin", password="testpass123"
        )
        self.target_account = Account.objects.create_user(
            phone_number="09171112222", display_name="Target Operator"
        )
        self.client.force_login(self.admin_account)

    def _attach_url(self, ids):
        return f"{reverse('admin:machines_license_attach_to_machine')}?ids={ids}"

    def test_action_redirects_to_intermediate_form(self):
        lic = License.objects.create(account=None)
        resp = self.client.post(
            reverse("admin:machines_license_changelist"),
            {"action": "attach_to_new_machine", "_selected_action": [lic.pk]},
        )
        self.assertEqual(resp.status_code, 302)
        self.assertIn(reverse("admin:machines_license_attach_to_machine"), resp.url)
        self.assertIn(f"ids={lic.pk}", resp.url)

    def test_unclaimed_license_creates_machine_and_sets_account(self):
        lic = License.objects.create(account=None)
        machine_count_before = Machine.objects.count()

        resp = self.client.post(
            self._attach_url(lic.pk),
            {"ids": str(lic.pk), "account": self.target_account.pk, "nickname": "Front Counter"},
        )
        self.assertRedirects(resp, reverse("admin:machines_license_changelist"))

        self.assertEqual(Machine.objects.count(), machine_count_before + 1)
        machine = Machine.objects.get(license_key=lic.license_key)
        self.assertEqual(machine.owner_id, self.target_account.pk)
        self.assertEqual(machine.nickname, "Front Counter")

        lic.refresh_from_db()
        self.assertEqual(lic.account_id, self.target_account.pk)
        self.assertTrue(lic.is_claimed)

    def test_already_attached_license_rejected_no_changes(self):
        lic = License.objects.create(account=self.admin_account)
        machine = Machine.objects.create(
            owner=self.admin_account, license_key=lic.license_key, nickname="Original"
        )
        machine_count_before = Machine.objects.count()

        resp = self.client.post(
            self._attach_url(lic.pk),
            {"ids": str(lic.pk), "account": self.target_account.pk, "nickname": "Hijacked"},
        )
        self.assertRedirects(resp, reverse("admin:machines_license_changelist"))

        self.assertEqual(Machine.objects.count(), machine_count_before)  # no new Machine
        machine.refresh_from_db()
        self.assertEqual(machine.owner_id, self.admin_account.pk)  # untouched
        self.assertEqual(machine.nickname, "Original")  # untouched

    def test_released_license_reactivates_same_machine_row(self):
        lic = License.objects.create(account=self.admin_account)
        machine = Machine.objects.create(
            owner=self.admin_account,
            license_key=lic.license_key,
            nickname="Old Name",
            days_remaining=42,
            removed_at=timezone.now(),
        )
        machine_count_before = Machine.objects.count()

        resp = self.client.post(
            self._attach_url(lic.pk),
            {"ids": str(lic.pk), "account": self.target_account.pk, "nickname": "New Name"},
        )
        self.assertRedirects(resp, reverse("admin:machines_license_changelist"))

        self.assertEqual(Machine.objects.count(), machine_count_before)  # no duplicate row
        machine.refresh_from_db()
        self.assertEqual(machine.owner_id, self.target_account.pk)
        self.assertEqual(machine.nickname, "New Name")
        self.assertIsNone(machine.removed_at)
        self.assertEqual(machine.days_remaining, 42)  # balance survives the reactivation

        lic.refresh_from_db()
        self.assertEqual(lic.account_id, self.target_account.pk)

    def test_race_condition_integrity_error_caught_cleanly(self):
        """
        Simulates another request winning the race between this view's own pre-check and its
        Machine.objects.create() call -- Machine.license_key's DB-level unique constraint is the
        real guarantee, same as add_machine_view's own IntegrityError handling. Patched directly
        rather than spinning up real concurrent requests, same reasoning dashboard/views.py's own
        comment gives for why the DB constraint (not the pre-check) is the actual guarantee.
        """
        lic = License.objects.create(account=None)
        machine_count_before = Machine.objects.count()

        with patch(
            "machines.admin.Machine.objects.create",
            side_effect=IntegrityError("duplicate license_key"),
        ):
            resp = self.client.post(
                self._attach_url(lic.pk),
                {"ids": str(lic.pk), "account": self.target_account.pk, "nickname": ""},
            )

        self.assertRedirects(resp, reverse("admin:machines_license_changelist"))
        self.assertEqual(Machine.objects.count(), machine_count_before)  # no partial row
        lic.refresh_from_db()
        self.assertIsNone(lic.account_id)  # not claimed -- the whole atomic block rolled back

    def test_multi_select_one_failure_does_not_block_the_others(self):
        good_lic_1 = License.objects.create(account=None)
        good_lic_2 = License.objects.create(account=None)
        already_claimed_lic = License.objects.create(account=self.admin_account)
        Machine.objects.create(owner=self.admin_account, license_key=already_claimed_lic.license_key)

        ids = f"{good_lic_1.pk},{good_lic_2.pk},{already_claimed_lic.pk}"
        resp = self.client.post(
            self._attach_url(ids),
            {"ids": ids, "account": self.target_account.pk, "nickname": ""},
        )
        self.assertRedirects(resp, reverse("admin:machines_license_changelist"))

        self.assertTrue(Machine.objects.filter(license_key=good_lic_1.license_key).exists())
        self.assertTrue(Machine.objects.filter(license_key=good_lic_2.license_key).exists())
        good_lic_1.refresh_from_db()
        good_lic_2.refresh_from_db()
        self.assertEqual(good_lic_1.account_id, self.target_account.pk)
        self.assertEqual(good_lic_2.account_id, self.target_account.pk)
        # The already-claimed one stays exactly as it was -- still owned by admin_account, not
        # silently reassigned to target_account.
        already_claimed_lic.refresh_from_db()
        self.assertEqual(already_claimed_lic.account_id, self.admin_account.pk)

    def test_no_account_selected_reprompts_form_without_side_effects(self):
        lic = License.objects.create(account=None)
        resp = self.client.post(self._attach_url(lic.pk), {"ids": str(lic.pk), "nickname": "x"})
        self.assertEqual(resp.status_code, 200)  # re-renders form with validation error
        self.assertFalse(Machine.objects.filter(license_key=lic.license_key).exists())
        lic.refresh_from_db()
        self.assertIsNone(lic.account_id)

    def test_non_staff_cannot_reach_the_action(self):
        self.client.logout()
        regular = Account.objects.create_user(phone_number="09175556666", display_name="Regular")
        self.client.force_login(regular)
        lic = License.objects.create(account=None)
        resp = self.client.get(self._attach_url(lic.pk))
        self.assertEqual(resp.status_code, 302)
        self.assertIn("/admin/login/", resp.url)

    def test_dropdown_shows_renamed_label(self):
        """PM feedback: the changelist Action dropdown originally showed "Attach to a new
        Machine," which read as confusingly close to "Delete selected license." Renamed to
        "Attach to Dashboard" -- confirm the changelist page actually renders that text.
        Needs at least one License row -- Django's admin hides the actions dropdown entirely
        on an empty changelist."""
        License.objects.create(account=None)
        resp = self.client.get(reverse("admin:machines_license_changelist"))
        self.assertContains(resp, "Attach to Dashboard")
        self.assertNotContains(resp, "Attach to a new Machine")


class AttachToDashboardAccountSearchTests(TestCase):
    """
    PM feedback: with many dashboard accounts, picking one from a single long <select> dropdown
    is a hassle -- the Account field on the "Attach to Dashboard" form now uses Django admin's
    built-in searchable widget (AutocompleteSelect / select2), searching by phone number or
    display name via AccountAdmin.search_fields, over the same /admin/autocomplete/ endpoint
    Django's own admin change forms use.

    NOTE on phone number search: Account.phone_number is stored normalized (+63XXXXXXXXXX, see
    accounts/models.py::normalize_phone_number). AccountAdmin.get_search_results now also tries
    normalize_phone_number(search_term) so a staff member can type the local format they'd
    naturally use (e.g. "09171112222") and still match the normalized stored value -- this
    benefits AccountAdmin's own changelist search box too, not just this widget, since both go
    through the same search_fields/get_search_results.
    """

    def setUp(self):
        self.admin_account = Account.objects.create_superuser(
            phone_number="09170000001", display_name="Admin", password="testpass123"
        )
        self.match_by_phone = Account.objects.create_user(
            phone_number="09171112222", display_name="Corner Store"
        )
        self.match_by_name = Account.objects.create_user(
            phone_number="09173334444", display_name="Benz Garcel"
        )
        self.non_match = Account.objects.create_user(
            phone_number="09175556666", display_name="Someone Else"
        )
        self.client.force_login(self.admin_account)

    def _autocomplete(self, term):
        return self.client.get(
            reverse("admin:autocomplete"),
            {
                "app_label": "machines",
                "model_name": "license",
                "field_name": "account",
                "term": term,
            },
        )

    def test_account_field_uses_autocomplete_widget(self):
        lic = License.objects.create(account=None)
        resp = self.client.get(
            f"{reverse('admin:machines_license_attach_to_machine')}?ids={lic.pk}"
        )
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, 'class="admin-autocomplete"')
        self.assertContains(resp, 'data-ajax--url="/admin/autocomplete/"')
        self.assertContains(resp, 'data-app-label="machines"')
        self.assertContains(resp, 'data-field-name="account"')

    def test_search_by_phone_number_in_local_format(self):
        """The format a staff member would actually type -- see class docstring."""
        resp = self._autocomplete("09171112222")
        self.assertEqual(resp.status_code, 200)
        results = resp.json()["results"]
        ids = [r["id"] for r in results]
        self.assertIn(str(self.match_by_phone.pk), ids)
        self.assertNotIn(str(self.non_match.pk), ids)

    def test_search_by_phone_number_in_stored_normalized_format(self):
        resp = self._autocomplete("639171112222")
        self.assertEqual(resp.status_code, 200)
        results = resp.json()["results"]
        ids = [r["id"] for r in results]
        self.assertIn(str(self.match_by_phone.pk), ids)
        self.assertNotIn(str(self.non_match.pk), ids)

    def test_search_by_display_name(self):
        resp = self._autocomplete("Benz")
        self.assertEqual(resp.status_code, 200)
        results = resp.json()["results"]
        ids = [r["id"] for r in results]
        self.assertIn(str(self.match_by_name.pk), ids)
        self.assertNotIn(str(self.non_match.pk), ids)

    def test_no_match_returns_empty_results_not_error(self):
        resp = self._autocomplete("nonexistent search term xyz")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()["results"], [])

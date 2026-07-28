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
        self.assertIn("All jobs completed successfully", out.getvalue())

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

    def test_one_day_topup_specifically_cancels_countdown(self):
        """Prompt's TC2 explicitly calls out a 1-day top-up, not just 'some' top-up, to prove
        there is truly no minimum threshold."""
        self.acc1.balance_points = 10
        self.acc1.save(update_fields=["balance_points"])
        m = Machine.objects.create(owner=self.acc1, days_remaining=0)
        self._backdate_zero_balance(m, 5)

        self.client.force_login(self.acc1)
        self.client.post(
            f"/machines/{m.id}/topup/",
            {"mode": "custom", "custom_days": "1"},
        )

        m.refresh_from_db()
        self.assertEqual(m.days_remaining, 1)
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

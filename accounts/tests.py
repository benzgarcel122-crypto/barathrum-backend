from datetime import timedelta
from unittest.mock import patch

from django.contrib.admin.models import LogEntry
from django.test import TestCase
from django.utils import timezone

from .models import Account, OTPCode
from .semaphore_client import SemaphoreAPIError


class AuthFormRedirectTests(TestCase):
    """
    Regression coverage for a real production bug: login_view and signup_view's POST paths
    (real HTML form submissions, not the JSON API variant) both build their redirect via
    quote(phone_number) -- if the `from urllib.parse import quote` import is ever missing, this
    is a NameError that only fires on an actual form POST, which nothing in this test file
    previously exercised. Both call sites are covered here now.
    """

    def setUp(self):
        # STEP 2.1a: OTPCode.issue() now calls out to Semaphore. Mocked here so these tests keep
        # exercising the thing they were actually written for (the quote()/redirect call sites)
        # rather than getting blocked by the new external SMS dependency.
        patcher = patch("accounts.semaphore_client.send_otp")
        self.mock_send_otp = patcher.start()
        self.addCleanup(patcher.stop)

    def test_signup_form_post_redirects_to_verify(self):
        resp = self.client.post(
            "/signup/", {"phone_number": "09171234567", "display_name": "New Op"}
        )
        self.assertEqual(resp.status_code, 302)
        self.assertTrue(resp.url.startswith("/verify/?phone="))

    def test_login_form_post_redirects_to_verify(self):
        Account.objects.create_user(phone_number="09171234567", display_name="Existing Op")
        resp = self.client.post("/login/", {"phone_number": "09171234567"})
        self.assertEqual(resp.status_code, 302)
        self.assertTrue(resp.url.startswith("/verify/?phone="))


class GiftPointsAdminActionTests(TestCase):
    """Admin-only "Gift points to selected accounts" action, requested by PM."""

    def setUp(self):
        self.admin_account = Account.objects.create_superuser(
            phone_number="09170000001", display_name="Admin", password="testpass123"
        )
        self.friend = Account.objects.create_user(
            phone_number="09171112222", display_name="Friend", balance_points=10
        )
        self.other = Account.objects.create_user(
            phone_number="09173334444", display_name="Other", balance_points=0
        )
        self.client.force_login(self.admin_account)

    def test_action_redirects_to_intermediate_form(self):
        resp = self.client.post(
            "/admin/accounts/account/",
            {"action": "gift_points_action", "_selected_action": [self.friend.pk]},
        )
        self.assertEqual(resp.status_code, 302)
        self.assertIn("/admin/accounts/account/gift-points/", resp.url)
        self.assertIn(f"ids={self.friend.pk}", resp.url)

    def test_gift_points_applies_to_selected_account_only(self):
        resp = self.client.post(
            f"/admin/accounts/account/gift-points/?ids={self.friend.pk}",
            {"ids": str(self.friend.pk), "points": "50", "reason": "referral gift"},
        )
        self.assertRedirects(resp, "/admin/accounts/account/")
        self.friend.refresh_from_db()
        self.other.refresh_from_db()
        self.assertEqual(self.friend.balance_points, 60)  # 10 existing + 50 gifted
        self.assertEqual(self.other.balance_points, 0)  # untouched

    def test_gift_points_multiple_accounts(self):
        ids = f"{self.friend.pk},{self.other.pk}"
        self.client.post(
            f"/admin/accounts/account/gift-points/?ids={ids}",
            {"ids": ids, "points": "25", "reason": ""},
        )
        self.friend.refresh_from_db()
        self.other.refresh_from_db()
        self.assertEqual(self.friend.balance_points, 35)
        self.assertEqual(self.other.balance_points, 25)

    def test_gift_points_writes_admin_log_entry(self):
        self.client.post(
            f"/admin/accounts/account/gift-points/?ids={self.friend.pk}",
            {"ids": str(self.friend.pk), "points": "50", "reason": "referral gift"},
        )
        entry = LogEntry.objects.filter(object_id=str(self.friend.pk)).latest("action_time")
        self.assertIn("Gifted 50 points", entry.change_message)
        self.assertIn("referral gift", entry.change_message)
        self.assertEqual(entry.user_id, self.admin_account.pk)

    def test_gift_points_rejects_non_positive_amount(self):
        resp = self.client.post(
            f"/admin/accounts/account/gift-points/?ids={self.friend.pk}",
            {"ids": str(self.friend.pk), "points": "0", "reason": ""},
        )
        self.assertEqual(resp.status_code, 200)  # re-renders form with validation error
        self.friend.refresh_from_db()
        self.assertEqual(self.friend.balance_points, 10)  # unchanged

    def test_non_staff_cannot_reach_gift_points(self):
        self.client.logout()
        regular = Account.objects.create_user(phone_number="09175556666", display_name="Regular")
        self.client.force_login(regular)
        resp = self.client.get(f"/admin/accounts/account/gift-points/?ids={self.friend.pk}")
        # Django admin's own login_required wrapper bounces non-staff users to the admin login.
        self.assertEqual(resp.status_code, 302)
        self.assertIn("/admin/login/", resp.url)


class OTPDeliveryTests(TestCase):
    """
    STEP 2.1a: real SMS delivery via Semaphore, 60s issuance cooldown, fail-closed error
    handling, plus regression coverage for verify_view's pre-existing lockout/expiry logic (which
    this task did not change, but which previously had zero test coverage -- Session 38 finding
    #12). `accounts.semaphore_client.send_otp` is mocked in every test here; the real Semaphore
    API is never called.
    """

    def setUp(self):
        # Patch send_otp on the semaphore_client MODULE (not a `from .semaphore_client import
        # send_otp` binding) -- accounts/models.py::OTPCode.issue() calls it as
        # `semaphore_client.send_otp(...)` via a module-level import specifically so this kind of
        # patch takes effect.
        patcher = patch("accounts.semaphore_client.send_otp")
        self.mock_send_otp = patcher.start()
        self.addCleanup(patcher.stop)

    def test_second_issuance_within_60s_is_blocked(self):
        first = OTPCode.issue("09171234567")
        self.assertEqual(self.mock_send_otp.call_count, 1)

        wait = OTPCode.seconds_until_next_issue_allowed("+639171234567")
        self.assertGreater(wait, 0)
        self.assertLessEqual(wait, 60)

        resp = self.client.post("/signup/", {"phone_number": "09171234567", "display_name": "X"})
        self.assertEqual(resp.status_code, 429)
        # Still just the one send from setup/first issue -- the cooldown-blocked request never
        # reached OTPCode.issue(), so send_otp was never called a second time.
        self.assertEqual(self.mock_send_otp.call_count, 1)
        self.assertEqual(OTPCode.objects.filter(phone_number="+639171234567").count(), 1)

    def test_issuance_allowed_again_after_60s(self):
        first = OTPCode.issue("09171234567")
        # Backdate rather than sleep in the test, per the task's explicit instruction.
        OTPCode.objects.filter(pk=first.pk).update(
            created_at=timezone.now() - timedelta(seconds=61)
        )

        self.assertEqual(OTPCode.seconds_until_next_issue_allowed("+639171234567"), 0)

        resp = self.client.post(
            "/signup/", {"phone_number": "09171234567", "display_name": "X"}
        )
        self.assertEqual(resp.status_code, 302)
        self.assertEqual(self.mock_send_otp.call_count, 2)
        self.assertEqual(OTPCode.objects.filter(phone_number="+639171234567").count(), 2)

    def test_five_wrong_codes_lock_out_then_correct_code_still_rejected(self):
        # verify_view checks failed_attempts >= MAX_FAILED_ATTEMPTS BEFORE incrementing on a wrong
        # guess (confirmed by reading accounts/views.py directly, untouched by this task) -- so
        # 5 wrong submissions each return 400 while raising failed_attempts 0->5, and it's the
        # NEXT (6th) submission that actually hits the lockout branch and returns 429.
        otp = OTPCode.issue("09171234567")
        for _ in range(OTPCode.MAX_FAILED_ATTEMPTS):
            resp = self.client.post(
                "/verify/", {"phone_number": "09171234567", "code": "000000"}
            )
            self.assertEqual(resp.status_code, 400)
        otp.refresh_from_db()
        self.assertEqual(otp.failed_attempts, OTPCode.MAX_FAILED_ATTEMPTS)

        # 6th submission, this time with the actually-correct code -- still rejected, because the
        # lockout is per-OTPCode-row, not per-guess-correctness.
        resp = self.client.post("/verify/", {"phone_number": "09171234567", "code": otp.code})
        self.assertEqual(resp.status_code, 429)
        self.assertFalse(Account.objects.filter(phone_number="+639171234567").exists())

    def test_expired_code_rejected_even_if_correct(self):
        otp = OTPCode.issue("09171234567")
        OTPCode.objects.filter(pk=otp.pk).update(
            expires_at=timezone.now() - timedelta(seconds=1)
        )
        resp = self.client.post("/verify/", {"phone_number": "09171234567", "code": otp.code})
        self.assertEqual(resp.status_code, 400)
        self.assertFalse(Account.objects.filter(phone_number="+639171234567").exists())

    def test_semaphore_failure_is_fail_closed(self):
        self.mock_send_otp.side_effect = SemaphoreAPIError("SEMAPHORE_API_KEY is not set.")

        resp = self.client.post(
            "/signup/", {"phone_number": "09171234567", "display_name": "X"}
        )
        self.assertEqual(resp.status_code, 503)
        self.assertFalse(OTPCode.objects.filter(phone_number="+639171234567").exists())

        Account.objects.create_user(phone_number="09179998888", display_name="Existing")
        resp = self.client.post("/login/", {"phone_number": "09179998888"})
        self.assertEqual(resp.status_code, 503)
        self.assertFalse(OTPCode.objects.filter(phone_number="+639179998888").exists())

    def test_successful_send_called_with_normalized_number_and_generated_code(self):
        otp = OTPCode.issue("09171234567")
        self.mock_send_otp.assert_called_once()
        called_number, called_code = self.mock_send_otp.call_args[0][:2]
        self.assertEqual(called_number, "+639171234567")
        self.assertEqual(called_code, otp.code)
        self.assertRegex(otp.code, r"^\d{6}$")

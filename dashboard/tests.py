from datetime import timedelta

from django.contrib.auth import get_user_model
from django.contrib.auth.hashers import check_password, make_password
from django.test import TestCase
from django.utils import timezone

from accounts.models import PointTransfer
from machines.models import License, Machine, Transaction

Account = get_user_model()


class Session31LicenseDecoupleTests(TestCase):
    """STEP 2.5 (Session 31) / STEP 2.6 (Session 32): standalone license generation + landing
    page test cases. Session 32 reversed ownership from "assigned at generation" to "assigned
    at claim time" -- see the tests below that specifically cover that reversal."""

    def setUp(self):
        self.acc1 = Account.objects.create_user(phone_number="09171234567", display_name="Op One")
        self.acc2 = Account.objects.create_user(phone_number="09179876543", display_name="Op Two")

    def test_tc5_landing_page_for_logged_out(self):
        resp = self.client.get("/")
        self.assertEqual(resp.status_code, 200)
        self.assertTemplateUsed(resp, "dashboard/landing.html")

    def test_tc6_dashboard_home_for_logged_in(self):
        self.client.force_login(self.acc1)
        resp = self.client.get("/")
        self.assertEqual(resp.status_code, 200)
        self.assertTemplateUsed(resp, "dashboard/home.html")

    def test_tc1_generate_license_key_is_ownerless(self):
        """STEP 2.6 (Session 32): generating a license no longer assigns an owner -- account
        stays None until some Machine claims it, regardless of who was logged in when it was
        generated."""
        self.acc1.balance_points = 20  # covers the license generation fee added later
        self.acc1.save(update_fields=["balance_points"])
        self.client.force_login(self.acc1)
        resp = self.client.post(
            "/licenses/generate/",
            {"recovery_password": "recover123", "recovery_password_confirm": "recover123"},
        )
        self.assertEqual(resp.status_code, 200)
        lic = License.objects.get()  # only one exists at this point in the test
        self.assertIsNone(lic.account_id)
        self.assertFalse(lic.is_claimed)
        self.assertIn(lic.license_key.encode(), resp.content)

    def test_tc2_any_account_can_claim_unclaimed_license(self):
        """STEP 2.6 (Session 32) REVERSAL: any logged-in account can claim any unclaimed
        license, regardless of who generated it -- the Session 31 "must match the generating
        account" rule is gone. On claim, License.account is repurposed to record the claimant."""
        lic = License.objects.create(account=None)  # simulates a key generated via the button
        self.client.force_login(self.acc1)
        resp = self.client.post(
            "/machines/add/", {"license_key": lic.license_key, "nickname": "Corner"}
        )
        self.assertEqual(resp.status_code, 200)
        machine = Machine.objects.get(license_key=lic.license_key)
        self.assertEqual(machine.owner_id, self.acc1.id)
        self.assertEqual(machine.nickname, "Corner")
        lic.refresh_from_db()
        self.assertTrue(lic.is_claimed)
        self.assertEqual(lic.account_id, self.acc1.id)  # repurposed to record the claimant

    def test_tc3_garbage_key_rejected(self):
        self.client.force_login(self.acc1)
        before = Machine.objects.count()
        resp = self.client.post("/machines/add/", {"license_key": "NOTAREALKEY12345", "nickname": "x"})
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(Machine.objects.count(), before)

    def test_tc4_already_claimed_key_rejected(self):
        """Post-claim protection still holds -- a key already attached to a Machine can't be
        claimed again by anyone, even though the pre-claim ownership check is gone."""
        lic = License.objects.create(account=None)
        Machine.objects.create(owner=self.acc1, license_key=lic.license_key)
        self.client.force_login(self.acc2)  # a THIRD party, different from the original claimant
        resp = self.client.post("/machines/add/", {"license_key": lic.license_key, "nickname": "dup"})
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(Machine.objects.filter(license_key=lic.license_key).count(), 1)

    def test_grandfathered_owned_licenses_are_not_reset(self):
        """
        Confirms the account -> nullable schema change (migration 0005) is additive only and
        doesn't touch existing data: a License row that already has an account set (simulating
        a pre-Session-32 row created under the old Session 31 rule) keeps that account exactly
        as-is. Nothing in the model, the migration, or the surrounding app code ever nulls out
        an existing account value -- it's only ever set (never cleared) by generate_license_view
        (to None, for NEW rows) or by add_machine_view's claim logic (to the claimant).
        """
        lic = License.objects.create(account=self.acc1)
        lic.refresh_from_db()
        self.assertEqual(lic.account_id, self.acc1.id)

    def test_tc7_full_regression(self):
        self.client.force_login(self.acc1)
        lic = License.objects.create(account=None)
        machine = Machine.objects.create(owner=self.acc1, license_key=lic.license_key)
        for route in ["/", "/machines/add/", "/licenses/generate/", "/wallet/topup/", "/account/", "/download/"]:
            self.assertEqual(self.client.get(route).status_code, 200, route)
        self.assertEqual(self.client.get(f"/machines/{machine.id}/").status_code, 200)
        self.assertEqual(self.client.get(f"/machines/{machine.id}/topup/").status_code, 200)
        self.client.logout()
        self.assertEqual(self.client.get("/download/").status_code, 200)

    def test_logout_button_present_in_dashboard_nav(self):
        """Was flagged as missing by the PM -- nav bar (visible on every dashboard page) now
        has a POST-form Log Out button pointing at the 'logout' URL."""
        self.client.force_login(self.acc1)
        resp = self.client.get("/")
        self.assertContains(resp, 'action="/logout/"')
        self.assertContains(resp, "Log Out")

    def test_logout_via_post_ends_session(self):
        self.client.force_login(self.acc1)
        resp = self.client.post("/logout/")
        self.assertRedirects(resp, "/login/")
        # Session is really gone -- a subsequent request to a login-required page redirects to login.
        resp = self.client.get("/machines/add/")
        self.assertEqual(resp.status_code, 302)
        self.assertIn("/login/", resp.url)

    def test_logout_rejects_get(self):
        """GET must not log the user out -- see logout_view's docstring for why (CSRF/prefetch)."""
        self.client.force_login(self.acc1)
        resp = self.client.get("/logout/")
        self.assertEqual(resp.status_code, 405)
        # Still logged in -- confirm a login-required page still works.
        self.assertEqual(self.client.get("/machines/add/").status_code, 200)

    def test_logout_requires_login(self):
        """Anonymous POST to /logout/ shouldn't error -- login_required just redirects to login."""
        resp = self.client.post("/logout/")
        self.assertEqual(resp.status_code, 302)
        self.assertIn("/login/", resp.url)


class SendPointsTests(TestCase):
    """Peer-to-peer wallet transfer, test cases per the Send Points task spec."""

    def setUp(self):
        self.acc1 = Account.objects.create_user(
            phone_number="09171234567", display_name="Op One", balance_points=500
        )
        self.acc2 = Account.objects.create_user(
            phone_number="09179876543", display_name="Op Two", balance_points=0
        )

    def test_tc1_successful_transfer(self):
        self.client.force_login(self.acc1)
        resp = self.client.post(
            "/send-points/",
            {"recipient_phone": "09179876543", "amount": "100", "note": "float"},
        )
        self.assertRedirects(resp, "/send-points/")
        self.acc1.refresh_from_db()
        self.acc2.refresh_from_db()
        self.assertEqual(self.acc1.balance_points, 400)
        self.assertEqual(self.acc2.balance_points, 100)
        transfer = PointTransfer.objects.get()
        self.assertEqual(transfer.sender_id, self.acc1.id)
        self.assertEqual(transfer.receiver_id, self.acc2.id)
        self.assertEqual(transfer.amount, 100)

        # Both sides see it in their own dashboard history.
        resp_sender = self.client.get("/send-points/")
        self.assertContains(resp_sender, "Op Two")
        self.client.force_login(self.acc2)
        resp_receiver = self.client.get("/send-points/")
        self.assertContains(resp_receiver, "Op One")

    def test_tc2_insufficient_balance_rejected(self):
        self.acc1.balance_points = 0
        self.acc1.save(update_fields=["balance_points"])
        self.client.force_login(self.acc1)
        resp = self.client.post(
            "/send-points/", {"recipient_phone": "09179876543", "amount": "1"}
        )
        self.assertRedirects(resp, "/send-points/")
        self.acc1.refresh_from_db()
        self.acc2.refresh_from_db()
        self.assertEqual(self.acc1.balance_points, 0)
        self.assertEqual(self.acc2.balance_points, 0)
        self.assertEqual(PointTransfer.objects.count(), 0)

    def test_tc3_cannot_send_to_self(self):
        self.client.force_login(self.acc1)
        resp = self.client.post(
            "/send-points/", {"recipient_phone": "09171234567", "amount": "50"}
        )
        self.assertRedirects(resp, "/send-points/")
        self.acc1.refresh_from_db()
        self.assertEqual(self.acc1.balance_points, 500)
        self.assertEqual(PointTransfer.objects.count(), 0)

    def test_tc4_unknown_recipient_rejected(self):
        self.client.force_login(self.acc1)
        resp = self.client.post(
            "/send-points/", {"recipient_phone": "09990001111", "amount": "50"}
        )
        self.assertRedirects(resp, "/send-points/")
        self.acc1.refresh_from_db()
        self.assertEqual(self.acc1.balance_points, 500)
        self.assertEqual(PointTransfer.objects.count(), 0)

    def test_tc5_zero_amount_rejected(self):
        self.client.force_login(self.acc1)
        resp = self.client.post(
            "/send-points/", {"recipient_phone": "09179876543", "amount": "0"}
        )
        self.assertRedirects(resp, "/send-points/")
        self.assertEqual(PointTransfer.objects.count(), 0)

    def test_tc5b_negative_amount_rejected(self):
        self.client.force_login(self.acc1)
        resp = self.client.post(
            "/send-points/", {"recipient_phone": "09179876543", "amount": "-5"}
        )
        self.assertRedirects(resp, "/send-points/")
        self.assertEqual(PointTransfer.objects.count(), 0)

    def test_invalid_phone_number_rejected(self):
        self.client.force_login(self.acc1)
        resp = self.client.post(
            "/send-points/", {"recipient_phone": "12345", "amount": "50"}
        )
        self.assertRedirects(resp, "/send-points/")
        self.assertEqual(PointTransfer.objects.count(), 0)

    def test_send_points_requires_login(self):
        resp = self.client.get("/send-points/")
        self.assertEqual(resp.status_code, 302)
        self.assertIn("/login/", resp.url)

    def test_send_points_nav_link_present(self):
        self.client.force_login(self.acc1)
        resp = self.client.get("/")
        self.assertContains(resp, "Send Points")
        self.assertContains(resp, "/send-points/")


class LicensePointsExclusionTests(TestCase):
    """
    End Goal #23 guardrail: License.license_points must never be movable by send_points_view or
    any other wallet-transfer path. Nothing in the current codebase lets this happen -- this class
    exists so a future change can never quietly wire the two systems together without a loud,
    immediate test failure. See accounts/models.py::PointTransfer and
    dashboard/views.py::send_points_view for the accompanying warning docstrings.
    """

    def setUp(self):
        self.acc1 = Account.objects.create_user(
            phone_number="09171234567", display_name="Op One", balance_points=500
        )
        self.acc2 = Account.objects.create_user(
            phone_number="09179876543", display_name="Op Two", balance_points=0
        )

    def test_wallet_transfer_never_touches_license_points(self):
        lic = License.objects.create(account=self.acc1, license_points=50)
        Machine.objects.create(owner=self.acc1, license_key=lic.license_key, nickname="Box A")
        license_points_before = lic.license_points

        self.client.force_login(self.acc1)
        resp = self.client.post(
            "/send-points/",
            {"recipient_phone": "09179876543", "amount": "100", "note": "float"},
        )
        self.assertRedirects(resp, "/send-points/")

        # The transfer itself worked normally...
        self.acc1.refresh_from_db()
        self.acc2.refresh_from_db()
        self.assertEqual(self.acc1.balance_points, 400)
        self.assertEqual(self.acc2.balance_points, 100)
        transfer = PointTransfer.objects.get()
        self.assertEqual(transfer.sender_id, self.acc1.id)
        self.assertEqual(transfer.receiver_id, self.acc2.id)
        self.assertEqual(transfer.amount, 100)

        # ...and the license's own points balance is byte-identical to before the transfer.
        lic.refresh_from_db()
        self.assertEqual(lic.license_points, license_points_before)
        self.assertEqual(lic.license_points, 50)

    def test_point_transfer_model_has_no_license_relationship(self):
        """Static/structural guarantee: fails immediately if a future migration ever adds an FK
        (or any other field) from PointTransfer to License or Machine, which is the most likely
        way this rule could get silently broken."""
        for field in PointTransfer._meta.get_fields():
            self.assertNotEqual(field.name, "license")
            self.assertNotEqual(field.name, "machine")
            related_model = getattr(field, "related_model", None)
            if related_model is not None:
                self.assertNotEqual(related_model.__name__, "License")
                self.assertNotEqual(related_model.__name__, "Machine")


class LicenseGenerationFeeTests(TestCase):
    """Anti-abuse fee on generate_license_view: LICENSE_GENERATION_FEE points deducted from the
    generating operator's own wallet balance, per this task's numbered test cases."""

    def setUp(self):
        self.acc1 = Account.objects.create_user(
            phone_number="09171234567", display_name="Op One", balance_points=100
        )

    def test_tc1_sufficient_balance_deducts_fee_and_creates_license(self):
        self.client.force_login(self.acc1)
        resp = self.client.post(
            "/licenses/generate/",
            {"recovery_password": "recover123", "recovery_password_confirm": "recover123"},
        )
        self.assertEqual(resp.status_code, 200)
        self.acc1.refresh_from_db()
        self.assertEqual(self.acc1.balance_points, 80)  # 100 - LICENSE_GENERATION_FEE(20)
        lic = License.objects.get()
        self.assertIsNone(lic.account_id)
        self.assertEqual(lic.generated_by_id, self.acc1.id)

    def test_tc2_insufficient_balance_rejected(self):
        self.acc1.balance_points = 10
        self.acc1.save(update_fields=["balance_points"])
        self.client.force_login(self.acc1)
        resp = self.client.post("/licenses/generate/")
        self.assertRedirects(resp, "/licenses/generate/")
        self.acc1.refresh_from_db()
        self.assertEqual(self.acc1.balance_points, 10)
        self.assertEqual(License.objects.count(), 0)

    def test_tc3_exact_fee_balance_succeeds_and_zeroes_out(self):
        self.acc1.balance_points = 20
        self.acc1.save(update_fields=["balance_points"])
        self.client.force_login(self.acc1)
        resp = self.client.post(
            "/licenses/generate/",
            {"recovery_password": "recover123", "recovery_password_confirm": "recover123"},
        )
        self.assertEqual(resp.status_code, 200)
        self.acc1.refresh_from_db()
        self.assertEqual(self.acc1.balance_points, 0)
        self.assertEqual(License.objects.count(), 1)

    def test_tc5_get_page_shows_fee_and_balance(self):
        self.client.force_login(self.acc1)
        resp = self.client.get("/licenses/generate/")
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "20 points")
        self.assertContains(resp, "100")  # current balance


class LicenseGenerationHistoryTests(TestCase):
    """History section on generate_license.html: shows the operator's own generated licenses,
    correctly distinguishing claimed vs. unclaimed, and never another operator's licenses."""

    def setUp(self):
        self.acc1 = Account.objects.create_user(
            phone_number="09171234567", display_name="Op One", balance_points=100
        )
        self.acc2 = Account.objects.create_user(
            phone_number="09179876543", display_name="Op Two", balance_points=100
        )

    def test_tc4_history_shows_own_licenses_claimed_and_unclaimed_only(self):
        unclaimed_lic = License.objects.create(account=None, generated_by=self.acc1)
        claimed_lic = License.objects.create(account=None, generated_by=self.acc1)
        Machine.objects.create(
            license_key=claimed_lic.license_key, owner=self.acc1, nickname="Front Desk Box"
        )
        other_op_lic = License.objects.create(account=None, generated_by=self.acc2)

        self.client.force_login(self.acc1)
        resp = self.client.get("/licenses/generate/")
        self.assertEqual(resp.status_code, 200)

        self.assertContains(resp, unclaimed_lic.license_key)
        self.assertContains(resp, "Unclaimed")
        self.assertContains(resp, claimed_lic.license_key)
        self.assertContains(resp, "Claimed")
        self.assertContains(resp, "Front Desk Box")
        # Never another operator's license.
        self.assertNotContains(resp, other_op_lic.license_key)


class RemoveLicenseFeatureTests(TestCase):
    """
    Dashboard-side "Remove License" (dashboard:remove_license) -- replaces the old password-
    gated Release License. Test case 4 from the Developer prompt: no password anywhere in this
    flow, confirm-only, does not touch License at all. The old password/lockout coverage
    (TC5/TC6 from the original Release License feature) now lives box-side instead, see
    machines.tests.UnbindLicenseViewTests for that behavior's new home.
    """

    def setUp(self):
        self.acc1 = Account.objects.create_user(
            phone_number="09171234567", display_name="Op One", balance_points=100
        )
        self.acc2 = Account.objects.create_user(
            phone_number="09179876543", display_name="Op Two", balance_points=100
        )

    def test_get_renders_confirm_page_no_password_field_anywhere(self):
        lic = License.objects.create(account=self.acc1, recovery_password_hash=make_password("correctpw"))
        machine = Machine.objects.create(owner=self.acc1, license_key=lic.license_key, nickname="Box A")

        self.client.force_login(self.acc1)
        resp = self.client.get(f"/machines/{machine.id}/remove/")

        self.assertEqual(resp.status_code, 200)
        self.assertNotContains(resp, "password")
        self.assertNotContains(resp, "Password")

    def test_post_sets_removed_at_and_redirects_home(self):
        lic = License.objects.create(account=self.acc1, recovery_password_hash=make_password("correctpw"))
        machine = Machine.objects.create(
            owner=self.acc1, license_key=lic.license_key, nickname="Box A", days_remaining=42
        )
        Transaction.objects.create(
            machine=machine, bundle_type="30day", days_added=30, amount_paid_pesos=27
        )
        txn_count_before = Transaction.objects.filter(machine=machine).count()
        days_before = machine.days_remaining

        self.client.force_login(self.acc1)
        resp = self.client.post(f"/machines/{machine.id}/remove/")
        self.assertRedirects(resp, "/")

        machine.refresh_from_db()
        self.assertIsNotNone(machine.removed_at)
        self.assertEqual(machine.days_remaining, days_before)
        self.assertEqual(Transaction.objects.filter(machine=machine).count(), txn_count_before)

        home_resp = self.client.get("/")
        self.assertNotContains(home_resp, "Box A")

    def test_reclaim_after_remove_reactivates_same_machine_row(self):
        lic = License.objects.create(account=self.acc1, recovery_password_hash=make_password("correctpw"))
        machine = Machine.objects.create(
            owner=self.acc1, license_key=lic.license_key, nickname="Box A", days_remaining=42
        )
        original_machine_id = machine.id
        days_before = machine.days_remaining

        self.client.force_login(self.acc1)
        self.client.post(f"/machines/{machine.id}/remove/")

        self.client.force_login(self.acc2)
        resp = self.client.post(
            "/machines/add/", {"license_key": lic.license_key, "nickname": "Reclaimed Box"}
        )
        self.assertEqual(resp.status_code, 200)

        reactivated = Machine.objects.get(license_key=lic.license_key)
        self.assertEqual(reactivated.id, original_machine_id)
        self.assertIsNone(reactivated.removed_at)
        self.assertEqual(reactivated.owner_id, self.acc2.id)
        self.assertEqual(reactivated.days_remaining, days_before)

    def test_removed_machine_excluded_from_owner_views(self):
        lic = License.objects.create(account=self.acc1, recovery_password_hash=make_password("correctpw"))
        machine = Machine.objects.create(
            owner=self.acc1, license_key=lic.license_key, nickname="Box A", days_remaining=10
        )
        machine_id = machine.id

        self.client.force_login(self.acc1)
        self.client.post(f"/machines/{machine.id}/remove/")

        # First GET consumes the one-time success flash message (which legitimately names the
        # machine, e.g. "Box A removed from your dashboard...") -- the second GET confirms the
        # machine listing area itself no longer shows it, independent of that message.
        self.client.get("/")
        home_resp = self.client.get("/")
        self.assertNotContains(home_resp, "Box A")

        detail_resp = self.client.get(f"/machines/{machine_id}/")
        self.assertEqual(detail_resp.status_code, 404)

    def test_remove_license_view_404s_for_non_owner(self):
        lic = License.objects.create(account=self.acc1, recovery_password_hash=make_password("correctpw"))
        machine = Machine.objects.create(owner=self.acc1, license_key=lic.license_key)

        self.client.force_login(self.acc2)
        resp = self.client.get(f"/machines/{machine.id}/remove/")
        self.assertEqual(resp.status_code, 404)

    def test_does_not_touch_license_row_at_all(self):
        """This view no longer looks up or modifies License in any way -- confirm the recovery
        password fields and activated_at are completely untouched by a remove."""
        lic = License.objects.create(
            account=self.acc1, recovery_password_hash=make_password("correctpw"), activated_at=timezone.now()
        )
        machine = Machine.objects.create(owner=self.acc1, license_key=lic.license_key)
        hash_before = lic.recovery_password_hash
        activated_before = lic.activated_at

        self.client.force_login(self.acc1)
        self.client.post(f"/machines/{machine.id}/remove/")

        lic.refresh_from_db()
        self.assertEqual(lic.recovery_password_hash, hash_before)
        self.assertEqual(lic.activated_at, activated_before)
        self.assertEqual(lic.release_failed_attempts, 0)
        self.assertIsNone(lic.release_locked_until)


class GenerateLicenseHistoryZeroBalanceCountdownTests(TestCase):
    """
    STEP 2.7 item 5 follow-up (this task): the License Generation History table's "expires in
    N days" text for claimed machines whose zero-balance cleanup countdown has started.
    """

    def setUp(self):
        self.acc1 = Account.objects.create_user(
            phone_number="09171234567", display_name="Op One", balance_points=1000
        )

    # TC1: positive-balance claimed machine (zero_balance_since is None) -> no "expires in" text.
    def test_tc1_positive_balance_machine_shows_no_expiry_text(self):
        lic = License.objects.create(account=self.acc1, generated_by=self.acc1)
        Machine.objects.create(
            owner=self.acc1, license_key=lic.license_key, nickname="Box A", days_remaining=10
        )

        self.client.force_login(self.acc1)
        resp = self.client.get("/licenses/generate/")

        self.assertContains(resp, "Box A")
        self.assertNotContains(resp, "expires in")

    # TC2: zero_balance_since just set (0-1 days elapsed) -> "expires in 20 days" (or 19).
    def test_tc2_freshly_zeroed_machine_shows_near_full_countdown(self):
        lic = License.objects.create(account=self.acc1, generated_by=self.acc1)
        machine = Machine.objects.create(
            owner=self.acc1, license_key=lic.license_key, nickname="Box B", days_remaining=0
        )
        Machine.objects.filter(pk=machine.pk).update(zero_balance_since=timezone.now())

        self.client.force_login(self.acc1)
        resp = self.client.get("/licenses/generate/")

        self.assertContains(resp, "expires in 20 day")

    # TC3: backdated 16+ days -> "expires in N days" renders, N <= 5, in red.
    def test_tc3_near_expiry_machine_renders_in_red(self):
        lic = License.objects.create(account=self.acc1, generated_by=self.acc1)
        machine = Machine.objects.create(
            owner=self.acc1, license_key=lic.license_key, nickname="Box C", days_remaining=0
        )
        Machine.objects.filter(pk=machine.pk).update(
            zero_balance_since=timezone.now() - timedelta(days=16)
        )

        self.client.force_login(self.acc1)
        resp = self.client.get("/licenses/generate/")
        content = resp.content.decode()

        self.assertIn("expires in 4 day", content)
        # Confirm the danger badge class is actually applied to the span wrapping this specific
        # text, not just present somewhere unrelated on the page.
        red_span_index = content.find("badge-danger")
        expiry_text_index = content.find("expires in 4 day")
        self.assertNotEqual(red_span_index, -1)
        self.assertNotEqual(expiry_text_index, -1)
        self.assertLess(red_span_index, expiry_text_index)
        self.assertLess(expiry_text_index - red_span_index, 200)  # same span, not a coincidence

    # TC4: top-up clears zero_balance_since -> "expires in" text disappears on next page load.
    def test_tc4_topup_removes_expiry_text_on_next_load(self):
        lic = License.objects.create(account=self.acc1, generated_by=self.acc1)
        machine = Machine.objects.create(
            owner=self.acc1, license_key=lic.license_key, nickname="Box D", days_remaining=0
        )
        Machine.objects.filter(pk=machine.pk).update(
            zero_balance_since=timezone.now() - timedelta(days=10)
        )

        self.client.force_login(self.acc1)
        before = self.client.get("/licenses/generate/")
        self.assertContains(before, "expires in")

        self.client.post(f"/machines/{machine.id}/topup/", {"mode": "custom", "custom_days": "10"})

        after = self.client.get("/licenses/generate/")
        self.assertNotContains(after, "expires in")


class MinimumTopupPointsTests(TestCase):
    """Enforces MINIMUM_TOPUP_POINTS (10) on both the wallet PayMongo top-up and the per-machine
    custom top-up; bundle and bulk top-ups are unaffected."""

    def setUp(self):
        self.acc1 = Account.objects.create_user(
            phone_number="09171234567", display_name="Op One", balance_points=1000
        )

    # TC1: wallet top-up with amount = 9 -> rejected, no Payment created.
    def test_tc1_wallet_topup_below_minimum_rejected(self):
        from machines.models import Payment

        self.client.force_login(self.acc1)
        resp = self.client.post("/wallet/topup/", {"amount": "9"})

        self.assertRedirects(resp, "/wallet/topup/")
        self.assertEqual(Payment.objects.count(), 0)

    # TC2: wallet top-up with amount = 10 -> succeeds, proceeds to PayMongo checkout.
    def test_tc2_wallet_topup_at_minimum_succeeds(self):
        from unittest.mock import patch
        from machines.models import Payment

        self.client.force_login(self.acc1)
        with patch(
            "dashboard.views._initiate_paymongo_checkout",
            return_value="https://checkout.paymongo.example/session123",
        ):
            resp = self.client.post("/wallet/topup/", {"amount": "10"})

        self.assertRedirects(
            resp, "https://checkout.paymongo.example/session123", fetch_redirect_response=False
        )
        self.assertEqual(Payment.objects.count(), 1)
        self.assertEqual(Payment.objects.get().amount_pesos, 10)

    # TC3: custom machine top-up with 9 days -> rejected, no deduction, no Transaction.
    def test_tc3_custom_topup_below_minimum_rejected(self):
        machine = Machine.objects.create(owner=self.acc1, days_remaining=5)

        self.client.force_login(self.acc1)
        resp = self.client.post(
            f"/machines/{machine.id}/topup/", {"mode": "custom", "custom_days": "9"}
        )

        self.assertRedirects(resp, f"/machines/{machine.id}/topup/?tab=custom")
        machine.refresh_from_db()
        self.acc1.refresh_from_db()
        self.assertEqual(machine.days_remaining, 5)
        self.assertEqual(self.acc1.balance_points, 1000)
        self.assertEqual(Transaction.objects.filter(machine=machine).count(), 0)

    # TC4: custom machine top-up with 10 days -> succeeds as normal.
    def test_tc4_custom_topup_at_minimum_succeeds(self):
        machine = Machine.objects.create(owner=self.acc1, days_remaining=5)

        self.client.force_login(self.acc1)
        resp = self.client.post(
            f"/machines/{machine.id}/topup/", {"mode": "custom", "custom_days": "10"}
        )

        self.assertRedirects(resp, "/")
        machine.refresh_from_db()
        self.assertEqual(machine.days_remaining, 15)
        self.assertEqual(Transaction.objects.filter(machine=machine).count(), 1)

    # TC5: bundle top-up and bulk top-up remain completely unaffected.
    def test_tc5_bundle_and_bulk_topup_unaffected(self):
        machine = Machine.objects.create(owner=self.acc1, days_remaining=5)

        self.client.force_login(self.acc1)
        resp = self.client.post(
            f"/machines/{machine.id}/topup/", {"mode": "bundle", "bundle_type": "30day"}
        )
        self.assertRedirects(resp, "/")
        machine.refresh_from_db()
        self.assertEqual(machine.days_remaining, 35)

        machine2 = Machine.objects.create(owner=self.acc1, days_remaining=5)
        bulk_resp = self.client.post(
            "/machines/bulk-topup/",
            {"machine_id": [machine2.id], f"bundle_{machine2.id}": "30day"},
        )
        self.assertRedirects(bulk_resp, "/")
        machine2.refresh_from_db()
        self.assertEqual(machine2.days_remaining, 35)


class DownloadPlaceholderPageTests(TestCase):
    """Session 88: download_placeholder_view itself was already correct (public,
    correctly wired) -- this covers the template replacement, confirming the page
    renders for a logged-out visitor and contains the real install command rather
    than the old "coming soon" stub."""

    def test_download_page_loads_for_logged_out_visitor(self):
        resp = self.client.get("/download/")
        self.assertEqual(resp.status_code, 200)
        self.assertTemplateUsed(resp, "dashboard/download_placeholder.html")

    def test_download_page_contains_install_command(self):
        resp = self.client.get("/download/")
        self.assertContains(
            resp,
            "https://github.com/benzgarcel122-crypto/barathrum-box-agent/releases/latest/download/install.sh",
        )
        self.assertContains(resp, "sudo bash")

    def test_download_page_no_longer_says_coming_soon(self):
        resp = self.client.get("/download/")
        self.assertNotContains(resp, "coming soon")

    def test_download_page_contains_checksum_verification_commands(self):
        """Session 90 (Security Findings #14, tracker row 27): the download page must
        instruct operators to verify install.sh's published checksum before running it,
        not pipe it unverified straight into sudo bash."""
        resp = self.client.get("/download/")
        self.assertContains(
            resp,
            "https://github.com/benzgarcel122-crypto/barathrum-box-agent/releases/latest/download/install.sh",
        )
        self.assertContains(
            resp,
            "https://github.com/benzgarcel122-crypto/barathrum-box-agent/releases/latest/download/install.sh.sha256",
        )
        self.assertContains(resp, "sha256sum -c install.sh.sha256 && sudo bash install.sh")

    def test_download_page_no_longer_has_unverified_pipe_to_sudo_bash(self):
        """Session 90: the old one-liner that piped install.sh straight into sudo bash
        with no verification step in between must be gone."""
        resp = self.client.get("/download/")
        self.assertNotContains(
            resp,
            "curl -fsSL https://github.com/benzgarcel122-crypto/barathrum-box-agent/releases/latest/download/install.sh | sudo bash",
            html=False,
        )

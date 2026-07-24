from django.contrib.auth import get_user_model
from django.test import TestCase

from accounts.models import PointTransfer
from machines.models import License, Machine

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
        self.client.force_login(self.acc1)
        resp = self.client.post("/licenses/generate/")
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

from django.contrib.auth import get_user_model
from django.test import TestCase

from machines.models import License, Machine

Account = get_user_model()


class Session31LicenseDecoupleTests(TestCase):
    """STEP 2.5 (Session 31): standalone license generation + landing page test cases."""

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

    def test_tc1_generate_license_key(self):
        self.client.force_login(self.acc1)
        resp = self.client.post("/licenses/generate/")
        self.assertEqual(resp.status_code, 200)
        lic = License.objects.get(account=self.acc1)
        self.assertFalse(lic.is_claimed)
        self.assertIn(lic.license_key.encode(), resp.content)

    def test_tc2_add_machine_with_valid_own_key(self):
        self.client.force_login(self.acc1)
        lic = License.objects.create(account=self.acc1)
        resp = self.client.post("/machines/add/", {"license_key": lic.license_key, "nickname": "Corner"})
        self.assertEqual(resp.status_code, 200)
        machine = Machine.objects.get(license_key=lic.license_key)
        self.assertEqual(machine.owner_id, self.acc1.id)
        self.assertEqual(machine.nickname, "Corner")
        self.assertTrue(lic.is_claimed)

    def test_tc3_garbage_key_rejected(self):
        self.client.force_login(self.acc1)
        before = Machine.objects.count()
        resp = self.client.post("/machines/add/", {"license_key": "NOTAREALKEY12345", "nickname": "x"})
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(Machine.objects.count(), before)

    def test_tc4_already_claimed_key_rejected(self):
        self.client.force_login(self.acc1)
        lic = License.objects.create(account=self.acc1)
        Machine.objects.create(owner=self.acc1, license_key=lic.license_key)
        resp = self.client.post("/machines/add/", {"license_key": lic.license_key, "nickname": "dup"})
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(Machine.objects.filter(license_key=lic.license_key).count(), 1)

    def test_cross_account_key_cannot_be_claimed(self):
        lic2 = License.objects.create(account=self.acc2)
        self.client.force_login(self.acc1)
        self.client.post("/machines/add/", {"license_key": lic2.license_key, "nickname": "steal"})
        self.assertFalse(Machine.objects.filter(license_key=lic2.license_key).exists())

    def test_tc7_full_regression(self):
        self.client.force_login(self.acc1)
        lic = License.objects.create(account=self.acc1)
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

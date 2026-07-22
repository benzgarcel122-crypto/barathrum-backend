from django.contrib.admin.models import LogEntry
from django.test import TestCase

from .models import Account


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

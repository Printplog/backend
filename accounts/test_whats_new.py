from django.contrib.auth import get_user_model
from rest_framework.test import APITestCase

User = get_user_model()


class WhatsNewSeenTests(APITestCase):
    def setUp(self):
        self.user = User.objects.create_user("reader", "reader@example.com", "pass-12345")
        self.client.force_authenticate(self.user)

    def test_current_user_reports_last_seen_update(self):
        response = self.client.get("/api/accounts/user/")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["last_seen_update"], "")

    def test_user_can_mark_an_update_as_seen(self):
        response = self.client.patch("/api/accounts/user/", {"last_seen_update": "2026-10-support-inbox"}, format="json")
        self.assertEqual(response.status_code, 200)
        self.user.refresh_from_db()
        self.assertEqual(self.user.last_seen_update, "2026-10-support-inbox")

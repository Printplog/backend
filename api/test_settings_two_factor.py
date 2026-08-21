import pyotp
from django.test import TestCase
from django.utils import timezone
from rest_framework import status
from rest_framework.test import APIClient

from accounts.models import AdminTwoFactorProfile, User
from accounts.two_factor import encrypt_secret, verify_user_code
from api.models import SiteSettings


class SiteSettingsTwoFactorTests(TestCase):
    def setUp(self):
        self.admin = User.objects.create_superuser(
            username="settings-admin",
            email="settings-admin@example.com",
            password="A-long-test-password-829!",
        )
        self.secret = pyotp.random_base32(length=32)
        AdminTwoFactorProfile.objects.create(
            user=self.admin,
            encrypted_secret=encrypt_secret(self.secret),
            confirmed_at=timezone.now(),
        )
        self.client = APIClient()
        self.client.force_authenticate(self.admin)
        self.url = "/api/settings/1/"

    def test_fresh_authenticator_code_allows_settings_update(self):
        response = self.client.patch(
            self.url,
            {
                "maintenance_mode": True,
                "two_factor_code": pyotp.TOTP(self.secret).now(),
            },
            format="json",
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertTrue(SiteSettings.get_settings().maintenance_mode)

    def test_settings_update_requires_authenticator_code(self):
        response = self.client.patch(
            self.url,
            {"maintenance_mode": True},
            format="json",
        )

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertFalse(SiteSettings.get_settings().maintenance_mode)

    def test_wrong_authenticator_code_is_rejected(self):
        response = self.client.patch(
            self.url,
            {"maintenance_mode": True, "two_factor_code": "000000"},
            format="json",
        )

        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)
        self.assertFalse(SiteSettings.get_settings().maintenance_mode)

    def test_normal_user_cannot_update_site_settings(self):
        user = User.objects.create_user(
            username="settings-normal-user",
            email="settings-normal@example.com",
            password="A-long-test-password-829!",
        )
        self.client.force_authenticate(user)

        response = self.client.patch(
            self.url,
            {
                "maintenance_mode": True,
                "two_factor_code": pyotp.TOTP(self.secret).now(),
            },
            format="json",
        )

        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)
        self.assertFalse(SiteSettings.get_settings().maintenance_mode)

    def test_authenticator_code_cannot_be_replayed(self):
        code = pyotp.TOTP(self.secret).now()
        first = self.client.patch(
            self.url,
            {"maintenance_mode": True, "two_factor_code": code},
            format="json",
        )
        second = self.client.patch(
            self.url,
            {"maintenance_mode": False, "two_factor_code": code},
            format="json",
        )

        self.assertEqual(first.status_code, status.HTTP_200_OK)
        self.assertEqual(second.status_code, status.HTTP_403_FORBIDDEN)
        self.assertTrue(SiteSettings.get_settings().maintenance_mode)

    def test_code_used_for_login_can_still_confirm_one_settings_update(self):
        code = pyotp.TOTP(self.secret).now()
        self.assertTrue(verify_user_code(self.admin, code))

        response = self.client.patch(
            self.url,
            {"maintenance_mode": True, "two_factor_code": code},
            format="json",
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertTrue(SiteSettings.get_settings().maintenance_mode)

    def test_email_code_endpoint_no_longer_accepts_requests(self):
        response = self.client.post("/api/settings/request-code/", {}, format="json")

        self.assertEqual(response.status_code, status.HTTP_405_METHOD_NOT_ALLOWED)

    def test_invalid_settings_payload_does_not_consume_authenticator_code(self):
        code = pyotp.TOTP(self.secret).now()
        invalid = self.client.patch(
            self.url,
            {"api_default_rate_limit": 0, "two_factor_code": code},
            format="json",
        )
        valid = self.client.patch(
            self.url,
            {"api_default_rate_limit": 240, "two_factor_code": code},
            format="json",
        )

        self.assertEqual(invalid.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(valid.status_code, status.HTTP_200_OK)
        self.assertEqual(SiteSettings.get_settings().api_default_rate_limit, 240)

import pyotp
from analytics.models import AuditLog
from django.test import TestCase
from django.utils import timezone
from rest_framework import status
from rest_framework.test import APIClient

from accounts.models import AdminTwoFactorProfile, User
from accounts.two_factor import encrypt_secret, verify_user_code
from api.models import IntegrationSecret, SiteSettings
from api.utils.integration_secrets import get_integration_secret


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

    def test_admin_can_replace_write_only_integration_secrets(self):
        api_key = "re_test_admin_managed_key"
        webhook_secret = "whsec_test_admin_managed_secret"

        response = self.client.patch(
            self.url,
            {
                "resend_api_key": api_key,
                "resend_webhook_secret": webhook_secret,
                "two_factor_code": pyotp.TOTP(self.secret).now(),
            },
            format="json",
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertNotContains(response, api_key)
        self.assertNotContains(response, webhook_secret)
        self.assertNotIn("resend_api_key", response.data)
        self.assertNotIn("resend_webhook_secret", response.data)
        self.assertTrue(response.data["resend_api_key_configured"])
        self.assertTrue(response.data["resend_webhook_secret_configured"])

        stored = IntegrationSecret.objects.get(key="resend.api_key")
        self.assertNotEqual(stored.encrypted_value, api_key)
        self.assertEqual(stored.updated_by, self.admin)
        self.assertEqual(get_integration_secret("resend_api_key"), api_key)
        self.assertEqual(
            get_integration_secret("resend_webhook_secret"),
            webhook_secret,
        )
        audit_details = AuditLog.objects.get(action="UPDATE_SETTINGS").details
        self.assertEqual(
            audit_details["integration_secrets_updated"],
            ["resend_api_key", "resend_webhook_secret"],
        )
        self.assertNotIn(api_key, str(audit_details))
        self.assertNotIn(webhook_secret, str(audit_details))

    def test_secret_values_are_never_returned_by_settings_api(self):
        api_key = "re_test_never_return_this_value"
        self.client.patch(
            self.url,
            {
                "resend_api_key": api_key,
                "two_factor_code": pyotp.TOTP(self.secret).now(),
            },
            format="json",
        )

        response = self.client.get("/api/settings/")

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertNotContains(response, api_key)
        self.assertNotIn("resend_api_key", response.data)
        self.assertTrue(response.data["resend_api_key_configured"])

    def test_blank_secret_input_keeps_existing_secret(self):
        initial_key = "re_test_keep_existing_value"
        first_code = pyotp.TOTP(self.secret).now()
        first = self.client.patch(
            self.url,
            {
                "resend_api_key": initial_key,
                "two_factor_code": first_code,
            },
            format="json",
        )
        self.assertEqual(first.status_code, status.HTTP_200_OK)

        profile = self.admin.admin_two_factor
        profile.last_settings_counter = -1
        profile.save(update_fields=["last_settings_counter"])
        second = self.client.patch(
            self.url,
            {
                "resend_api_key": "  ",
                "two_factor_code": pyotp.TOTP(self.secret).now(),
            },
            format="json",
        )

        self.assertEqual(second.status_code, status.HTTP_200_OK)
        self.assertEqual(get_integration_secret("resend_api_key"), initial_key)

    def test_invalid_secret_does_not_consume_authenticator_code(self):
        code = pyotp.TOTP(self.secret).now()
        invalid = self.client.patch(
            self.url,
            {"resend_api_key": "not-a-resend-key", "two_factor_code": code},
            format="json",
        )
        valid = self.client.patch(
            self.url,
            {"resend_api_key": "re_test_valid_key", "two_factor_code": code},
            format="json",
        )

        self.assertEqual(invalid.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(valid.status_code, status.HTTP_200_OK)

    def test_regular_user_cannot_see_secret_configuration_metadata(self):
        user = User.objects.create_user(
            username="settings-public-reader",
            email="settings-public-reader@example.com",
            password="A-long-test-password-829!",
        )
        self.client.force_authenticate(user)

        response = self.client.get("/api/settings/")

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertNotIn("resend_api_key_configured", response.data)
        self.assertNotIn("resend_webhook_secret_configured", response.data)

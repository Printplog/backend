from unittest.mock import patch

import pyotp
from django.core.cache import cache
from django.test import TestCase
from rest_framework import status
from rest_framework.test import APIClient
from rest_framework_simplejwt.tokens import RefreshToken

from .models import AdminTwoFactorProfile, User


class AdminTwoFactorLoginTests(TestCase):
    def setUp(self):
        cache.clear()
        self.password = "A-long-test-password-829!"
        self.normal_user = User.objects.create_user(
            username="normal-user",
            email="normal@example.com",
            password=self.password,
        )
        self.admin = User.objects.create_superuser(
            username="admin-user",
            email="admin@example.com",
            password=self.password,
        )
        self.client = APIClient()

    def login(self, username):
        return self.client.post(
            "/api/accounts/login/",
            {"username": username, "password": self.password},
            format="json",
        )

    @patch("api.utils.email_service.EmailService.send_login_notification")
    def test_normal_user_login_is_unchanged(self, send_login_notification):
        response = self.login(self.normal_user.username)

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertNotIn("requires_two_factor", response.json())
        self.assertTrue(response.cookies["access_token"].value)
        self.assertTrue(response.cookies["refresh_token"].value)
        send_login_notification.assert_called_once()

    @patch("api.utils.email_service.EmailService.send_login_notification")
    def test_admin_receives_no_session_before_two_factor(self, send_login_notification):
        response = self.login(self.admin.username)

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.json()["requires_two_factor"], True)
        self.assertEqual(response.json()["setup_required"], True)
        self.assertEqual(response.cookies["access_token"].value, "")
        self.assertEqual(response.cookies["refresh_token"].value, "")
        self.assertTrue(response.cookies["admin_2fa_challenge"].value)
        self.assertEqual(
            response.cookies["admin_2fa_challenge"]["httponly"],
            True,
        )
        send_login_notification.assert_not_called()

    @patch("api.utils.email_service.EmailService.send_login_notification")
    def test_admin_can_enroll_and_complete_login(self, send_login_notification):
        self.login(self.admin.username)
        setup = self.client.post("/api/accounts/two-factor/setup/", {}, format="json")

        self.assertEqual(setup.status_code, status.HTTP_200_OK)
        self.assertTrue(setup.json()["provisioning_uri"].startswith("otpauth://totp/"))
        secret = setup.json()["manual_key"]

        verify = self.client.post(
            "/api/accounts/two-factor/verify/",
            {"code": pyotp.TOTP(secret).now()},
            format="json",
        )

        self.assertEqual(verify.status_code, status.HTTP_200_OK)
        self.assertEqual(len(verify.json()["recovery_codes"]), 8)
        self.assertTrue(verify.cookies["access_token"].value)
        self.assertTrue(verify.cookies["refresh_token"].value)
        self.assertEqual(verify.cookies["admin_2fa_challenge"].value, "")
        self.assertTrue(AdminTwoFactorProfile.objects.filter(user=self.admin).exists())
        send_login_notification.assert_called_once()

        current_user = self.client.get("/api/accounts/user/")
        self.assertEqual(current_user.status_code, status.HTTP_200_OK)
        self.assertEqual(current_user.json()["username"], self.admin.username)

    @patch("api.utils.email_service.EmailService.send_login_notification")
    def test_recovery_code_is_single_use(self, send_login_notification):
        self.login(self.admin.username)
        setup = self.client.post("/api/accounts/two-factor/setup/", {}, format="json")
        secret = setup.json()["manual_key"]
        enrolled = self.client.post(
            "/api/accounts/two-factor/verify/",
            {"code": pyotp.TOTP(secret).now()},
            format="json",
        )
        recovery_code = enrolled.json()["recovery_codes"][0]

        self.login(self.admin.username)
        recovered = self.client.post(
            "/api/accounts/two-factor/verify/",
            {"code": recovery_code},
            format="json",
        )
        self.assertEqual(recovered.status_code, status.HTTP_200_OK)

        profile = AdminTwoFactorProfile.objects.get(user=self.admin)
        self.assertEqual(len(profile.recovery_code_hashes), 7)

        self.login(self.admin.username)
        replay = self.client.post(
            "/api/accounts/two-factor/verify/",
            {"code": recovery_code},
            format="json",
        )
        self.assertEqual(replay.status_code, status.HTTP_400_BAD_REQUEST)

    def test_old_admin_jwt_without_mfa_claim_is_rejected(self):
        refresh = RefreshToken.for_user(self.admin)
        self.client.cookies["access_token"] = str(refresh.access_token)

        response = self.client.get("/api/accounts/user/")

        self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)
        self.assertIn("two-factor", response.json()["detail"].lower())

    @patch("requests.get")
    def test_google_login_cannot_bypass_admin_two_factor(self, google_get):
        self.admin.google_id = "google-admin-id"
        self.admin.save(update_fields=["google_id"])
        google_get.return_value.status_code = 200
        google_get.return_value.json.return_value = {
            "sub": "google-admin-id",
            "email": self.admin.email,
            "email_verified": True,
        }

        response = self.client.post(
            "/api/accounts/google/",
            {"access_token": "test-google-token"},
            format="json",
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.json()["requires_two_factor"], True)
        self.assertEqual(response.cookies["access_token"].value, "")
        self.assertEqual(response.cookies["refresh_token"].value, "")

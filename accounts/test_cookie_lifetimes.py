from datetime import timedelta
from django.test import SimpleTestCase, override_settings
from accounts.views import _jwt_cookie_settings

class CookieLifetimeTests(SimpleTestCase):
    @override_settings(SIMPLE_JWT={
        'ACCESS_TOKEN_LIFETIME': timedelta(minutes=30),
        'REFRESH_TOKEN_LIFETIME': timedelta(days=7),
    })
    def test_cookies_follow_configured_token_lifetimes(self):
        self.assertEqual(_jwt_cookie_settings()['max_age'], 30 * 60)
        self.assertEqual(_jwt_cookie_settings(refresh=True)['max_age'], 7 * 24 * 60 * 60)
        self.assertTrue(_jwt_cookie_settings(refresh=True)['httponly'])

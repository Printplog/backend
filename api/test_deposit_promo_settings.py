from decimal import Decimal
from django.test import TestCase
from api.models import SiteSettings


class DepositPromoSettingsTests(TestCase):
    def test_defaults(self):
        s = SiteSettings.get_settings()
        self.assertFalse(s.enable_deposit_promo)
        self.assertEqual(s.deposit_promo_min_amount, Decimal("30.00"))
        self.assertEqual(s.deposit_promo_percentage, Decimal("100.00"))
        self.assertEqual(s.deposit_promo_max_bonus, Decimal("50.00"))
        self.assertEqual(s.deposit_promo_expiry_days, 7)
        self.assertEqual(s.deposit_promo_message, "")

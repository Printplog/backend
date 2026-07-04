from decimal import Decimal
from django.contrib.auth import get_user_model
from django.test import TestCase
from django.utils import timezone
from datetime import timedelta
from wallet.models import Wallet, DepositBonus

User = get_user_model()


class DepositBonusModelTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username="u1", email="u1@t.com", password="x")
        self.wallet, _ = Wallet.objects.get_or_create(user=self.user)

    def test_wallet_has_zero_bonus_balance_by_default(self):
        self.assertEqual(self.wallet.bonus_balance, Decimal("0.00"))

    def test_can_create_active_bonus(self):
        b = DepositBonus.objects.create(
            wallet=self.wallet,
            amount_granted=Decimal("30.00"),
            amount_remaining=Decimal("30.00"),
            percentage_applied=Decimal("100.00"),
            expires_at=timezone.now() + timedelta(days=7),
            status=DepositBonus.Status.ACTIVE,
        )
        self.assertEqual(b.status, "active")
        self.assertEqual(self.wallet.bonuses.count(), 1)


class CreditBonusTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username="u2", email="u2@t.com", password="x")
        self.wallet, _ = Wallet.objects.get_or_create(user=self.user)

    def test_credit_bonus_creates_record_and_updates_cache(self):
        b = self.wallet.credit_bonus(Decimal("30.00"), percentage=Decimal("100.00"))
        self.wallet.refresh_from_db()
        self.assertEqual(self.wallet.bonus_balance, Decimal("30.00"))
        self.assertEqual(b.amount_remaining, Decimal("30.00"))
        self.assertEqual(b.status, "active")

    def test_spendable_balance_sums_both(self):
        self.wallet.balance = Decimal("10.00")
        self.wallet.save(update_fields=["balance"])
        self.wallet.credit_bonus(Decimal("30.00"))
        self.wallet.refresh_from_db()
        self.assertEqual(self.wallet.spendable_balance, Decimal("40.00"))

    def test_credit_bonus_rejects_nonpositive(self):
        with self.assertRaises(ValueError):
            self.wallet.credit_bonus(Decimal("0.00"))

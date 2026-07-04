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


from django.core.exceptions import ValidationError


class DebitBonusFirstTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username="u3", email="u3@t.com", password="x")
        self.wallet, _ = Wallet.objects.get_or_create(user=self.user)
        self.wallet.balance = Decimal("100.00")
        self.wallet.save(update_fields=["balance"])

    def _bonus(self, amount, days):
        return self.wallet.credit_bonus(
            Decimal(amount), expires_at=timezone.now() + timedelta(days=days)
        )

    def test_spends_bonus_before_real_balance(self):
        self._bonus("30.00", days=7)
        self.wallet.refresh_from_db()
        self.wallet.debit(Decimal("20.00"), description="buy")
        self.wallet.refresh_from_db()
        self.assertEqual(self.wallet.bonus_balance, Decimal("10.00"))
        self.assertEqual(self.wallet.balance, Decimal("100.00"))

    def test_soonest_expiry_consumed_first(self):
        self._bonus("15.00", days=30)   # later
        self._bonus("15.00", days=2)    # sooner -> spent first
        self.wallet.refresh_from_db()
        self.wallet.debit(Decimal("15.00"))
        soon = self.wallet.bonuses.order_by("expires_at").first()
        self.assertEqual(soon.status, "spent")
        self.assertEqual(soon.amount_remaining, Decimal("0.00"))

    def test_overflow_pulls_from_real_balance(self):
        self._bonus("30.00", days=7)
        self.wallet.refresh_from_db()
        self.wallet.debit(Decimal("50.00"))
        self.wallet.refresh_from_db()
        self.assertEqual(self.wallet.bonus_balance, Decimal("0.00"))
        self.assertEqual(self.wallet.balance, Decimal("80.00"))  # 100 - (50-30)

    def test_insufficient_combined_raises(self):
        self._bonus("30.00", days=7)
        self.wallet.balance = Decimal("5.00")
        self.wallet.save(update_fields=["balance"])
        self.wallet.refresh_from_db()
        with self.assertRaises(ValidationError):
            self.wallet.debit(Decimal("100.00"))

    def test_bonus_makes_small_balance_sufficient(self):
        self.wallet.balance = Decimal("5.00")
        self.wallet.save(update_fields=["balance"])
        self._bonus("30.00", days=7)
        self.wallet.refresh_from_db()
        self.wallet.debit(Decimal("20.00"))
        self.wallet.refresh_from_db()
        self.assertEqual(self.wallet.bonus_balance, Decimal("10.00"))
        self.assertEqual(self.wallet.balance, Decimal("5.00"))

    def test_never_expiring_bonus_spent_last(self):
        self.wallet.credit_bonus(Decimal("15.00"), expires_at=None)
        self._bonus("15.00", days=5)
        self.wallet.refresh_from_db()
        self.wallet.debit(Decimal("15.00"))
        dated = self.wallet.bonuses.filter(expires_at__isnull=False).first()
        self.assertEqual(dated.status, "spent")
        self.assertEqual(dated.amount_remaining, Decimal("0.00"))
        never_expiring = self.wallet.bonuses.filter(expires_at__isnull=True).first()
        self.assertEqual(never_expiring.status, "active")
        self.assertEqual(never_expiring.amount_remaining, Decimal("15.00"))

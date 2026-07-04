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


from decimal import Decimal as D
from wallet.models import Transaction
from api.models import SiteSettings


def _grant_if_qualifies(wallet, credited_amount, tx):
    """Mirror of the webhook promo block, used to unit-test the rule."""
    from api.models import SiteSettings
    from django.utils import timezone as tz
    from datetime import timedelta as td
    s = SiteSettings.get_settings()
    if s.enable_deposit_promo and credited_amount >= s.deposit_promo_min_amount:
        raw = (credited_amount * s.deposit_promo_percentage / D("100"))
        bonus = min(raw, s.deposit_promo_max_bonus).quantize(D("0.01"))
        if bonus > 0:
            expires = (tz.now() + td(days=s.deposit_promo_expiry_days)) if s.deposit_promo_expiry_days else None
            wallet.credit_bonus(bonus, expires_at=expires, source_transaction=tx, percentage=s.deposit_promo_percentage)


class WebhookPromoRuleTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username="u4", email="u4@t.com", password="x")
        self.wallet, _ = Wallet.objects.get_or_create(user=self.user)
        self.s = SiteSettings.get_settings()
        self.s.enable_deposit_promo = True
        self.s.deposit_promo_min_amount = D("30.00")
        self.s.deposit_promo_percentage = D("100.00")
        self.s.deposit_promo_max_bonus = D("50.00")
        self.s.deposit_promo_expiry_days = 7
        self.s.save()

    def test_qualifying_deposit_grants_capped_bonus(self):
        _grant_if_qualifies(self.wallet, D("500.00"), None)
        self.wallet.refresh_from_db()
        self.assertEqual(self.wallet.bonus_balance, D("50.00"))  # capped

    def test_exact_percentage_below_cap(self):
        _grant_if_qualifies(self.wallet, D("30.00"), None)
        self.wallet.refresh_from_db()
        self.assertEqual(self.wallet.bonus_balance, D("30.00"))

    def test_below_min_grants_nothing(self):
        _grant_if_qualifies(self.wallet, D("29.99"), None)
        self.wallet.refresh_from_db()
        self.assertEqual(self.wallet.bonus_balance, D("0.00"))

    def test_promo_off_grants_nothing(self):
        self.s.enable_deposit_promo = False
        self.s.save()
        _grant_if_qualifies(self.wallet, D("100.00"), None)
        self.wallet.refresh_from_db()
        self.assertEqual(self.wallet.bonus_balance, D("0.00"))


class SpendIntegrationTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username="u5", email="u5@t.com", password="x")
        self.wallet, _ = Wallet.objects.get_or_create(user=self.user)

    def test_can_afford_with_bonus_only(self):
        self.wallet.credit_bonus(Decimal("30.00"))
        self.wallet.refresh_from_db()
        # affordability must use spendable_balance, not balance
        self.assertGreaterEqual(self.wallet.spendable_balance, Decimal("10.00"))
        tx = self.wallet.debit(Decimal("10.00"), description="template")
        self.wallet.refresh_from_db()
        self.assertEqual(self.wallet.bonus_balance, Decimal("20.00"))
        self.assertEqual(self.wallet.balance, Decimal("0.00"))
        self.assertEqual(tx.amount, Decimal("-10.00"))

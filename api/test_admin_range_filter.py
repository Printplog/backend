from datetime import timedelta
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.utils import timezone
from rest_framework.test import APIRequestFactory, force_authenticate

from api.views.admin import AdminUsers
from api.views.wallet import TransactionHistoryView
from wallet.models import Transaction, Wallet

User = get_user_model()


class AdminRangeFilterTests(TestCase):
    """
    The All / 1D / 7D / 30D / 6M / 1Y control has to narrow the table itself,
    not just the stat cards above it. "All" means no range param at all, and
    must never collapse to the helper's default 1-day window.
    """

    def setUp(self):
        self.factory = APIRequestFactory()
        self.admin = User.objects.create_superuser(
            username="range-admin", email="range-admin@example.com", password="pw"
        )
        now = timezone.now()
        self.ages = (0, 3, 15, 200)
        for age in self.ages:
            user = User.objects.create_user(
                username=f"joined-{age}d-ago",
                email=f"joined-{age}@example.com",
                password="pw",
            )
            User.objects.filter(pk=user.pk).update(date_joined=now - timedelta(days=age))

    def get(self, view, path, query=""):
        request = self.factory.get(path + query)
        force_authenticate(request, user=self.admin)
        return view.as_view()(request)

    def user_count(self, query=""):
        response = self.get(AdminUsers, "/api/admin/users/", query)
        self.assertEqual(response.status_code, 200)
        return response.data["users"]["count"]

    def test_all_returns_every_user(self):
        # 4 seeded + the admin itself.
        self.assertEqual(self.user_count(), 5)

    def test_each_range_narrows_the_user_list(self):
        # The admin was created "now", so it falls inside every window.
        self.assertEqual(self.user_count("?days=1"), 2)    # 0d + admin
        self.assertEqual(self.user_count("?days=7"), 3)    # 0d, 3d + admin
        self.assertEqual(self.user_count("?days=30"), 4)   # 0d, 3d, 15d + admin
        self.assertEqual(self.user_count("?days=365"), 5)  # all

    def test_range_is_narrower_than_all(self):
        self.assertLess(self.user_count("?days=1"), self.user_count())

    def test_explicit_date_filters_the_user_list(self):
        target = (timezone.now() - timedelta(days=3)).date().isoformat()
        self.assertEqual(self.user_count(f"?date={target}"), 1)

    def test_all_users_stat_tracks_the_filtered_range(self):
        response = self.get(AdminUsers, "/api/admin/users/", "?days=1")
        self.assertEqual(response.data["all_users"], response.data["users"]["count"])


class TransactionRangeFilterTests(TestCase):
    def setUp(self):
        self.factory = APIRequestFactory()
        self.admin = User.objects.create_superuser(
            username="tx-admin", email="tx-admin@example.com", password="pw"
        )
        self.customer = User.objects.create_user(
            username="tx-customer", email="tx-customer@example.com", password="pw"
        )
        wallet, _ = Wallet.objects.get_or_create(user=self.customer)
        now = timezone.now()
        for age in (0, 3, 15, 200):
            transaction = Transaction.objects.create(
                wallet=wallet,
                amount=Decimal("10.00"),
                type=Transaction.Type.DEPOSIT,
                status=Transaction.Status.COMPLETED,
                description=f"seed-{age}",
            )
            Transaction.objects.filter(pk=transaction.pk).update(
                created_at=now - timedelta(days=age)
            )

    def tx_count(self, query=""):
        request = self.factory.get("/api/admin/wallet/transactions/" + query)
        force_authenticate(request, user=self.admin)
        response = TransactionHistoryView.as_view()(request)
        self.assertEqual(response.status_code, 200)
        return response.data["count"]

    def test_all_returns_every_transaction(self):
        self.assertEqual(self.tx_count(), 4)

    def test_each_range_narrows_the_transaction_list(self):
        self.assertEqual(self.tx_count("?days=1"), 1)
        self.assertEqual(self.tx_count("?days=7"), 2)
        self.assertEqual(self.tx_count("?days=30"), 3)
        self.assertEqual(self.tx_count("?days=365"), 4)

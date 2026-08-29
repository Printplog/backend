from django.urls import path
from .views import *
from .distribution_views import (
    DistributionBalanceView,
    DistributionConfigurationView,
    DistributionDashboardView,
    DistributionRetryView,
    DistributionRunView,
)

urlpatterns = [
    path('wallet/', WalletDetailView.as_view(), name='wallet-detail'),
    path('create-payment/', CreateCryptoPaymentView.as_view(), name='create-crypto-payment'),
    path('cancel-payment/', CancelCryptoPaymentView.as_view(), name='cancel-crypto-payment'),
    path("webhook/cpay/", CPayWebhookView.as_view(), name="cpay-webhook"),
    path("webhook/cryptapi/", CryptAPIWebhookView.as_view(), name="cryptapi-webhook"),
    path("admin/cpay-distribution/", DistributionDashboardView.as_view(), name="cpay-distribution-dashboard"),
    path("admin/cpay-distribution/configuration/", DistributionConfigurationView.as_view(), name="cpay-distribution-configuration"),
    path("admin/cpay-distribution/balance/", DistributionBalanceView.as_view(), name="cpay-distribution-balance"),
    path("admin/cpay-distribution/run/", DistributionRunView.as_view(), name="cpay-distribution-run"),
    path("admin/cpay-distribution/batches/<uuid:batch_id>/retry/", DistributionRetryView.as_view(), name="cpay-distribution-retry"),
]

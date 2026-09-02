import base64
import hashlib
import json
import time
from decimal import Decimal
from unittest.mock import patch
from urllib.parse import parse_qs, urlparse

import jwt
from cryptography.hazmat.primitives import padding
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from django.test import SimpleTestCase, TestCase, override_settings
from rest_framework.test import APIClient

from accounts.models import User
from api.models import SiteSettings
from wallet.models import (
    CPayDepositRoute,
    CPayWebhookEvent,
    CryptAPIWebhookEvent,
    RevenueDistributionBatch,
    RevenueDistributionConfig,
    RevenueDistributionPayout,
    RevenueShareRecipient,
    Transaction,
)
from wallet.provider_security import decrypt_cpay_callback, decrypt_payment_secret, encrypt_payment_secret
from wallet.providers import CPayClient
from wallet.tasks import check_revenue_distribution, execute_revenue_distribution


CPAY_ADDRESS = "0x1111111111111111111111111111111111111111"
SECOND_ADDRESS = "0x2222222222222222222222222222222222222222"


def _cryptojs_encrypt_for_test(message: bytes, passphrase: bytes) -> str:
    salt = b"12345678"
    material = b""
    block = b""
    while len(material) < 48:
        block = hashlib.md5(block + passphrase + salt).digest()
        material += block
    padder = padding.PKCS7(algorithms.AES.block_size).padder()
    padded = padder.update(message) + padder.finalize()
    encryptor = Cipher(algorithms.AES(material[:32]), modes.CBC(material[32:48])).encryptor()
    encrypted = encryptor.update(padded) + encryptor.finalize()
    return base64.b64encode(b"Salted__" + salt + encrypted).decode("ascii")


class CPayProviderSecurityTests(SimpleTestCase):
    def test_official_cryptojs_callback_envelope_is_decrypted(self):
        wallet_id = "6a93397f2becfd51021e008d"
        final_salt = b"cpay-callback-final-salt"
        payload = {"orderId": "provider-order", "wallet": {"id": wallet_id}}
        token = jwt.encode(
            {
                "id": wallet_id,
                "salt": _cryptojs_encrypt_for_test(final_salt, wallet_id.encode("utf-8")),
                "exp": int(time.time()) + 60,
            },
            "signature-is-provider-verified-separately",
            algorithm="HS256",
        )
        encrypted_data = _cryptojs_encrypt_for_test(json.dumps(payload).encode("utf-8"), final_salt)

        decoded_wallet_id, decoded_payload = decrypt_cpay_callback(f"Bearer {token}", encrypted_data)

        self.assertEqual(decoded_wallet_id, wallet_id)
        self.assertEqual(decoded_payload, payload)


@override_settings(CPAY_BEP20_USDT_CURRENCY_ID="usdt-bsc-id")
class CPayProviderResponseTests(SimpleTestCase):
    @patch.object(CPayClient, "_authenticate", return_value="wallet-token")
    @patch.object(CPayClient, "_request")
    def test_v2_nested_balance_response_is_supported(self, request, _authenticate):
        request.return_value = {
            "data": {
                "balances": [
                    {
                        "currency": {"id": "usdt-bsc-id", "name": "USDT", "nodeType": "bsc"},
                        "balance": {"value": "125.5", "hold": "20.25"},
                    }
                ]
            }
        }

        self.assertEqual(CPayClient().get_available_usdt_balance(), Decimal("105.25"))

    @patch.object(CPayClient, "_authenticate", return_value="wallet-token")
    @patch.object(CPayClient, "_request")
    def test_transaction_lookup_falls_back_when_cpay_search_misses_withdrawal(
        self,
        request,
        _authenticate,
    ):
        completed = {
            "_id": "cpay-withdrawal-1",
            "type": "Withdrawal",
            "systemStatus": "Done",
            "status": True,
        }
        request.side_effect = [
            {"data": {"entities": []}},
            {"data": {"entities": [completed]}},
        ]

        result = CPayClient().find_transaction("cpay-withdrawal-1")

        self.assertEqual(result, completed)
        self.assertEqual(request.call_count, 2)
        self.assertEqual(
            request.call_args_list[1].kwargs["params"],
            {"page": 1, "limit": 50, "order": "DESC"},
        )

    @patch.object(CPayClient, "_authenticate", return_value="wallet-token")
    @patch.object(CPayClient, "_request")
    def test_transaction_lookup_does_not_accept_a_different_recent_transaction(
        self,
        request,
        _authenticate,
    ):
        request.side_effect = [
            {"data": {"entities": []}},
            {"data": {"entities": [{"_id": "another-transaction"}]}},
        ]

        self.assertIsNone(CPayClient().find_transaction("cpay-withdrawal-1"))


@override_settings(
    CPAY_DEPOSIT_ROUTING_ENABLED=True,
    CPAY_PUBLIC_KEY="public",
    CPAY_PRIVATE_KEY="private",
    CPAY_BEP20_USDT_CURRENCY_ID="usdt-bsc-id",
    CRYPTAPI_CALLBACK_BASE_URL="https://example.test/api/webhook/cryptapi/",
    CRYPTAPI_LEGACY_CALLBACK_SECRET="cryptapi-secret",
    CRYPTAPI_REQUIRE_SIGNATURE=True,
)
class CPayDepositRoutingTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(
            username="depositor",
            email="depositor@example.com",
            password="pw",
        )
        self.client = APIClient()
        self.client.force_authenticate(self.user)

    @patch("wallet.views.send_wallet_update")
    @patch("wallet.views.CryptAPIClient.create_address")
    @patch("wallet.views.CPayClient.create_client_wallet")
    def test_fixed_bridge_returns_cryptapi_address_routed_to_fresh_cpay_wallet(
        self,
        create_cpay_wallet,
        create_cryptapi_address,
        _send_wallet_update,
    ):
        create_cpay_wallet.return_value = {
            "id": "cpay-wallet-1",
            "address": CPAY_ADDRESS,
            "passphrase": "wallet-passphrase",
        }
        create_cryptapi_address.return_value = {
            "status": "success",
            "address_in": SECOND_ADDRESS,
            "address_out": CPAY_ADDRESS,
        }

        response = self.client.post("/api/create-payment/", {}, format="json")

        self.assertEqual(response.status_code, 201)
        self.assertEqual(response.data["payment_address"], SECOND_ADDRESS)
        self.assertEqual(response.data["gateway"], "cryptapi_cpay")
        self.assertEqual(response.data["network"], "BEP20")
        route = CPayDepositRoute.objects.get()
        self.assertEqual(route.cpay_address, CPAY_ADDRESS)
        self.assertEqual(route.cryptapi_address_in, SECOND_ADDRESS)
        self.assertEqual(route.route_type, CPayDepositRoute.RouteType.CRYPTAPI_BRIDGE)
        self.assertEqual(route.client_reference, str(route.transaction.tx_id))
        self.assertEqual(decrypt_payment_secret(route.encrypted_passphrase), "wallet-passphrase")
        self.assertNotIn("wallet-passphrase", route.encrypted_passphrase)
        create_cpay_wallet.assert_called_once_with()
        create_cryptapi_address.assert_called_once()
        call = create_cryptapi_address.call_args.kwargs
        self.assertEqual(call["destination_address"], CPAY_ADDRESS)
        callback_url = call["callback_url"]
        callback_query = parse_qs(urlparse(callback_url).query)
        self.assertEqual(callback_query["payment"], [str(route.transaction.tx_id)])
        self.assertEqual(
            decrypt_payment_secret(route.encrypted_callback_nonce),
            callback_query["nonce"][0],
        )
        self.assertEqual(decrypt_payment_secret(route.cryptapi_callback_url), callback_url)

    @patch("wallet.views.CPayWebhookView._queue_distribution_check")
    @patch("wallet.views.send_wallet_update")
    @patch("wallet.views.decrypt_cpay_callback")
    @patch("wallet.views.CPayClient.find_transaction")
    def test_cpay_callback_rechecks_provider_and_credits_once(
        self,
        find_transaction,
        decrypt_callback,
        _send_wallet_update,
        _queue_distribution,
    ):
        wallet_id = "6a93397f2becfd51021e008d"
        tx = Transaction.objects.create(
            wallet=self.user.wallet,
            type=Transaction.Type.DEPOSIT,
            status=Transaction.Status.PENDING,
            amount=Decimal("0"),
            address=CPAY_ADDRESS,
        )
        route = CPayDepositRoute.objects.create(
            transaction=tx,
            route_type=CPayDepositRoute.RouteType.DIRECT,
            client_reference=str(tx.tx_id),
            cpay_wallet_id=wallet_id,
            cpay_address=CPAY_ADDRESS,
            encrypted_passphrase=encrypt_payment_secret("wallet-passphrase"),
        )
        callback_payload = {
            "orderId": "6a935833ff8f959b56ef7361",
            "typeTransaction": "Replenishment",
            "status": True,
            "systemStatus": "Done",
            "wallet": {"id": wallet_id},
            "incomingTxHash": "incoming-chain-transaction",
        }
        decrypt_callback.return_value = (wallet_id, callback_payload)
        find_transaction.return_value = {
            "_id": callback_payload["orderId"],
            "type": "Replenishment",
            "status": True,
            "systemStatus": "Done",
            "info": {
                "currencyId": "usdt-bsc-id",
                "currency": "USDT",
                "nodeType": "bsc",
                "fromId": wallet_id,
                "incomingTxHash": "incoming-chain-transaction",
                "amount": {"value": "4.9757801937"},
            },
        }

        with self.captureOnCommitCallbacks(execute=True):
            first = self.client.post(
                "/api/webhook/cpay/",
                {"data": "encrypted"},
                format="json",
                HTTP_AUTHORIZATION="Bearer provider-token",
            )
            second = self.client.post(
                "/api/webhook/cpay/",
                {"data": "encrypted"},
                format="json",
                HTTP_AUTHORIZATION="Bearer provider-token",
            )

        self.assertEqual(first.status_code, 200)
        self.assertEqual(second.status_code, 200)
        self.user.wallet.refresh_from_db()
        tx.refresh_from_db()
        route.refresh_from_db()
        self.assertEqual(self.user.wallet.balance, Decimal("4.97"))
        self.assertEqual(tx.amount, Decimal("4.97"))
        self.assertEqual(route.forwarded_amount, Decimal("4.970000"))
        self.assertEqual(CPayWebhookEvent.objects.count(), 1)
        find_transaction.assert_called_once_with(
            callback_payload["orderId"],
            wallet_id=wallet_id,
            passphrase="wallet-passphrase",
        )
        _queue_distribution.assert_called_once()

    @patch("wallet.views.CryptAPIWebhookView._queue_distribution_check")
    @patch("wallet.views.send_wallet_update")
    @patch("wallet.views.verify_cryptapi_signature", return_value=True)
    def test_signed_callback_credits_gross_amount_and_absorbs_fees_once(
        self,
        _verify_signature,
        _send_wallet_update,
        _queue_distribution,
    ):
        tx = Transaction.objects.create(
            wallet=self.user.wallet,
            type=Transaction.Type.DEPOSIT,
            status=Transaction.Status.PENDING,
            amount=Decimal("0"),
            address=SECOND_ADDRESS,
        )
        nonce = "a-long-unguessable-callback-nonce"
        route = CPayDepositRoute.objects.create(
            transaction=tx,
            client_reference=str(tx.tx_id),
            cpay_wallet_id="cpay-wallet-2",
            cpay_address=CPAY_ADDRESS,
            encrypted_passphrase=encrypt_payment_secret("wallet-passphrase"),
            encrypted_callback_nonce=encrypt_payment_secret(nonce),
            cryptapi_callback_url="https://example.test/callback",
            cryptapi_address_in=SECOND_ADDRESS,
        )
        site_settings = SiteSettings.get_settings()
        site_settings.enable_deposit_promo = True
        site_settings.deposit_promo_min_amount = Decimal("30.00")
        site_settings.deposit_promo_percentage = Decimal("100.00")
        site_settings.deposit_promo_max_bonus = Decimal("100.00")
        site_settings.save()
        payload = {
            "uuid": "cryptapi-callback-1",
            "address_in": SECOND_ADDRESS,
            "address_out": CPAY_ADDRESS,
            "txid_in": "incoming-chain-transaction",
            "txid_out": "forwarding-chain-transaction",
            "value_coin": "30.000000",
            "value_forwarded_coin": "29.650000",
            "coin": "bep20_usdt",
            "pending": 0,
            "confirmations": 1,
            "required_confirmations": 1,
        }
        url = f"/api/webhook/cryptapi/?payment={tx.tx_id}&nonce={nonce}"

        with self.captureOnCommitCallbacks(execute=True):
            first = self.client.post(
                url,
                data=json.dumps(payload),
                content_type="application/json",
                HTTP_X_CA_SIGNATURE="signed",
            )
            second = self.client.post(
                url,
                data=json.dumps(payload),
                content_type="application/json",
                HTTP_X_CA_SIGNATURE="signed",
            )

        self.assertEqual(first.status_code, 200)
        self.assertEqual(first.content, b"*ok*")
        self.assertEqual(second.status_code, 200)
        self.user.wallet.refresh_from_db()
        tx.refresh_from_db()
        route.refresh_from_db()
        event = CryptAPIWebhookEvent.objects.get()
        self.assertEqual(self.user.wallet.balance, Decimal("30.00"))
        self.assertEqual(self.user.wallet.bonus_balance, Decimal("30.00"))
        self.assertEqual(tx.amount, Decimal("30.00"))
        self.assertEqual(route.forwarded_amount, Decimal("29.650000"))
        self.assertEqual(event.amount_received, Decimal("30.000000"))
        self.assertEqual(event.amount_forwarded, Decimal("29.650000"))
        self.assertEqual(event.cost_absorbed, Decimal("0.350000"))
        self.assertEqual(CryptAPIWebhookEvent.objects.count(), 1)
        _queue_distribution.assert_called_once()

    @patch("wallet.views.verify_cryptapi_signature", return_value=True)
    def test_confirmed_callback_without_forwarding_hash_is_not_credited(self, _verify_signature):
        tx = Transaction.objects.create(
            wallet=self.user.wallet,
            type=Transaction.Type.DEPOSIT,
            status=Transaction.Status.PENDING,
            amount=Decimal("0"),
            address=SECOND_ADDRESS,
        )
        nonce = "another-long-unguessable-callback-nonce"
        CPayDepositRoute.objects.create(
            transaction=tx,
            route_type=CPayDepositRoute.RouteType.CRYPTAPI_BRIDGE,
            client_reference=str(tx.tx_id),
            cpay_wallet_id="cpay-wallet-without-forwarding-hash",
            cpay_address=CPAY_ADDRESS,
            encrypted_passphrase=encrypt_payment_secret("wallet-passphrase"),
            encrypted_callback_nonce=encrypt_payment_secret(nonce),
            cryptapi_address_in=SECOND_ADDRESS,
        )
        payload = {
            "uuid": "cryptapi-callback-without-forwarding-hash",
            "address_in": SECOND_ADDRESS,
            "address_out": CPAY_ADDRESS,
            "txid_in": "incoming-without-forwarding-hash",
            "value_forwarded_coin": "10.00",
            "coin": "bep20_usdt",
            "pending": 0,
            "confirmations": 1,
            "required_confirmations": 1,
        }

        response = self.client.post(
            f"/api/webhook/cryptapi/?payment={tx.tx_id}&nonce={nonce}",
            data=json.dumps(payload),
            content_type="application/json",
            HTTP_X_CA_SIGNATURE="signed",
        )

        self.assertEqual(response.status_code, 400)
        self.user.wallet.refresh_from_db()
        tx.refresh_from_db()
        self.assertEqual(self.user.wallet.balance, Decimal("0"))
        self.assertEqual(tx.status, Transaction.Status.PENDING)
        self.assertFalse(CryptAPIWebhookEvent.objects.exists())


@override_settings(
    CPAY_PUBLIC_KEY="public",
    CPAY_PRIVATE_KEY="private",
    CPAY_BEP20_USDT_CURRENCY_ID="usdt-bsc-id",
    CPAY_PAYOUT_WALLET_ID="treasury-wallet",
    CPAY_PAYOUT_WALLET_PASSPHRASE="treasury-passphrase",
    CPAY_LIVE_PAYOUTS_ENABLED=True,
    CPAY_MAX_TRANCHES_PER_RUN=10,
)
class RevenueDistributionTaskTests(TestCase):
    def setUp(self):
        self.config = RevenueDistributionConfig.get_config()
        self.config.enabled = True
        self.config.threshold_amount = Decimal("100.00")
        self.config.save()
        RevenueShareRecipient.objects.create(
            name="First",
            email="first@example.com",
            bep20_address=CPAY_ADDRESS,
            percentage=Decimal("60.00"),
        )
        RevenueShareRecipient.objects.create(
            name="Second",
            email="second@example.com",
            bep20_address=SECOND_ADDRESS,
            percentage=Decimal("40.00"),
        )

    @patch("wallet.tasks.execute_revenue_distribution.delay")
    @patch("wallet.tasks.CPayClient.get_available_usdt_balance", return_value=Decimal("250.987654"))
    def test_whole_available_balance_is_split_exactly_once(self, _get_balance, queue_execution):
        with self.captureOnCommitCallbacks(execute=True):
            result = check_revenue_distribution.run()
            duplicate = check_revenue_distribution.run()

        self.assertTrue(result["created"])
        self.assertEqual(duplicate["reason"], "batch_in_progress")
        batch = RevenueDistributionBatch.objects.get()
        self.assertEqual(batch.amount, Decimal("250.000000"))
        self.assertEqual(
            list(batch.payouts.values_list("amount", flat=True)),
            [Decimal("150.000000"), Decimal("100.000000")],
        )
        queue_execution.assert_called_once_with(str(batch.id))

    @patch("wallet.tasks.reconcile_revenue_distributions.apply_async")
    @patch("wallet.tasks.CPayClient.withdraw_usdt", side_effect=["cpay-tx-1", "cpay-tx-2"])
    def test_submissions_use_one_idempotent_transfer_per_recipient(self, withdraw, queue_reconcile):
        batch = RevenueDistributionBatch.objects.create(
            amount=Decimal("100"),
            threshold_amount=Decimal("100"),
            balance_before=Decimal("100"),
        )
        for recipient, amount in zip(RevenueShareRecipient.objects.all(), (Decimal("60"), Decimal("40"))):
            payout = RevenueDistributionPayout.objects.create(
                batch=batch,
                recipient=recipient,
                recipient_name=recipient.name,
                recipient_email=recipient.email,
                bep20_address=recipient.bep20_address,
                percentage=recipient.percentage,
                amount=amount,
                idempotency_key=f"test-{recipient.id}",
            )

        execute_revenue_distribution.run(str(batch.id))
        execute_revenue_distribution.run(str(batch.id))

        self.assertEqual(withdraw.call_count, 2)
        self.assertEqual(
            set(RevenueDistributionPayout.objects.values_list("status", flat=True)),
            {RevenueDistributionPayout.Status.SUBMITTED},
        )
        for call in withdraw.call_args_list:
            self.assertTrue(call.kwargs["idempotency_key"].startswith("test-"))
        queue_reconcile.assert_called()

    @patch.object(CPayClient, "_authenticate", return_value="wallet-token")
    @patch.object(CPayClient, "_request", return_value={"data": {"id": "cpay-tx-1"}})
    def test_withdrawal_amount_is_sent_as_a_decimal_string(self, request, _authenticate):
        CPayClient().withdraw_usdt(
            to=CPAY_ADDRESS,
            amount=Decimal("3.00"),
            idempotency_key="manual-test",
        )

        self.assertEqual(request.call_args.kwargs["json"]["amount"], "3.00")


class RevenueDistributionAdminValidationTests(TestCase):
    def setUp(self):
        self.admin = User.objects.create_superuser(
            username="owner",
            email="owner@example.com",
            password="pw",
        )
        self.client = APIClient()
        self.client.force_authenticate(self.admin)

    def test_enabled_allocation_must_total_one_hundred_before_totp_is_consumed(self):
        response = self.client.post(
            "/api/admin/cpay-distribution/configuration/",
            {
                "enabled": True,
                "threshold_amount": "100",
                "two_factor_code": "123456",
                "recipients": [
                    {
                        "name": "First",
                        "email": "first@example.com",
                        "bep20_address": CPAY_ADDRESS,
                        "percentage": "75",
                    }
                ],
            },
            format="json",
        )
        self.assertEqual(response.status_code, 400)
        self.assertIn("100%", response.data["detail"])

    @override_settings(CPAY_LIVE_PAYOUTS_ENABLED=True)
    @patch("wallet.distribution_views.CPayClient.get_available_usdt_balance", return_value=Decimal("124.87"))
    @patch("wallet.distribution_views._require_totp", return_value=None)
    @patch("wallet.distribution_views.execute_revenue_distribution.delay")
    def test_retry_resizes_unsubmitted_batch_and_rotates_idempotency_key(
        self, queue_execution, _require_totp, _get_balance,
    ):
        batch = RevenueDistributionBatch.objects.create(
            amount=Decimal("100"),
            threshold_amount=Decimal("100"),
            balance_before=Decimal("122"),
            status=RevenueDistributionBatch.Status.FAILED,
        )
        payout = RevenueDistributionPayout.objects.create(
            batch=batch,
            recipient_name="First",
            recipient_email="first@example.com",
            bep20_address=CPAY_ADDRESS,
            percentage=Decimal("100"),
            amount=Decimal("100"),
            idempotency_key="rejected-request-key",
            status=RevenueDistributionPayout.Status.FAILED,
        )

        with self.captureOnCommitCallbacks(execute=True):
            response = self.client.post(
                f"/api/admin/cpay-distribution/batches/{batch.id}/retry/",
                {"two_factor_code": "123456"},
                format="json",
            )

        self.assertEqual(response.status_code, 202)
        batch.refresh_from_db()
        payout.refresh_from_db()
        self.assertEqual(batch.amount, Decimal("124.000000"))
        self.assertEqual(batch.balance_before, Decimal("124.870000"))
        self.assertEqual(payout.amount, Decimal("124.000000"))
        self.assertEqual(payout.status, RevenueDistributionPayout.Status.PENDING)
        self.assertNotEqual(payout.idempotency_key, "rejected-request-key")
        queue_execution.assert_called_once_with(str(batch.id))

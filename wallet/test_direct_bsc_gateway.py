import hashlib
import hmac
import json
from decimal import Decimal
from unittest.mock import Mock, patch

from django.test import TestCase, override_settings
from rest_framework.test import APIClient

from accounts.models import User
from wallet.blockchain import PreparedTokenTransfer, VerifiedTokenTransfer
from wallet.deposits import sweep_direct_bsc_deposit
from wallet.models import (
    CPayDepositRoute,
    DirectBSCDepositAddress,
    OnChainDeposit,
    RevenueDistributionBatch,
    RevenueDistributionPayout,
    Transaction,
)
from wallet.tasks import execute_revenue_distribution


GATEWAY_ADDRESS = "0x1111111111111111111111111111111111111111"
SENDER_ADDRESS = "0x2222222222222222222222222222222222222222"
USDT_ADDRESS = "0x55d398326f99059fF775485246999027B3197955"
TX_HASH = "0x" + ("a" * 64)


@override_settings(
    PAYMENT_GATEWAY_PROVIDER="bsc",
    BSC_RPC_URL="https://rpc.example.test",
    BSC_CHAIN_ID=56,
    BSC_USDT_CONTRACT_ADDRESS=USDT_ADDRESS,
    BSC_GATEWAY_WALLET_ADDRESS=GATEWAY_ADDRESS,
    BSC_REQUIRED_CONFIRMATIONS=3,
)
class DirectBSCGatewayTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(
            username="direct-depositor",
            email="direct@example.com",
            password="pw",
        )
        self.client = APIClient()
        self.client.force_authenticate(self.user)
        latest_block = patch("wallet.deposits.BSCWalletClient.latest_block_number", return_value=100)
        latest_block.start()
        self.addCleanup(latest_block.stop)

    @patch("wallet.views.send_wallet_update")
    def test_create_payment_returns_unique_address_without_cpay(self, _wallet_update):
        response = self.client.post("/api/create-payment/", {}, format="json")

        self.assertEqual(response.status_code, 201)
        self.assertEqual(response.data["gateway"], "direct_bsc")
        self.assertEqual(response.data["required_confirmations"], 3)
        self.assertFalse(CPayDepositRoute.objects.exists())
        transaction = Transaction.objects.get()
        route = DirectBSCDepositAddress.objects.get(transaction=transaction)
        self.assertEqual(response.data["payment_address"], route.address)
        self.assertEqual(transaction.address, route.address)
        self.assertNotEqual(route.address.lower(), GATEWAY_ADDRESS.lower())
        self.assertNotIn("0x", route.encrypted_private_key[:2].lower())

    @patch("wallet.deposits._after_credit")
    @patch("wallet.blockchain.BSCWalletClient.verify_usdt_deposit")
    def test_confirmed_hash_credits_full_transfer_exactly_once(self, verify, _after_credit):
        payment = self.client.post("/api/create-payment/", {}, format="json")
        assigned_address = payment.data["payment_address"]
        verify.return_value = self._transfer(confirmed=True, confirmations=3, amount="30.129")

        first = self.client.post(
            "/api/confirm-payment/",
            {"id": payment.data["transaction_id"], "transaction_hash": TX_HASH},
            format="json",
        )
        second = self.client.post(
            "/api/confirm-payment/",
            {"id": payment.data["transaction_id"], "transaction_hash": TX_HASH},
            format="json",
        )

        self.assertEqual(first.status_code, 200)
        self.assertEqual(second.status_code, 200)
        self.user.wallet.refresh_from_db()
        transaction = Transaction.objects.get(pk=payment.data["transaction_id"])
        receipt = OnChainDeposit.objects.get(transaction=transaction)
        self.assertEqual(self.user.wallet.balance, Decimal("30.12"))
        self.assertEqual(transaction.amount, Decimal("30.12"))
        self.assertEqual(transaction.status, Transaction.Status.COMPLETED)
        self.assertEqual(receipt.amount, Decimal("30.129"))
        self.assertEqual(receipt.credited_amount, Decimal("30.12"))
        self.assertEqual(receipt.status, OnChainDeposit.Status.CONFIRMED)
        self.assertEqual(OnChainDeposit.objects.count(), 1)
        verify.assert_called_once_with(TX_HASH, recipient_address=assigned_address)

    @patch("wallet.tasks.reconcile_onchain_deposit.apply_async")
    @patch("wallet.blockchain.BSCWalletClient.verify_usdt_deposit")
    def test_unconfirmed_transfer_is_reserved_and_not_credited(self, verify, queue_recheck):
        payment = self.client.post("/api/create-payment/", {}, format="json")
        verify.return_value = self._transfer(confirmed=False, confirmations=1, amount="10")

        with self.captureOnCommitCallbacks(execute=True):
            response = self.client.post(
                "/api/confirm-payment/",
                {"id": payment.data["transaction_id"], "transaction_hash": TX_HASH},
                format="json",
            )

        self.assertEqual(response.status_code, 202)
        self.user.wallet.refresh_from_db()
        receipt = OnChainDeposit.objects.get()
        self.assertEqual(self.user.wallet.balance, Decimal("0"))
        self.assertEqual(receipt.status, OnChainDeposit.Status.PENDING)
        self.assertEqual(receipt.confirmations, 1)
        queue_recheck.assert_called_once_with(args=[str(receipt.id)], countdown=30)

    @patch("wallet.deposits._after_credit")
    @patch("wallet.deposits.BSCWalletClient")
    def test_browser_status_poll_discovers_and_credits_payment_automatically(
        self,
        blockchain_client,
        _after_credit,
    ):
        blockchain_client.return_value.latest_block_number.return_value = 100
        payment = self.client.post("/api/create-payment/", {}, format="json")
        route = DirectBSCDepositAddress.objects.get(transaction_id=payment.data["transaction_id"])
        chain = blockchain_client.return_value
        chain.latest_block_number.return_value = 101
        chain.find_usdt_deposit_hashes.return_value = [TX_HASH]
        chain.verify_usdt_deposit.return_value = VerifiedTokenTransfer(
            transaction_hash=TX_HASH,
            sender_address=SENDER_ADDRESS,
            recipient_address=route.address,
            amount=Decimal("12.345"),
            block_number=99,
            confirmations=3,
            confirmed=True,
        )

        response = self.client.get(f"/api/payment-status/{payment.data['transaction_id']}/")

        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.data["detected"])
        self.assertTrue(response.data["credited"])
        self.assertEqual(response.data["amount"], "12.345000000000000000")
        self.user.wallet.refresh_from_db()
        self.assertEqual(self.user.wallet.balance, Decimal("12.34"))
        chain.find_usdt_deposit_hashes.assert_called_once_with(
            recipient_address=route.address,
            from_block=99,
            to_block=101,
        )

        second = self.client.get(f"/api/payment-status/{payment.data['transaction_id']}/")
        self.assertEqual(second.status_code, 200)
        self.assertEqual(OnChainDeposit.objects.count(), 1)
        self.user.wallet.refresh_from_db()
        self.assertEqual(self.user.wallet.balance, Decimal("12.34"))

    @patch("wallet.deposits._after_credit")
    @patch("wallet.blockchain.BSCWalletClient.verify_usdt_deposit")
    def test_same_hash_cannot_credit_two_users(self, verify, _after_credit):
        first_payment = self.client.post("/api/create-payment/", {}, format="json")
        verify.return_value = self._transfer(confirmed=True, confirmations=3, amount="5")
        first = self.client.post(
            "/api/confirm-payment/",
            {"id": first_payment.data["transaction_id"], "transaction_hash": TX_HASH},
            format="json",
        )
        self.assertEqual(first.status_code, 200)

        other = User.objects.create_user(username="other", email="other@example.com", password="pw")
        self.client.force_authenticate(other)
        second_payment = self.client.post("/api/create-payment/", {}, format="json")
        replay = self.client.post(
            "/api/confirm-payment/",
            {"id": second_payment.data["transaction_id"], "transaction_hash": TX_HASH},
            format="json",
        )

        self.assertEqual(replay.status_code, 409)
        other.wallet.refresh_from_db()
        self.assertEqual(other.wallet.balance, Decimal("0"))

    @override_settings(BSC_LIVE_SWEEPS_ENABLED=True)
    @patch("wallet.deposits._after_credit")
    @patch("wallet.blockchain.BSCWalletClient.verify_usdt_deposit")
    def test_confirmed_deposit_is_funded_and_swept_idempotently(self, verify, _after_credit):
        payment = self.client.post("/api/create-payment/", {}, format="json")
        verify.return_value = self._transfer(confirmed=True, confirmations=3, amount="9.85")
        response = self.client.post(
            "/api/confirm-payment/",
            {"id": payment.data["transaction_id"], "transaction_hash": TX_HASH},
            format="json",
        )
        self.assertEqual(response.status_code, 200)
        route = DirectBSCDepositAddress.objects.get(transaction_id=payment.data["transaction_id"])

        chain = Mock()
        sweep = PreparedTokenTransfer(
            transaction_hash="0x" + ("b" * 64),
            signed_transaction="sweep-bytes",
            gas_cost_wei=100,
            amount=Decimal("9.85"),
        )
        funding = PreparedTokenTransfer(
            transaction_hash="0x" + ("c" * 64),
            signed_transaction="funding-bytes",
        )
        chain.prepare_usdt_sweep.return_value = sweep
        chain.get_native_balance_wei.return_value = 0
        chain.prepare_native_transfer.return_value = funding
        chain.broadcast_prepared_transfer.side_effect = lambda prepared: prepared.transaction_hash

        first = sweep_direct_bsc_deposit(route_id=route.id, client=chain)
        self.assertEqual(first["status"], "gas_submitted")
        route.refresh_from_db()
        self.assertEqual(route.sweep_status, DirectBSCDepositAddress.SweepStatus.FUNDING)
        self.assertEqual(route.gas_funding_transaction_hash, funding.transaction_hash)
        chain.prepare_native_transfer.assert_called_once_with(to=route.address, amount_wei=125)

        chain.get_transfer_status.return_value.status = "completed"
        chain.get_native_balance_wei.return_value = 125
        second = sweep_direct_bsc_deposit(route_id=route.id, client=chain)
        self.assertEqual(second["status"], "sweep_submitted")
        route.refresh_from_db()
        self.assertEqual(route.sweep_status, DirectBSCDepositAddress.SweepStatus.SWEEPING)
        self.assertEqual(route.sweep_transaction_hash, sweep.transaction_hash)

        completed_status = Mock(status="completed", confirmations=3)
        chain.get_transfer_status.return_value = completed_status
        third = sweep_direct_bsc_deposit(route_id=route.id, client=chain)
        self.assertEqual(third["status"], "completed")
        route.refresh_from_db()
        self.assertEqual(route.sweep_status, DirectBSCDepositAddress.SweepStatus.COMPLETED)
        self.assertEqual(route.swept_amount, Decimal("9.85"))

        broadcasts = chain.broadcast_prepared_transfer.call_count
        fourth = sweep_direct_bsc_deposit(route_id=route.id, client=chain)
        self.assertEqual(fourth["status"], "completed")
        self.assertEqual(chain.broadcast_prepared_transfer.call_count, broadcasts)

    @override_settings(
        ALCHEMY_WEBHOOK_ID="wh_test",
        ALCHEMY_WEBHOOK_SIGNING_KEY="test-signing-key",
    )
    @patch("wallet.deposits._after_credit")
    @patch("wallet.blockchain.BSCWalletClient.verify_usdt_deposit")
    def test_signed_alchemy_webhook_claims_tracked_usdt_transfer(self, verify, _after_credit):
        payment = self.client.post("/api/create-payment/", {}, format="json")
        route = DirectBSCDepositAddress.objects.get(transaction_id=payment.data["transaction_id"])
        verify.return_value = VerifiedTokenTransfer(
            transaction_hash=TX_HASH,
            sender_address=SENDER_ADDRESS,
            recipient_address=route.address,
            amount=Decimal("7.50"),
            block_number=100,
            confirmations=3,
            confirmed=True,
        )
        payload = {
            "webhookId": "wh_test",
            "id": "whevt_test",
            "type": "ADDRESS_ACTIVITY",
            "event": {
                "network": "BNB_MAINNET",
                "activity": [{
                    "category": "token",
                    "hash": TX_HASH,
                    "toAddress": route.address,
                    "rawContract": {"address": USDT_ADDRESS},
                }],
            },
        }
        body = json.dumps(payload, separators=(",", ":")).encode()
        signature = hmac.new(b"test-signing-key", body, hashlib.sha256).hexdigest()

        response = self.client.generic(
            "POST",
            "/api/webhook/alchemy/",
            body,
            content_type="application/json",
            HTTP_X_ALCHEMY_SIGNATURE=signature,
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["accepted"], 1)
        self.user.wallet.refresh_from_db()
        self.assertEqual(self.user.wallet.balance, Decimal("7.50"))

    @override_settings(
        ALCHEMY_WEBHOOK_ID="wh_test",
        ALCHEMY_WEBHOOK_SIGNING_KEY="test-signing-key",
    )
    def test_alchemy_webhook_rejects_invalid_signature(self):
        response = self.client.post(
            "/api/webhook/alchemy/",
            {"webhookId": "wh_test", "type": "ADDRESS_ACTIVITY", "event": {"activity": []}},
            format="json",
            HTTP_X_ALCHEMY_SIGNATURE="not-valid",
        )
        self.assertEqual(response.status_code, 401)

    @patch("wallet.tasks.reconcile_revenue_distributions.apply_async")
    @patch("wallet.tasks.get_payout_provider")
    def test_retry_rebroadcasts_same_signed_payout_instead_of_signing_again(
        self,
        get_provider,
        _queue_reconcile,
    ):
        provider = Mock()
        provider.broadcast_prepared_transfer.return_value = TX_HASH
        get_provider.return_value = provider
        batch = RevenueDistributionBatch.objects.create(
            amount=Decimal("3"),
            threshold_amount=Decimal("1"),
            balance_before=Decimal("3"),
            status=RevenueDistributionBatch.Status.FAILED,
        )
        payout = RevenueDistributionPayout.objects.create(
            batch=batch,
            recipient_name="Recipient",
            recipient_email="recipient@example.com",
            bep20_address=SENDER_ADDRESS,
            percentage=Decimal("100"),
            amount=Decimal("3"),
            idempotency_key="direct-retry",
            provider_transaction_id=TX_HASH,
            transaction_hash=TX_HASH,
            signed_transaction="deadbeef",
            status=RevenueDistributionPayout.Status.FAILED,
        )

        result = execute_revenue_distribution.run(str(batch.id))

        self.assertEqual(result["status"], "submitted")
        provider.prepare_usdt_transfer.assert_not_called()
        prepared = provider.broadcast_prepared_transfer.call_args.args[0]
        self.assertEqual(prepared.transaction_hash, TX_HASH)
        self.assertEqual(prepared.signed_transaction, "deadbeef")
        payout.refresh_from_db()
        self.assertEqual(payout.status, RevenueDistributionPayout.Status.SUBMITTED)

    @staticmethod
    def _transfer(*, confirmed, confirmations, amount):
        return VerifiedTokenTransfer(
            transaction_hash=TX_HASH,
            sender_address=SENDER_ADDRESS,
            recipient_address=GATEWAY_ADDRESS,
            amount=Decimal(amount),
            block_number=100,
            confirmations=confirmations,
            confirmed=confirmed,
        )

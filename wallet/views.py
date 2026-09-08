import hmac
import logging
import secrets
import uuid
from decimal import Decimal, InvalidOperation, ROUND_DOWN
from datetime import timedelta

from django.conf import settings as django_settings
from django.core.exceptions import ObjectDoesNotExist
from django.db import IntegrityError, transaction
from django.http import HttpResponse
from rest_framework.views import APIView
from rest_framework.response import Response
from rest_framework import status
from rest_framework.permissions import IsAuthenticated
from django.utils import timezone

from wallet.models import (
    CPayDepositRoute,
    CPayWebhookEvent,
    CryptAPIWebhookEvent,
    DirectBSCDepositAddress,
    OnChainDeposit,
    Transaction,
)
from asgiref.sync import async_to_sync
from channels.layers import get_channel_layer
from wallet.serializers import WalletSerializer
from api.models import SiteSettings
from wallet.provider_security import (
    PaymentSecretError,
    decrypt_cpay_callback,
    decrypt_payment_secret,
    encrypt_payment_secret,
)
from wallet.providers import (
    CPayClient,
    CryptAPIClient,
    PaymentProviderError,
    direct_bsc_enabled,
    verify_cryptapi_signature,
)


logger = logging.getLogger(__name__)


def send_wallet_update(user, new_payment):
    channel_layer = get_channel_layer()
    wallet = user.wallet
    data = WalletSerializer(wallet).data
    async_to_sync(channel_layer.group_send)( # type: ignore
        f"user_wallet_{user.id}",
        {
            "type": "wallet.updated",
            "data": data,
            "new_payment": new_payment,
        },
    )

    # BROADCAST TO ADMIN ANALYTICS
    async_to_sync(channel_layer.group_send)(
        "admin_activity",
        {
            "type": "activity_event",
            "data": {
                "type": "new_sale",
                "sale": {
                    "amount": float(wallet.balance) if hasattr(wallet, 'balance') else 0, # Note: this is balance, but we want the event
                    "type": "payment" if new_payment else "update"
                }
            }
        }
    )

  

class WalletDetailView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request):
        wallet = request.user.wallet
        serializer = WalletSerializer(wallet)
        return Response(serializer.data)

 
class CreateCryptoPaymentView(APIView):
    permission_classes = [IsAuthenticated]

    def post(self, request):
        if not request.user.is_authenticated:
            return Response({"detail": "Authentication required."}, status=status.HTTP_401_UNAUTHORIZED)
            
        # Prevent multiple pending transactions - firmly scoped to this user ID
        user_id = request.user.id
        has_pending = Transaction.objects.filter(
            wallet__user_id=user_id, 
            status=Transaction.Status.PENDING
        ).exists()

        if has_pending:
            return Response(
                {"detail": "You have a pending transaction. Please wait until it is completed before adding more funds."},
                status=status.HTTP_400_BAD_REQUEST
            )

        tx_id = uuid.uuid4()
        tx = Transaction.objects.create(
            wallet=request.user.wallet,
            type=Transaction.Type.DEPOSIT,
            status=Transaction.Status.PENDING,
            amount=Decimal("0.00"),
            tx_id=tx_id,
            description="Wallet Funding",
            gateway="direct_bsc" if direct_bsc_enabled() else "cryptapi_cpay",
        )

        try:
            if direct_bsc_enabled():
                from wallet.blockchain import BSCWalletClient
                from wallet.deposits import create_direct_bsc_deposit_address

                if not BSCWalletClient.deposit_configured():
                    raise PaymentProviderError("The direct BNB Chain gateway is not configured.")
                route = create_direct_bsc_deposit_address(payment=tx)
                payment_address = route.address
                gateway = "direct_bsc"
            else:
                if not django_settings.CPAY_DEPOSIT_ROUTING_ENABLED:
                    raise PaymentProviderError("CryptAPI to CPay deposit routing is disabled.")
                if not CPayClient.deposit_configured():
                    raise PaymentProviderError("CPay deposits are not configured.")
                if not django_settings.CRYPTAPI_CALLBACK_BASE_URL:
                    raise PaymentProviderError("CryptAPI callbacks are not configured.")

                cpay_wallet = CPayClient().create_client_wallet()
                callback_nonce = secrets.token_urlsafe(32)
                separator = "&" if "?" in django_settings.CRYPTAPI_CALLBACK_BASE_URL else "?"
                callback_url = (
                    f"{django_settings.CRYPTAPI_CALLBACK_BASE_URL}{separator}"
                    f"payment={tx_id}&nonce={callback_nonce}"
                )
                route = CPayDepositRoute.objects.create(
                    transaction=tx,
                    route_type=CPayDepositRoute.RouteType.CRYPTAPI_BRIDGE,
                    client_reference=str(tx_id),
                    cpay_wallet_id=cpay_wallet["id"],
                    cpay_address=cpay_wallet["address"],
                    encrypted_passphrase=encrypt_payment_secret(cpay_wallet["passphrase"]),
                    encrypted_callback_nonce=encrypt_payment_secret(callback_nonce),
                    cryptapi_callback_url=encrypt_payment_secret(callback_url),
                )
                data = CryptAPIClient().create_address(
                    destination_address=cpay_wallet["address"],
                    callback_url=callback_url,
                )
                payment_address = data["address_in"]
                gateway = "cryptapi_cpay"
                route.cryptapi_address_in = payment_address
                route.save(update_fields=["cryptapi_address_in", "updated_at"])
        except PaymentProviderError:
            logger.exception("Payment route creation failed for transaction %s", tx_id)
            tx.status = Transaction.Status.FAILED
            tx.description = "Deposit route creation failed"
            tx.save(update_fields=["status", "description"])
            if hasattr(tx, "cpay_route"):
                tx.cpay_route.status = CPayDepositRoute.Status.FAILED
                tx.cpay_route.save(update_fields=["status", "updated_at"])
            return Response(
                {"detail": "The secure deposit address could not be created. Please try again."},
                status=status.HTTP_502_BAD_GATEWAY,
            )

        tx.address = payment_address
        tx.save(update_fields=["address"])
        
        send_wallet_update(request.user, False)

        return Response({
            "transaction_id": str(tx.id),
            "ticker": django_settings.CRYPTAPI_TICKER,
            "payment_address": payment_address,
            "tx_id": tx.tx_id,
            "network": "BEP20",
            "gateway": gateway,
            "required_confirmations": (
                django_settings.BSC_REQUIRED_CONFIRMATIONS if direct_bsc_enabled() else 1
            ),
        }, status=status.HTTP_201_CREATED)


class ConfirmCryptoPaymentView(APIView):
    permission_classes = [IsAuthenticated]
    throttle_scope = "wallet_write"

    def post(self, request):
        if not direct_bsc_enabled():
            return Response(
                {"detail": "Direct blockchain payment verification is not enabled."},
                status=status.HTTP_409_CONFLICT,
            )
        transaction_id = request.data.get("id")
        transaction_hash = str(request.data.get("transaction_hash") or "").strip()
        if not transaction_id or not transaction_hash:
            return Response(
                {"detail": "Deposit request and transaction hash are required."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        from wallet.deposits import DepositClaimError, verify_and_credit_onchain_deposit

        try:
            result = verify_and_credit_onchain_deposit(
                transaction_id=transaction_id,
                user_id=request.user.id,
                transaction_hash=transaction_hash,
            )
        except DepositClaimError as exc:
            return Response({"detail": str(exc)}, status=status.HTTP_409_CONFLICT)
        except PaymentProviderError as exc:
            return Response({"detail": str(exc)}, status=status.HTTP_400_BAD_REQUEST)

        response_status = status.HTTP_200_OK if result.confirmed else status.HTTP_202_ACCEPTED
        return Response(
            {
                "transaction_id": result.transaction_id,
                "transaction_hash": result.transaction_hash,
                "amount": str(result.amount),
                "confirmations": result.confirmations,
                "required_confirmations": result.required_confirmations,
                "confirmed": result.confirmed,
                "credited": result.credited,
            },
            status=response_status,
        )


class CryptoPaymentStatusView(APIView):
    """Poll and scan one direct payment without requiring a customer-supplied hash."""

    permission_classes = [IsAuthenticated]
    throttle_scope = "wallet_write"

    def get(self, request, payment_id):
        try:
            payment = Transaction.objects.select_related("wallet__user").get(
                pk=payment_id,
                wallet__user_id=request.user.id,
                type=Transaction.Type.DEPOSIT,
            )
        except Transaction.DoesNotExist:
            return Response({"detail": "Deposit request not found."}, status=status.HTTP_404_NOT_FOUND)

        if payment.gateway == "direct_bsc" and payment.status == Transaction.Status.PENDING:
            try:
                route = payment.direct_bsc_route
                from wallet.deposits import scan_direct_bsc_deposit

                scan_direct_bsc_deposit(route_id=route.id)
            except ObjectDoesNotExist:
                route = None
            except PaymentProviderError as exc:
                logger.warning("Automatic BSC deposit scan failed for %s: %s", payment.id, exc)
                return Response({"detail": str(exc)}, status=status.HTTP_503_SERVICE_UNAVAILABLE)

        payment.refresh_from_db()
        receipt = OnChainDeposit.objects.filter(transaction=payment).first()
        return Response(
            {
                "transaction_id": str(payment.id),
                "status": payment.status,
                "detected": receipt is not None,
                "transaction_hash": receipt.transaction_hash if receipt else "",
                "amount": str(receipt.amount if receipt else payment.amount),
                "confirmations": receipt.confirmations if receipt else 0,
                "required_confirmations": django_settings.BSC_REQUIRED_CONFIRMATIONS,
                "credited": payment.status == Transaction.Status.COMPLETED,
                "automatic_monitoring": hasattr(payment, "direct_bsc_route"),
            }
        )


class AlchemyWebhookView(APIView):
    """Accept signed BNB Address Activity or Custom notifications from Alchemy."""

    authentication_classes = []
    permission_classes = []

    def post(self, request):
        from wallet.alchemy import valid_webhook_signature, webhook_configured

        if not webhook_configured():
            return Response({"detail": "Alchemy webhook is not configured."}, status=status.HTTP_503_SERVICE_UNAVAILABLE)
        raw_body = request._request.body
        signature = request.headers.get("X-Alchemy-Signature", "")
        if not valid_webhook_signature(raw_body, signature):
            return Response({"detail": "Invalid webhook signature."}, status=status.HTTP_401_UNAUTHORIZED)

        payload = request.data if isinstance(request.data, dict) else {}
        if payload.get("webhookId") != django_settings.ALCHEMY_WEBHOOK_ID:
            return Response({"detail": "Unknown webhook."}, status=status.HTTP_401_UNAUTHORIZED)
        payload_type = payload.get("type")
        if payload_type == "ADDRESS_ACTIVITY":
            activities = (payload.get("event") or {}).get("activity") or []
        elif payload_type == "GRAPHQL":
            from wallet.alchemy import custom_webhook_activities

            activities = custom_webhook_activities(payload)
        else:
            return Response({"status": "OK", "ignored": True})

        accepted = 0
        seen_hashes = set()
        for activity in activities:
            if not isinstance(activity, dict):
                continue
            raw_contract = activity.get("rawContract") or {}
            contract_address = str(
                raw_contract.get("address") or activity.get("contractAddress") or ""
            ).lower()
            recipient = str(activity.get("toAddress") or "").lower()
            transaction_hash = str(activity.get("hash") or "").lower()
            if (
                activity.get("category") not in {"token", "erc20"}
                or contract_address != django_settings.BSC_USDT_CONTRACT_ADDRESS.lower()
                or transaction_hash in seen_hashes
            ):
                continue
            seen_hashes.add(transaction_hash)
            try:
                route = DirectBSCDepositAddress.objects.select_related(
                    "transaction__wallet__user"
                ).get(address__iexact=recipient)
            except DirectBSCDepositAddress.DoesNotExist:
                continue

            from wallet.deposits import DepositClaimError, verify_and_credit_onchain_deposit

            try:
                verify_and_credit_onchain_deposit(
                    transaction_id=route.transaction_id,
                    user_id=route.transaction.wallet.user_id,
                    transaction_hash=transaction_hash,
                )
                accepted += 1
            except DepositClaimError as exc:
                logger.warning("Rejected Alchemy deposit event %s: %s", transaction_hash, exc)
            except PaymentProviderError as exc:
                logger.warning("Alchemy deposit verification unavailable for %s: %s", transaction_hash, exc)
                return Response({"status": "RETRY"}, status=status.HTTP_503_SERVICE_UNAVAILABLE)

        return Response({"status": "OK", "accepted": accepted})


class CancelCryptoPaymentView(APIView):
    permission_classes = [IsAuthenticated]

    def post(self, request):
        tx_id = request.data.get("id")
        if not tx_id:
            return Response({"detail": "Transaction ID required."}, status=status.HTTP_400_BAD_REQUEST)
        try:
            tx = Transaction.objects.get(id=tx_id, wallet=request.user.wallet, status=Transaction.Status.PENDING)
        except Transaction.DoesNotExist:
            return Response({"detail": "Pending transaction not found."}, status=status.HTTP_404_NOT_FOUND)
        if OnChainDeposit.objects.filter(transaction=tx).exists():
            return Response(
                {"detail": "A blockchain payment has already been detected and is awaiting confirmation."},
                status=status.HTTP_409_CONFLICT,
            )
        tx.status = Transaction.Status.FAILED
        tx.description = "Deposit cancelled"
        tx.save(update_fields=["status", "description"])
        send_wallet_update(request.user, False)
        return Response({"detail": "Transaction cancelled successfully."}, status=status.HTTP_200_OK)


class CPayWebhookView(APIView):
    """Receive encrypted CPay events and credit only provider-verified deposits."""

    authentication_classes = []
    permission_classes = []
    throttle_scope = "wallet_write"

    def post(self, request):
        try:
            wallet_id, payload = decrypt_cpay_callback(
                request.headers.get("Authorization", ""),
                request.data.get("data") if isinstance(request.data, dict) else "",
            )
        except PaymentSecretError:
            logger.warning("Rejected an invalid CPay callback envelope")
            return Response({"status": "ERROR"}, status=status.HTTP_401_UNAUTHORIZED)

        try:
            route = CPayDepositRoute.objects.select_related("transaction__wallet__user").get(
                cpay_wallet_id=wallet_id,
                route_type=CPayDepositRoute.RouteType.DIRECT,
            )
        except CPayDepositRoute.DoesNotExist:
            # The account callback also receives payout and old-wallet events.
            return Response({"status": "OK", "ignored": True})

        provider_transaction_id = str(payload.get("orderId") or "").strip()
        event_type = str(payload.get("typeTransaction") or "").strip()
        callback_status = str(payload.get("systemStatus") or "").strip()
        callback_wallet_id = str((payload.get("wallet") or {}).get("id") or "").strip()
        if not provider_transaction_id or event_type != "Replenishment" or callback_wallet_id != wallet_id:
            return Response({"status": "OK", "ignored": True})
        if callback_status != "Done" or payload.get("status") is not True:
            return Response({"status": "OK", "pending": True})
        if CPayWebhookEvent.objects.filter(provider_transaction_id=provider_transaction_id).exists():
            return Response({"status": "OK"})

        try:
            entity = CPayClient().find_transaction(
                provider_transaction_id,
                wallet_id=route.cpay_wallet_id,
                passphrase=decrypt_payment_secret(route.encrypted_passphrase),
            )
        except (PaymentProviderError, PaymentSecretError):
            logger.exception("Could not verify CPay transaction %s", provider_transaction_id)
            return Response({"status": "RETRY"}, status=status.HTTP_503_SERVICE_UNAVAILABLE)
        if not entity:
            return Response({"status": "RETRY"}, status=status.HTTP_503_SERVICE_UNAVAILABLE)

        info = entity.get("info") or {}
        entity_id = str(entity.get("_id") or entity.get("id") or "")
        from_wallet_id = str(info.get("fromId") or "")
        if (
            entity_id != provider_transaction_id
            or entity.get("type") != "Replenishment"
            or entity.get("systemStatus") != "Done"
            or entity.get("status") is not True
            or str(info.get("currencyId") or "") != django_settings.CPAY_BEP20_USDT_CURRENCY_ID
            or str(info.get("currency") or "").upper() != "USDT"
            or str(info.get("nodeType") or "").lower() != "bsc"
            or (from_wallet_id and from_wallet_id != route.cpay_wallet_id)
        ):
            logger.warning("CPay callback did not match the stored deposit route")
            return Response({"status": "ERROR"}, status=status.HTTP_400_BAD_REQUEST)

        callback_incoming_hash = str(payload.get("incomingTxHash") or "").strip()
        provider_incoming_hash = str(info.get("incomingTxHash") or "").strip()
        if callback_incoming_hash and provider_incoming_hash and callback_incoming_hash != provider_incoming_hash:
            return Response({"status": "ERROR"}, status=status.HTTP_400_BAD_REQUEST)
        tx_hash = provider_incoming_hash or callback_incoming_hash
        if not tx_hash:
            hashes = info.get("hashs") or []
            tx_hash = str(hashes[-1]) if hashes else str(payload.get("hash") or "").strip()

        try:
            raw_amount = (info.get("amount") or {}).get("value")
            amount = Decimal(str(raw_amount)).quantize(Decimal("0.01"), rounding=ROUND_DOWN)
        except (InvalidOperation, TypeError, ValueError):
            return Response({"status": "ERROR"}, status=status.HTTP_400_BAD_REQUEST)
        if amount <= 0:
            return Response({"status": "ERROR"}, status=status.HTTP_400_BAD_REQUEST)

        try:
            credited_tx, wallet = self._credit_route(
                route_id=route.id,
                provider_transaction_id=provider_transaction_id,
                tx_hash=tx_hash,
                amount=amount,
            )
        except IntegrityError:
            return Response({"status": "OK"})
        if credited_tx is not None:
            send_wallet_update(wallet.user, True)
            transaction.on_commit(self._queue_distribution_check)
        return Response({"status": "OK"})

    @staticmethod
    @transaction.atomic
    def _credit_route(*, route_id, provider_transaction_id, tx_hash, amount):
        if CPayWebhookEvent.objects.filter(provider_transaction_id=provider_transaction_id).exists():
            return None, None

        route = CPayDepositRoute.objects.select_related(
            "transaction__wallet__user"
        ).select_for_update(of=("self",)).get(pk=route_id, route_type=CPayDepositRoute.RouteType.DIRECT)
        wallet = route.transaction.wallet
        if route.transaction.status == Transaction.Status.PENDING:
            credited_tx = route.transaction
            credited_tx.status = Transaction.Status.COMPLETED
            credited_tx.tx_hash = tx_hash
            credited_tx.amount = amount
            credited_tx.save(update_fields=["status", "tx_hash", "amount"])
        elif route.transaction.status == Transaction.Status.FAILED:
            credited_tx = Transaction.objects.create(
                wallet=wallet,
                type=Transaction.Type.DEPOSIT,
                status=Transaction.Status.COMPLETED,
                amount=amount,
                tx_hash=tx_hash,
                address=route.cpay_address,
                description="Late CPay wallet funding",
            )
        else:
            logger.error("Refusing to credit an already-completed CPay route without an event receipt")
            return None, None

        wallet.credit(amount, create_transaction=False)
        CPayWebhookEvent.objects.create(
            route=route,
            provider_transaction_id=provider_transaction_id,
            tx_hash=tx_hash,
            amount=amount,
            credited_transaction=credited_tx,
        )
        route.cpay_transaction_id = provider_transaction_id
        route.cpay_tx_hash = tx_hash
        route.forwarded_amount = route.forwarded_amount + amount
        route.status = CPayDepositRoute.Status.FORWARDED
        route.save(update_fields=[
            "cpay_transaction_id", "cpay_tx_hash", "forwarded_amount", "status", "updated_at",
        ])
        _apply_deposit_rewards(wallet, amount, credited_tx)
        return credited_tx, wallet

    @staticmethod
    def _queue_distribution_check():
        from wallet.tasks import check_revenue_distribution

        check_revenue_distribution.apply_async(countdown=60)



class CryptAPIWebhookView(APIView):
    authentication_classes = []
    permission_classes = []

    def post(self, request):
        raw_body = request.body
        if django_settings.CRYPTAPI_REQUIRE_SIGNATURE:
            signature = request.headers.get("x-ca-signature", "")
            if not verify_cryptapi_signature(raw_body, signature):
                logger.warning("Rejected CryptAPI callback with an invalid signature")
                return Response({"detail": "Invalid webhook signature."}, status=status.HTTP_401_UNAUTHORIZED)

        payment_id = str(request.query_params.get("payment") or "").strip()
        nonce = str(request.query_params.get("nonce") or "").strip()
        if not payment_id or not nonce:
            return self._legacy_callback(request)

        try:
            route = CPayDepositRoute.objects.select_related("transaction__wallet__user").get(
                transaction__tx_id=payment_id,
                route_type=CPayDepositRoute.RouteType.CRYPTAPI_BRIDGE,
            )
        except CPayDepositRoute.DoesNotExist:
            return Response({"detail": "Payment route not found."}, status=status.HTTP_404_NOT_FOUND)

        expected_nonce = decrypt_payment_secret(route.encrypted_callback_nonce)
        if not hmac.compare_digest(nonce, expected_nonce):
            return Response({"detail": "Invalid callback nonce."}, status=status.HTTP_403_FORBIDDEN)

        payload = request.data
        callback_id = str(payload.get("uuid") or "").strip()
        address_in = str(payload.get("address_in") or "").strip()
        address_out = str(payload.get("address_out") or "").strip()
        txid_in = str(payload.get("txid_in") or "").strip()
        txid_out = str(payload.get("txid_out") or "").strip()
        coin = str(payload.get("coin") or "").strip().lower()
        expected_coin = django_settings.CRYPTAPI_TICKER.replace("/", "_").lower()

        try:
            pending = int(payload.get("pending", 1))
            confirmations = int(payload.get("confirmations", 0))
            required_confirmations = int(payload.get("required_confirmations", 1))
        except (TypeError, ValueError):
            return Response({"detail": "Invalid confirmation values."}, status=status.HTTP_400_BAD_REQUEST)

        if pending != 0 or confirmations < required_confirmations:
            return HttpResponse("*ok*", content_type="text/plain")

        if not all([
            callback_id,
            address_in,
            address_out,
            txid_in,
            txid_out,
            payload.get("value_coin"),
            payload.get("value_forwarded_coin"),
        ]):
            return Response({"detail": "Missing required callback fields."}, status=status.HTTP_400_BAD_REQUEST)
        if coin and coin != expected_coin:
            return Response({"detail": "Unexpected payment currency."}, status=status.HTTP_400_BAD_REQUEST)
        if address_in != route.cryptapi_address_in or address_out.lower() != route.cpay_address.lower():
            return Response({"detail": "Payment route does not match."}, status=status.HTTP_400_BAD_REQUEST)

        try:
            amount_received = Decimal(str(payload["value_coin"])).quantize(
                Decimal("0.000001"), rounding=ROUND_DOWN
            )
            amount_forwarded = Decimal(str(payload["value_forwarded_coin"])).quantize(
                Decimal("0.000001"), rounding=ROUND_DOWN
            )
        except (InvalidOperation, TypeError, ValueError):
            return Response({"detail": "Invalid amount format."}, status=status.HTTP_400_BAD_REQUEST)
        if not amount_received.is_finite() or not amount_forwarded.is_finite():
            return Response({"detail": "Invalid amount format."}, status=status.HTTP_400_BAD_REQUEST)
        credited_amount = amount_received.quantize(Decimal("0.01"), rounding=ROUND_DOWN)
        if credited_amount <= 0 or amount_forwarded <= 0:
            return Response({"detail": "Payment amount is too small to credit."}, status=status.HTTP_400_BAD_REQUEST)
        if amount_forwarded > amount_received:
            return Response({"detail": "Forwarded amount exceeds received amount."}, status=status.HTTP_400_BAD_REQUEST)
        cost_absorbed = amount_received - amount_forwarded

        try:
            credited_tx, wallet = self._credit_route(
                route_id=route.id,
                callback_id=callback_id,
                txid_in=txid_in,
                txid_out=txid_out,
                credited_amount=credited_amount,
                amount_received=amount_received,
                amount_forwarded=amount_forwarded,
                cost_absorbed=cost_absorbed,
            )
        except IntegrityError:
            return HttpResponse("*ok*", content_type="text/plain")

        if credited_tx is not None:
            send_wallet_update(wallet.user, True)
            transaction.on_commit(self._queue_distribution_check)
        return HttpResponse("*ok*", content_type="text/plain")

    @staticmethod
    @transaction.atomic
    def _credit_route(
        *,
        route_id,
        callback_id,
        txid_in,
        txid_out,
        credited_amount,
        amount_received,
        amount_forwarded,
        cost_absorbed,
    ):
        if CryptAPIWebhookEvent.objects.filter(callback_id=callback_id).exists():
            return None, None

        route = CPayDepositRoute.objects.select_related(
            "transaction__wallet__user"
        ).select_for_update(of=("self",)).get(pk=route_id, route_type=CPayDepositRoute.RouteType.CRYPTAPI_BRIDGE)
        wallet = route.transaction.wallet
        if route.transaction.status == Transaction.Status.PENDING:
            credited_tx = route.transaction
            credited_tx.status = Transaction.Status.COMPLETED
            credited_tx.tx_hash = txid_in
            credited_tx.amount = credited_amount
            credited_tx.save(update_fields=["status", "tx_hash", "amount"])
        else:
            credited_tx = Transaction.objects.create(
                wallet=wallet,
                type=Transaction.Type.DEPOSIT,
                status=Transaction.Status.COMPLETED,
                amount=credited_amount,
                tx_hash=txid_in,
                address=route.cryptapi_address_in,
                description="Additional wallet funding",
            )

        wallet.credit(credited_amount, create_transaction=False)
        CryptAPIWebhookEvent.objects.create(
            route=route,
            callback_id=callback_id,
            txid_in=txid_in,
            txid_out=txid_out,
            amount_received=amount_received,
            amount_forwarded=amount_forwarded,
            cost_absorbed=cost_absorbed,
            credited_transaction=credited_tx,
        )
        route.cryptapi_callback_id = callback_id
        route.cryptapi_txid_in = txid_in
        route.cryptapi_txid_out = txid_out
        route.forwarded_amount = route.forwarded_amount + amount_forwarded
        route.status = CPayDepositRoute.Status.FORWARDED
        route.save(update_fields=[
            "cryptapi_callback_id", "cryptapi_txid_in", "cryptapi_txid_out",
            "forwarded_amount", "status", "updated_at",
        ])
        _apply_deposit_rewards(wallet, credited_amount, credited_tx)
        return credited_tx, wallet

    @staticmethod
    def _queue_distribution_check():
        from wallet.tasks import check_revenue_distribution

        check_revenue_distribution.apply_async(countdown=60)

    def _legacy_callback(self, request):
        """Keep pre-CPay pending addresses serviceable during the rollout."""
        secret = str(request.query_params.get("secret") or "")
        tx_id = str(request.query_params.get("uuid") or "")
        configured = django_settings.CRYPTAPI_LEGACY_CALLBACK_SECRET
        if not configured or not hmac.compare_digest(secret, configured) or not tx_id:
            return Response({"detail": "Invalid callback route."}, status=status.HTTP_403_FORBIDDEN)
        payload = request.data
        try:
            pending = int(payload.get("pending", 1))
            confirmations = int(payload.get("confirmations", 0))
            required = int(payload.get("required_confirmations", 1))
        except (TypeError, ValueError):
            return Response({"detail": "Invalid confirmation values."}, status=status.HTTP_400_BAD_REQUEST)
        if pending != 0 or confirmations < required:
            return HttpResponse("*ok*", content_type="text/plain")
        try:
            amount = Decimal(str(payload.get("value_coin"))).quantize(
                Decimal("0.01"), rounding=ROUND_DOWN
            )
        except (InvalidOperation, TypeError, ValueError):
            return Response({"detail": "Invalid amount format."}, status=status.HTTP_400_BAD_REQUEST)
        if not amount.is_finite():
            return Response({"detail": "Invalid amount format."}, status=status.HTTP_400_BAD_REQUEST)
        if amount <= 0:
            return Response({"detail": "Payment amount is too small to credit."}, status=status.HTTP_400_BAD_REQUEST)

        with transaction.atomic():
            try:
                tx = Transaction.objects.select_related("wallet__user").select_for_update(of=("self",)).get(
                    tx_id=tx_id,
                    type=Transaction.Type.DEPOSIT,
                )
            except Transaction.DoesNotExist:
                return Response({"detail": "Payment not found."}, status=status.HTTP_404_NOT_FOUND)
            if tx.status == Transaction.Status.COMPLETED:
                return HttpResponse("*ok*", content_type="text/plain")
            if str(payload.get("address_in") or "") != tx.address:
                return Response({"detail": "Payment address does not match."}, status=status.HTTP_400_BAD_REQUEST)
            tx.status = Transaction.Status.COMPLETED
            tx.amount = amount
            tx.tx_hash = str(payload.get("txid_in") or "")
            tx.save(update_fields=["status", "amount", "tx_hash"])
            tx.wallet.credit(amount, create_transaction=False)
            _apply_deposit_rewards(tx.wallet, amount, tx)
        send_wallet_update(tx.wallet.user, True)
        return HttpResponse("*ok*", content_type="text/plain")


def _apply_deposit_rewards(wallet, credited_amount, credited_tx):
    site_settings = SiteSettings.get_settings()
    if site_settings.enable_deposit_promo and credited_amount >= site_settings.deposit_promo_min_amount:
        raw_bonus = credited_amount * site_settings.deposit_promo_percentage / Decimal("100")
        bonus_amount = min(raw_bonus, site_settings.deposit_promo_max_bonus).quantize(Decimal("0.01"))
        if bonus_amount > 0:
            days = site_settings.deposit_promo_expiry_days
            expires_at = (timezone.now() + timedelta(days=days)) if days else None
            wallet.credit_bonus(
                bonus_amount,
                expires_at=expires_at,
                source_transaction=credited_tx,
                percentage=site_settings.deposit_promo_percentage,
            )

    user = wallet.user
    if site_settings.enable_referrals and user.referred_by:
        bonus_amount = (credited_amount * site_settings.referral_percentage) / Decimal("100.00")
        if bonus_amount > 0:
            user.referred_by.wallet.credit_referral(bonus_amount)
            user.wallet.credit_referral(bonus_amount)
            from api.models import Referral

            Referral.objects.create(
                referrer=user.referred_by,
                referred_user=user,
                is_rewarded=True,
                reward_amount=bonus_amount,
            )

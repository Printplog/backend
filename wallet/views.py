import requests
import uuid
from decimal import Decimal
from datetime import timedelta

from rest_framework.views import APIView
from rest_framework.response import Response
from rest_framework import status
from django.urls import reverse
from django.utils import timezone

from wallet.models import Transaction
from asgiref.sync import async_to_sync
from channels.layers import get_channel_layer
from wallet.serializers import WalletSerializer
from api.models import SiteSettings


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
    def get(self, request):
        wallet = request.user.wallet
        serializer = WalletSerializer(wallet)
        return Response(serializer.data)

 
class CreateCryptoPaymentView(APIView):
    TICKER = "bep20/usdt"
    CALLBACK_SECRET = "your_callback_secret_here"

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

        wallet = request.user.wallet
        # Fetch dynamic receiving address from SiteSettings
        settings_obj = SiteSettings.get_settings()
        receiving_address = settings_obj.crypto_address or "0x8482a1d4716736bf3b71736fafac9e8cd679fae8"

        # Generate a unique UUID per transaction
        tx_id = uuid.uuid4()

        # Build a unique callback URL per request
        callback_url = (
            "https://api.sharptoolz.com/api/webhook/cryptapi/"
            f"?secret={self.CALLBACK_SECRET}&uuid={tx_id}"
        )

        cryptapi_url = f"https://api.cryptapi.io/{self.TICKER}/create/"
        params = {
            "callback": callback_url,
            "address": receiving_address,
            "confirmations": "1",
            "pending": "0",
            "post": "1",
            "json": "1",
            "priority": "default",
        }

        try: 
            res = requests.get(cryptapi_url, params=params, timeout=10)
            res.raise_for_status()
            data = res.json()
        except Exception as e:
            return Response({"detail": "CryptAPI error", "error": str(e)}, status=status.HTTP_502_BAD_GATEWAY)

        if "address_in" not in data:
            return Response({"detail": "Invalid response from CryptAPI"}, status=status.HTTP_502_BAD_GATEWAY)

        wallet = request.user.wallet
        tx = Transaction.objects.create(
            wallet=wallet,
            type=Transaction.Type.DEPOSIT,
            status=Transaction.Status.PENDING,
            amount=Decimal("0.00"),  # Amount is initially zero
            address=data["address_in"],
            tx_id=tx_id,  # ← Save UUID
            description="Wallet Funding"
        )
        
        send_wallet_update(request.user, False)

        return Response({
            "transaction_id": str(tx.id),
            "ticker": self.TICKER,
            "payment_address": data["address_in"],
            "tx_id": tx.tx_id,  # ← Include tx_id in the response
        }, status=status.HTTP_201_CREATED)


class CancelCryptoPaymentView(APIView):
    def post(self, request):
        tx_id = request.data.get("id")
        if not tx_id:
            return Response({"detail": "Transaction ID required."}, status=status.HTTP_400_BAD_REQUEST)
        try:
            tx = Transaction.objects.get(id=tx_id, wallet=request.user.wallet, status=Transaction.Status.PENDING)
        except Transaction.DoesNotExist:
            return Response({"detail": "Pending transaction not found."}, status=status.HTTP_404_NOT_FOUND)
        tx.delete()
        send_wallet_update(request.user, False)
        return Response({"detail": "Transaction cancelled successfully."}, status=status.HTTP_200_OK)



class CryptAPIWebhookView(APIView):
    authentication_classes = []
    permission_classes = []

    CALLBACK_SECRET = "your_callback_secret_here"

    def post(self, request):
        secret = request.query_params.get("secret")
        uuid_ = request.query_params.get("uuid")  # ← Get UUID from query
        address = request.data.get("address_in")
        txid_in = request.data.get("txid_in")
        value_forwarded_coin = request.data.get("value_forwarded_coin")
        value_coin = request.data.get("value_coin")
        confirmations = int(request.data.get("confirmations", 0))
        required_confirmations = int(request.data.get("required_confirmations", 1))
        pending = int(request.data.get("pending", 1))

        if secret != self.CALLBACK_SECRET or not uuid_:
            return Response({"detail": "Invalid secret or UUID."}, status=status.HTTP_403_FORBIDDEN)

        if not all([address, txid_in, value_forwarded_coin]):
            return Response({"detail": "Missing required fields."}, status=status.HTTP_400_BAD_REQUEST)

        if pending != 0 or confirmations < required_confirmations:
            return Response({"detail": "Awaiting confirmation."}, status=status.HTTP_202_ACCEPTED)

        try:
            tx = Transaction.objects.select_related('wallet').get(
                tx_id=uuid_,
                status=Transaction.Status.PENDING,
                type=Transaction.Type.DEPOSIT
            )
        except Transaction.DoesNotExist:
            return Response({"detail": "Transaction not found or already processed."}, status=status.HTTP_404_NOT_FOUND)

        if tx.tx_hash:
            return Response({"detail": "Transaction already confirmed."}, status=status.HTTP_200_OK)

        try:
            amount_decimal = Decimal(value_forwarded_coin)
            # SMART ROUNDING: If fractional part >= 0.9, round up to the nearest whole number
            # Examples: 6.90 -> 7, 6.99 -> 7, 6.89 -> 6.89
            if amount_decimal % 1 >= Decimal('0.9'):
                amount_decimal = amount_decimal.quantize(Decimal('1'), rounding='ROUND_CEILING')
            credited_amount = amount_decimal
        except Exception:
            return Response({"detail": "Invalid amount format."}, status=status.HTTP_400_BAD_REQUEST)

        tx.status = Transaction.Status.COMPLETED
        tx.tx_hash = txid_in
        tx.amount = credited_amount # Save the final credited amount (rounded) to the transaction
        tx.save()
        
        # Credit the wallet WITHOUT creating a duplicate transaction record
        tx.wallet.credit(credited_amount, create_transaction=False)
        send_wallet_update(tx.wallet.user, True)

        settings = SiteSettings.get_settings()

        # ---- Deposit Promo Bonus ----
        if settings.enable_deposit_promo and credited_amount >= settings.deposit_promo_min_amount:
            raw_bonus = credited_amount * settings.deposit_promo_percentage / Decimal("100")
            bonus_amount = min(raw_bonus, settings.deposit_promo_max_bonus).quantize(Decimal("0.01"))
            if bonus_amount > 0:
                days = settings.deposit_promo_expiry_days
                expires_at = (timezone.now() + timedelta(days=days)) if days else None
                tx.wallet.credit_bonus(
                    bonus_amount,
                    expires_at=expires_at,
                    source_transaction=tx,
                    percentage=settings.deposit_promo_percentage,
                )
                send_wallet_update(tx.wallet.user, True)

        # Referral Reward Logic
        user = tx.wallet.user
        if settings.enable_referrals and user.referred_by:
            # Mutual Reward Calculation: (deposit * percentage / 100)
            bonus_amount = (credited_amount * settings.referral_percentage) / Decimal('100.00')
            
            if bonus_amount > 0:
                # Credit Referrer
                user.referred_by.wallet.credit_referral(bonus_amount)
                send_wallet_update(user.referred_by, True)

                # Credit Invitee (the depositor)
                user.wallet.credit_referral(bonus_amount)
                send_wallet_update(user, True)

                # Log the referral (one-to-many now, as it's recurring)
                from api.models import Referral
                Referral.objects.create(
                    referrer=user.referred_by,
                    referred_user=user,
                    is_rewarded=True,
                    reward_amount=bonus_amount
                )

        return Response({"detail": "Wallet credited successfully."}, status=status.HTTP_200_OK)
   
    
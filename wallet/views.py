import requests
import uuid
from decimal import Decimal

from rest_framework.views import APIView
from rest_framework.response import Response
from rest_framework import status
from django.urls import reverse

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
  

class WalletDetailView(APIView):
    def get(self, request):
        wallet = request.user.wallet
        serializer = WalletSerializer(wallet)
        return Response(serializer.data)

 
class CreateCryptoPaymentView(APIView):
    TICKER = "bep20/usdt"
    CALLBACK_SECRET = "your_callback_secret_here"

    def post(self, request):
        wallet = request.user.wallet
        
        # Prevent multiple pending transactions
        if Transaction.objects.filter(wallet=wallet, status=Transaction.Status.PENDING).exists():
            return Response(
                {"detail": "You have a pending transaction. Please wait until it is completed before adding more funds."},
                status=status.HTTP_400_BAD_REQUEST
            )

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
            credited_amount = Decimal(value_forwarded_coin)
        except:
            return Response({"detail": "Invalid amount format."}, status=status.HTTP_400_BAD_REQUEST)

        tx.status = Transaction.Status.COMPLETED
        tx.tx_hash = txid_in
        tx.amount = value_coin
        tx.save()
        print("done")
        tx.wallet.credit(credited_amount)
        send_wallet_update(tx.wallet.user, True)
        print("hey")

        return Response({"detail": "Wallet credited successfully."}, status=status.HTTP_200_OK)
   
    
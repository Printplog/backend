from rest_framework import serializers
from wallet.models import Wallet, Transaction


class TransactionSerializer(serializers.ModelSerializer):
    class Meta:
        model = Transaction
        fields = [
            'id', 'tx_id', 'type', 'amount', 'status',
            'description', 'tx_hash', 'address', 'created_at'
        ]


class WalletSerializer(serializers.ModelSerializer):
    transactions = TransactionSerializer(many=True, read_only=True)
    bonus_expires_at = serializers.SerializerMethodField()

    class Meta:
        model = Wallet
        fields = ['id', 'balance', 'bonus_balance', 'bonus_expires_at', 'transactions']

    def get_bonus_expires_at(self, obj):
        from wallet.models import DepositBonus
        nxt = (
            obj.bonuses.filter(status=DepositBonus.Status.ACTIVE, expires_at__isnull=False)
            .order_by("expires_at")
            .values_list("expires_at", flat=True)
            .first()
        )
        return nxt.isoformat() if nxt else None

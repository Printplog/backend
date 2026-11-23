from rest_framework import serializers
from .models import FernwayProfile, FernwayAccount, FernwayCard, FernwayTransaction


class FernwayTransactionSerializer(serializers.ModelSerializer):
    account_id = serializers.UUIDField(source='account.id', read_only=True)

    class Meta:
        model = FernwayTransaction
        fields = [
            'id',
            'account_id',
            'direction',
            'amount',
            'fee',
            'counterparty',
            'counterparty_detail',
            'bank_name',
            'recipient_account_number',
            'recipient_routing_number',
            'narration',
            'category',
            'channel',
            'status',
            'reference',
            'occurred_at',
            'created_at',
        ]


class FernwayCardSerializer(serializers.ModelSerializer):
    class Meta:
        model = FernwayCard
        fields = [
            'id',
            'label',
            'kind',
            'brand',
            'card_number',
            'last4',
            'expiry',
            'cvv',
            'pin',
            'holder_name',
            'monthly_limit',
            'is_frozen',
            'online_payments',
            'created_at',
        ]


class FernwayAccountSerializer(serializers.ModelSerializer):
    transactions = FernwayTransactionSerializer(many=True, read_only=True)

    class Meta:
        model = FernwayAccount
        fields = [
            'id',
            'account_type',
            'name',
            'account_number',
            'routing_number',
            'balance',
            'apy_bps',
            'transactions',
            'created_at',
        ]


class FernwayProfileSerializer(serializers.ModelSerializer):
    accounts = FernwayAccountSerializer(many=True, read_only=True)
    cards = FernwayCardSerializer(many=True, read_only=True)
    recent_transactions = serializers.SerializerMethodField()
    pending_transfers = serializers.SerializerMethodField()
    pending_transfers_count = serializers.SerializerMethodField()
    total_balance = serializers.SerializerMethodField()
    tool_price = serializers.SerializerMethodField()
    monthly_price = serializers.SerializerMethodField()
    wallet_balance = serializers.SerializerMethodField()
    subscription_status = serializers.SerializerMethodField()
    is_live = serializers.SerializerMethodField()
    days_remaining = serializers.SerializerMethodField()
    needs_renewal_reminder = serializers.SerializerMethodField()
    owner_email = serializers.SerializerMethodField()
    owner_email_verified = serializers.SerializerMethodField()

    class Meta:
        model = FernwayProfile
        fields = [
            'id',
            'first_name',
            'last_name',
            'email',
            'email_verified',
            'email_verified_at',
            'owner_email',
            'owner_email_verified',
            'phone',
            'address',
            'password',
            'transaction_pin',
            'theme',
            'auth_token',
            'is_created',
            'is_test',
            'is_live',
            'subscription_status',
            'subscription_started_at',
            'subscription_expires_at',
            'days_remaining',
            'needs_renewal_reminder',
            'tool_price',
            'monthly_price',
            'wallet_balance',
            'accounts',
            'cards',
            'recent_transactions',
            'pending_transfers',
            'pending_transfers_count',
            'total_balance',
            'created_at',
            'updated_at',
        ]

    def get_recent_transactions(self, obj):
        txs = FernwayTransaction.objects.filter(account__profile=obj).order_by('-occurred_at')[:30]
        return FernwayTransactionSerializer(txs, many=True).data

    def get_pending_transfers(self, obj):
        txs = FernwayTransaction.objects.filter(
            account__profile=obj,
            direction='debit',
            status='pending',
        ).order_by('-occurred_at')
        return FernwayTransactionSerializer(txs, many=True).data

    def get_pending_transfers_count(self, obj):
        return FernwayTransaction.objects.filter(
            account__profile=obj,
            direction='debit',
            status='pending',
        ).count()

    def get_total_balance(self, obj):
        return sum(account.balance for account in obj.accounts.all())

    def get_tool_price(self, obj):
        from api.models import Tool
        tool = Tool.objects.filter(slug='fernway').first()
        return float(tool.price) if tool and tool.price else 15.00

    def get_monthly_price(self, obj):
        return self.get_tool_price(obj)

    def get_wallet_balance(self, obj):
        from wallet.models import Wallet
        wallet = Wallet.objects.filter(user=obj.user).first()
        return float(wallet.spendable_balance) if wallet else 0.0

    def get_subscription_status(self, obj):
        return obj.effective_subscription_status

    def get_is_live(self, obj):
        return obj.is_live

    def get_owner_email(self, obj):
        return getattr(obj.user, 'email', '') or ''

    def get_owner_email_verified(self, obj):
        user = getattr(obj, 'user', None)
        if not user:
            return False
        return bool(getattr(user, 'is_superuser', False) or getattr(user, 'email_verified', False))

    def get_days_remaining(self, obj):
        return obj.days_remaining

    def get_needs_renewal_reminder(self, obj):
        return obj.needs_renewal_reminder


class FernwayAdminProfileSerializer(FernwayProfileSerializer):
    owner = serializers.SerializerMethodField()

    class Meta(FernwayProfileSerializer.Meta):
        fields = FernwayProfileSerializer.Meta.fields + ['owner']

    def get_owner(self, obj):
        if not obj.user:
            return None
        return {
            'id': obj.user_id,
            'username': getattr(obj.user, 'username', '') or '',
            'email': getattr(obj.user, 'email', '') or '',
        }

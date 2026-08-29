from django.contrib import admin
from .models import *
# Register your models here.


@admin.register(Wallet)
class WalletAdmin(admin.ModelAdmin):
    list_display = ('user', 'balance', 'referral_balance')
    search_fields = ('user__username', 'user__email')

@admin.register(Transaction)
class TransactionAdmin(admin.ModelAdmin):
    list_display = ('id', 'wallet', 'type', 'amount', 'status', 'created_at')
    list_filter = ('type', 'status', 'created_at')
    search_fields = ('wallet__user__username', 'tx_hash', 'tx_id', 'description')

@admin.register(WithdrawalRequest)
class WithdrawalRequestAdmin(admin.ModelAdmin):
    list_display = ('user', 'amount', 'status', 'created_at')
    list_filter = ('status', 'created_at')
    search_fields = ('user__username', 'user__email', 'usdt_address')


@admin.register(CPayDepositRoute)
class CPayDepositRouteAdmin(admin.ModelAdmin):
    list_display = ('transaction', 'route_type', 'cpay_address', 'status', 'forwarded_amount', 'created_at')
    list_filter = ('route_type', 'status', 'created_at')
    search_fields = ('transaction__tx_id', 'cpay_wallet_id', 'cpay_address', 'cryptapi_txid_in', 'cpay_transaction_id')
    readonly_fields = ('encrypted_passphrase', 'encrypted_callback_nonce')


@admin.register(CryptAPIWebhookEvent)
class CryptAPIWebhookEventAdmin(admin.ModelAdmin):
    list_display = ('callback_id', 'route', 'amount_forwarded', 'created_at')
    search_fields = ('callback_id', 'txid_in', 'txid_out', 'route__cpay_address')
    readonly_fields = ('route', 'callback_id', 'txid_in', 'txid_out', 'amount_forwarded', 'credited_transaction')


@admin.register(CPayWebhookEvent)
class CPayWebhookEventAdmin(admin.ModelAdmin):
    list_display = ('provider_transaction_id', 'route', 'amount', 'created_at')
    search_fields = ('provider_transaction_id', 'tx_hash', 'route__cpay_address')
    readonly_fields = ('route', 'provider_transaction_id', 'tx_hash', 'amount', 'credited_transaction')


@admin.register(RevenueDistributionConfig)
class RevenueDistributionConfigAdmin(admin.ModelAdmin):
    list_display = ('enabled', 'threshold_amount', 'last_available_balance', 'last_balance_checked_at')


@admin.register(RevenueShareRecipient)
class RevenueShareRecipientAdmin(admin.ModelAdmin):
    list_display = ('name', 'email', 'bep20_address', 'percentage', 'is_active')
    list_filter = ('is_active',)
    search_fields = ('name', 'email', 'bep20_address')


class RevenueDistributionPayoutInline(admin.TabularInline):
    model = RevenueDistributionPayout
    extra = 0
    readonly_fields = (
        'recipient_name', 'recipient_email', 'bep20_address', 'percentage', 'amount',
        'idempotency_key', 'provider_transaction_id', 'transaction_hash', 'status',
    )


@admin.register(RevenueDistributionBatch)
class RevenueDistributionBatchAdmin(admin.ModelAdmin):
    list_display = ('id', 'amount', 'status', 'balance_before', 'created_at', 'completed_at')
    list_filter = ('status', 'created_at')
    inlines = [RevenueDistributionPayoutInline]

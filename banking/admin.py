from django.contrib import admin
from .models import FernwayProfile, FernwayAccount, FernwayCard, FernwayTransaction


@admin.register(FernwayProfile)
class FernwayProfileAdmin(admin.ModelAdmin):
    list_display = (
        'email',
        'first_name',
        'last_name',
        'user',
        'subscription_status',
        'is_test',
        'subscription_expires_at',
        'created_at',
    )
    list_filter = ('subscription_status', 'is_test', 'is_created', 'theme')
    search_fields = ('email', 'first_name', 'last_name', 'user__username', 'user__email')
    readonly_fields = ('id', 'auth_token', 'created_at', 'updated_at')


@admin.register(FernwayAccount)
class FernwayAccountAdmin(admin.ModelAdmin):
    list_display = ('name', 'account_type', 'profile', 'balance', 'account_number')
    list_filter = ('account_type',)
    search_fields = ('profile__email', 'account_number')


@admin.register(FernwayCard)
class FernwayCardAdmin(admin.ModelAdmin):
    list_display = ('holder_name', 'brand', 'last4', 'is_frozen', 'monthly_limit', 'profile')
    list_filter = ('brand', 'kind', 'is_frozen')


@admin.register(FernwayTransaction)
class FernwayTransactionAdmin(admin.ModelAdmin):
    list_display = ('counterparty', 'direction', 'amount', 'category', 'status', 'occurred_at')
    list_filter = ('direction', 'category', 'status')
    search_fields = ('counterparty', 'narration', 'reference')

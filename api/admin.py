from django.contrib import admin
from django.contrib import messages
from .models import (
    Tool, Template, PurchasedTemplate, SiteSettings, Tutorial, Font, TransformVariable,
    AiChatSession, AiChatMessage, Referral, ApiEntitlement, ApiCustomerSettings,
    ApiKey, EmbedSession, ApiIdempotencyRecord, DocumentRenderJob,
)

@admin.register(Template)
class TemplateAdmin(admin.ModelAdmin):
    list_display = ('name', 'id', 'hot', 'is_active', 'created_at')
    list_filter = ('hot', 'is_active', 'tool')
    search_fields = ('name', 'id')

    def delete_model(self, request, obj):
        # Count purchased templates before deletion
        purchased_count = obj.purchases.count()
        super().delete_model(request, obj)
        
        if purchased_count > 0:
            messages.warning( 
                request, 
                f"Template '{obj.name}' deleted successfully. {purchased_count} purchased template(s) are now orphaned but preserved."
            )
        else:
            messages.success(request, f"Template '{obj.name}' deleted successfully.")
    
    def delete_queryset(self, request, queryset):
        total_purchased = 0
        for obj in queryset:
            total_purchased += obj.purchases.count()
        
        super().delete_queryset(request, queryset)
        
        if total_purchased > 0:
            messages.warning(
                request,
                f"Templates deleted successfully. {total_purchased} purchased template(s) are now orphaned but preserved."
            )
        else:
            messages.success(request, f"Templates deleted successfully.")

@admin.register(Tool)
class ToolAdmin(admin.ModelAdmin):
    list_display = ('name', 'id', 'price', 'is_active', 'created_at')
    list_filter = ('is_active',)
    search_fields = ('name', 'id')

@admin.register(PurchasedTemplate)
class PurchasedTemplateAdmin(admin.ModelAdmin):
    list_display = ('name', 'buyer', 'template', 'created_at')
    list_filter = ('created_at', 'buyer')
    search_fields = ('name', 'buyer__username', 'buyer__email', 'tracking_id')

@admin.register(SiteSettings)
class SiteSettingsAdmin(admin.ModelAdmin):
    list_display = ('id', 'updated_at', 'maintenance_mode')


@admin.register(ApiEntitlement)
class ApiEntitlementAdmin(admin.ModelAdmin):
    list_display = ("user", "status", "paid_amount", "activated_at")
    list_filter = ("status", "activated_at")
    search_fields = ("user__username", "user__email")
    readonly_fields = ("paid_amount", "payment_transaction", "activated_at", "updated_at")


@admin.register(ApiCustomerSettings)
class ApiCustomerSettingsAdmin(admin.ModelAdmin):
    list_display = ("user", "updated_at")
    search_fields = ("user__username", "user__email")


@admin.register(ApiKey)
class ApiKeyAdmin(admin.ModelAdmin):
    list_display = ("name", "user", "prefix", "last_used_at", "expires_at", "revoked_at")
    list_filter = ("revoked_at", "created_at")
    search_fields = ("name", "prefix", "user__username", "user__email")
    readonly_fields = ("secret_hash", "prefix", "last_used_at", "created_at")

    def has_add_permission(self, request):
        return False


@admin.register(EmbedSession)
class EmbedSessionAdmin(admin.ModelAdmin):
    list_display = ("id", "user", "template", "operation", "status", "mode", "expires_at", "created_at")
    list_filter = ("operation", "status", "mode", "created_at")
    search_fields = ("external_user_id", "user__username", "user__email")
    readonly_fields = ("token_hash", "created_at", "updated_at", "completed_at")

    def has_add_permission(self, request):
        return False


@admin.register(ApiIdempotencyRecord)
class ApiIdempotencyRecordAdmin(admin.ModelAdmin):
    list_display = ("operation", "api_key", "key", "document", "render_job", "created_at")
    readonly_fields = ("api_key", "operation", "key", "request_hash", "document", "render_job", "created_at")

    def has_add_permission(self, request):
        return False


@admin.register(DocumentRenderJob)
class DocumentRenderJobAdmin(admin.ModelAdmin):
    list_display = ("id", "user", "document", "format", "status", "output_size", "expires_at")
    list_filter = ("format", "status", "created_at")
    search_fields = ("id", "document__id", "user__username", "user__email")
    readonly_fields = (
        "user", "document", "requested_by_key", "format", "status", "output_file",
        "output_size", "error_code", "expires_at", "started_at", "completed_at",
        "created_at", "updated_at",
    )

    def has_add_permission(self, request):
        return False

@admin.register(Tutorial)
class TutorialAdmin(admin.ModelAdmin):
    list_display = ('title', 'template', 'tool', 'is_featured', 'url')
    list_filter = ('is_featured',)
    search_fields = ('title', 'url', 'template__name', 'tool__name')

@admin.register(Font)
class FontAdmin(admin.ModelAdmin):
    list_display = ('name', 'family', 'weight', 'style')
    search_fields = ('name', 'family')

@admin.register(TransformVariable)
class TransformVariableAdmin(admin.ModelAdmin):
    list_display = ('name', 'category', 'value', 'updated_at')
    list_filter = ('category',)
    search_fields = ('name',)

@admin.register(AiChatSession)
class AiChatSessionAdmin(admin.ModelAdmin):
    list_display = ('title', 'user', 'created_at', 'updated_at')
    list_filter = ('created_at', 'updated_at')
    search_fields = ('title', 'user__username', 'user__email')

@admin.register(AiChatMessage)
class AiChatMessageAdmin(admin.ModelAdmin):
    list_display = ('session', 'role', 'created_at')
    list_filter = ('role', 'created_at')
    search_fields = ('content',)

@admin.register(Referral)
class ReferralAdmin(admin.ModelAdmin):
    list_display = ('referrer', 'referred_user', 'reward_amount', 'created_at')
    list_filter = ('is_rewarded', 'created_at')
    search_fields = ('referrer__username', 'referred_user__username')

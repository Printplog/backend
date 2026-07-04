from rest_framework import serializers
from ..models import Tool, Font, TransformVariable, SiteSettings, Tutorial
from api.utils import get_signed_url

class FieldUpdateSerializer(serializers.Serializer):
    id = serializers.CharField()
    value = serializers.JSONField(required=False, allow_null=True)
    # Baked barcode PNG (single-source): the frontend encodes the barcode and
    # ships the finished image so the server-side render can inject it directly.
    barcodeImage = serializers.CharField(required=False, allow_null=True, allow_blank=True)


class ToolSerializer(serializers.ModelSerializer):
    tutorial = serializers.SerializerMethodField()

    class Meta:
        model = Tool
        fields = '__all__'

    def get_tutorial(self, obj):
        # Plain dict instead of TutorialSerializer to avoid a circular import
        # (serializers/templates.py imports from this module).
        tutorial = getattr(obj, 'tutorial', None)
        if tutorial:
            return {
                'id': tutorial.id,
                'url': tutorial.url,
                'title': tutorial.title,
                'is_featured': tutorial.is_featured,
            }
        return None

    def _sync_tutorial(self, tool):
        """Create/update/clear the tool's tutorial from tutorial_url/tutorial_title
        in the request payload (same contract as TemplateSerializer)."""
        request = self.context.get('request')
        if not request:
            return
        tutorial_url = request.data.get('tutorial_url')
        if tutorial_url is None:
            return  # field absent: leave the tutorial untouched
        tutorial_title = request.data.get('tutorial_title') or ''
        if tutorial_url == '':
            Tutorial.objects.filter(tool=tool).delete()
            return
        Tutorial.objects.update_or_create(
            tool=tool,
            defaults={'url': tutorial_url, 'title': tutorial_title},
        )

    def create(self, validated_data):
        tool = super().create(validated_data)
        self._sync_tutorial(tool)
        return tool

    def update(self, instance, validated_data):
        tool = super().update(instance, validated_data)
        self._sync_tutorial(tool)
        return tool


class TransformVariableSerializer(serializers.ModelSerializer):
    class Meta:
        model = TransformVariable
        fields = '__all__'


class FontSerializer(serializers.ModelSerializer):
    font_url = serializers.SerializerMethodField()
    
    class Meta:
        model = Font
        fields = ['id', 'name', 'family', 'weight', 'style', 'font_file', 'font_url', 'created_at']
        read_only_fields = ['id', 'created_at']
    
    def get_font_url(self, obj):
        """Return signed URL for the font file."""
        if obj.font_file:
            return get_signed_url(obj.font_file)
        return None


class SiteSettingsSerializer(serializers.ModelSerializer):
    class Meta:
        model = SiteSettings
        fields = '__all__'
        read_only_fields = ['id', 'updated_at', 'template_cache_version']


class PublicSiteSettingsSerializer(serializers.ModelSerializer):
    """Serializer for guest users only showing non-sensitive configuration."""
    class Meta:
        model = SiteSettings
        fields = [
            'whatsapp_number', 'whatsapp_community_link', 'support_email',
            'telegram_link', 'twitter_link', 'instagram_link', 'tiktok_link',
            'min_topup_amount', 'funding_whatsapp_number', 'exchange_rate_override',
            'maintenance_mode', 'disable_new_signups', 'disable_deposits',
            'global_announcement_text', 'global_announcement_link', 'enable_global_announcement',
            'dev_name_obfuscated', 'owner_name_obfuscated', 'template_cache_version', 'enable_ai_features',
            'show_whatsapp_on_hover', 'show_community_on_hover', 'show_telegram_on_hover',
            'show_instagram_on_hover', 'show_twitter_on_hover', 'show_tiktok_on_hover',
            'enable_referrals', 'referral_percentage', 'min_referral_deposit', 'min_withdrawal_threshold',
            'enable_deposit_promo', 'deposit_promo_min_amount', 'deposit_promo_percentage',
            'deposit_promo_max_bonus', 'deposit_promo_expiry_days', 'deposit_promo_message'
        ]
        read_only_fields = fields

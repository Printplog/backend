from rest_framework import serializers
from ..models import Tool, Font, TransformVariable, SiteSettings

class FieldUpdateSerializer(serializers.Serializer):
    id = serializers.CharField()
    value = serializers.JSONField(required=False, allow_null=True)


class ToolSerializer(serializers.ModelSerializer):
    class Meta:
        model = Tool
        fields = '__all__'


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
        """Return absolute URL for font file"""
        request = self.context.get('request')
        if obj.font_file and request:
            return request.build_absolute_uri(obj.font_file.url)
        return obj.font_file.url if obj.font_file else None


class SiteSettingsSerializer(serializers.ModelSerializer):
    class Meta:
        model = SiteSettings
        fields = ['crypto_address', 'whatsapp_number', 'manual_purchase_text', 'updated_at']
        read_only_fields = ['updated_at']

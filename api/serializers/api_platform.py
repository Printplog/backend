from rest_framework import serializers

from api.api_security import API_KEY_SCOPES, normalize_origins, validate_theme
from api.models import ApiCustomerSettings, ApiEntitlement, ApiKey, PurchasedTemplate


class ApiEntitlementSerializer(serializers.ModelSerializer):
    class Meta:
        model = ApiEntitlement
        fields = ["status", "paid_amount", "activated_at", "updated_at"]
        read_only_fields = fields


class ApiKeySerializer(serializers.ModelSerializer):
    active = serializers.BooleanField(source="is_active", read_only=True)

    class Meta:
        model = ApiKey
        fields = [
            "id", "name", "prefix", "scopes", "allowed_origins", "active",
            "last_used_at", "expires_at", "revoked_at", "created_at",
        ]
        read_only_fields = fields


class ApiKeyCreateSerializer(serializers.Serializer):
    name = serializers.CharField(max_length=100)
    scopes = serializers.ListField(
        child=serializers.CharField(max_length=50),
        required=False,
    )
    allowed_origins = serializers.ListField(
        child=serializers.CharField(max_length=500),
        required=False,
    )
    expires_at = serializers.DateTimeField(required=False, allow_null=True)

    def validate_scopes(self, value):
        scopes = list(dict.fromkeys(value))
        unknown = set(scopes) - API_KEY_SCOPES
        if unknown:
            raise serializers.ValidationError(f"Unknown scopes: {', '.join(sorted(unknown))}.")
        if not scopes:
            raise serializers.ValidationError("At least one scope is required.")
        return scopes

    def validate_allowed_origins(self, value):
        try:
            return normalize_origins(value)
        except ValueError as exc:
            raise serializers.ValidationError(str(exc)) from exc

    def validate_expires_at(self, value):
        from django.utils import timezone

        if value and value <= timezone.now():
            raise serializers.ValidationError("Expiry must be in the future.")
        return value


class ApiCustomerSettingsSerializer(serializers.ModelSerializer):
    class Meta:
        model = ApiCustomerSettings
        fields = ["allowed_origins", "theme", "created_at", "updated_at"]
        read_only_fields = ["created_at", "updated_at"]

    def validate_allowed_origins(self, value):
        try:
            return normalize_origins(value)
        except ValueError as exc:
            raise serializers.ValidationError(str(exc)) from exc

    def validate_theme(self, value):
        try:
            return validate_theme(value)
        except ValueError as exc:
            raise serializers.ValidationError(str(exc)) from exc


class ApiDocumentSerializer(serializers.ModelSerializer):
    template_id = serializers.UUIDField(read_only=True, allow_null=True)
    template_name = serializers.CharField(source="template.name", read_only=True, allow_null=True)
    mode = serializers.SerializerMethodField()

    class Meta:
        model = PurchasedTemplate
        fields = [
            "id", "template_id", "template_name", "external_user_id", "name", "mode",
            "test", "tracking_id", "keywords", "created_at", "updated_at",
        ]
        read_only_fields = fields

    def get_mode(self, obj) -> str:
        return "test" if obj.test else "paid"

class V1EmbedSessionCreateRequestSerializer(serializers.Serializer):
    template_id = serializers.UUIDField()
    external_user_id = serializers.CharField(max_length=255)
    origin = serializers.URLField(max_length=500)
    mode = serializers.ChoiceField(choices=["test", "paid"], default="test")
    preview_mode = serializers.ChoiceField(choices=["standard", "protected"], default="standard")
    theme = serializers.JSONField(required=False)
    ttl_minutes = serializers.IntegerField(required=False, min_value=1)


class V1EmbedSessionEditRequestSerializer(serializers.Serializer):
    origin = serializers.URLField(max_length=500)
    preview_mode = serializers.ChoiceField(choices=["standard", "protected"], default="standard")
    theme = serializers.JSONField(required=False)
    ttl_minutes = serializers.IntegerField(required=False, min_value=1)


class V1TemplateSummarySerializer(serializers.Serializer):
    id = serializers.UUIDField()
    name = serializers.CharField()
    type = serializers.CharField()
    price = serializers.DecimalField(max_digits=10, decimal_places=2)
    banner_url = serializers.URLField(allow_null=True)
    version = serializers.IntegerField()
    capabilities = serializers.JSONField()
    created_at = serializers.DateTimeField()
    updated_at = serializers.DateTimeField()


class V1TemplateListSerializer(serializers.Serializer):
    results = V1TemplateSummarySerializer(many=True)


class V1DocumentListItemSerializer(serializers.Serializer):
    id = serializers.UUIDField()
    template_id = serializers.UUIDField(allow_null=True)
    template_name = serializers.CharField(allow_null=True)
    external_user_id = serializers.CharField()
    name = serializers.CharField()
    mode = serializers.ChoiceField(choices=["test", "paid"])
    tracking_id = serializers.CharField(allow_null=True)
    created_at = serializers.DateTimeField()
    updated_at = serializers.DateTimeField()


class V1DocumentPageSerializer(serializers.Serializer):
    next = serializers.URLField(allow_null=True)
    previous = serializers.URLField(allow_null=True)
    results = V1DocumentListItemSerializer(many=True)


class V1EmbedSessionResponseSerializer(serializers.Serializer):
    id = serializers.UUIDField()
    embed_url = serializers.URLField()
    expires_at = serializers.DateTimeField()
    origin = serializers.URLField()
    operation = serializers.ChoiceField(choices=["create", "edit"])
    document_id = serializers.UUIDField(allow_null=True)


class V1RenderCreateRequestSerializer(serializers.Serializer):
    format = serializers.ChoiceField(choices=["png", "pdf"])


class V1RenderJobSerializer(serializers.Serializer):
    id = serializers.UUIDField()
    document_id = serializers.UUIDField()
    format = serializers.ChoiceField(choices=["png", "pdf"])
    status = serializers.ChoiceField(choices=["queued", "running", "completed", "failed"])
    output_size = serializers.IntegerField()
    error_code = serializers.CharField(allow_blank=True)
    download_url = serializers.URLField(allow_null=True)
    download_url_expires_in = serializers.IntegerField(allow_null=True)
    expires_at = serializers.DateTimeField()
    created_at = serializers.DateTimeField()
    started_at = serializers.DateTimeField(allow_null=True)
    completed_at = serializers.DateTimeField(allow_null=True)


class V1RenderWatchSerializer(serializers.Serializer):
    websocket_url = serializers.CharField()
    expires_in = serializers.IntegerField()

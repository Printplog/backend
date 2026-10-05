from rest_framework import serializers

from ..models import PurchasedTemplate, TrackingSupportMessage, TrackingSupportReply
from ..utils.support_verification import SupportVerificationError, read_email_verification_grant


def resolve_tracking_document(tracking_id):
    document = PurchasedTemplate.objects.filter(tracking_id=tracking_id).first()
    if document is None:
        document = PurchasedTemplate.objects.filter(tracking_id__iexact=tracking_id).first()
    return document


class SupportEmailVerificationRequestSerializer(serializers.Serializer):
    tracking_id = serializers.CharField(max_length=100, trim_whitespace=True)
    source = serializers.ChoiceField(choices=TrackingSupportMessage.Source.choices)
    email = serializers.EmailField(max_length=254)

    def validate_tracking_id(self, value):
        document = resolve_tracking_document(value.strip())
        if document is None:
            raise serializers.ValidationError("We could not find a document with this tracking ID.")
        return document.tracking_id

    def validate_email(self, value):
        return value.strip().lower()


class SupportEmailVerificationConfirmSerializer(serializers.Serializer):
    challenge_id = serializers.CharField(max_length=128, trim_whitespace=True)
    code = serializers.RegexField(r"^\d{4}$")


class PublicTrackingSupportSerializer(serializers.Serializer):
    tracking_id = serializers.CharField(max_length=100, trim_whitespace=True)
    source = serializers.ChoiceField(choices=TrackingSupportMessage.Source.choices)
    customer_name = serializers.CharField(max_length=120, trim_whitespace=True, required=False, default="Website visitor")
    customer_email = serializers.EmailField(max_length=254, required=False, allow_blank=True, default="")
    subject = serializers.CharField(max_length=160, trim_whitespace=True, required=False, default="Support conversation")
    message = serializers.CharField(max_length=5000, trim_whitespace=True, required=False, allow_blank=True, default="")
    verification_token = serializers.CharField(write_only=True, trim_whitespace=True)

    def validate_tracking_id(self, value):
        tracking_id = value.strip()
        document = resolve_tracking_document(tracking_id)
        if document is None:
            raise serializers.ValidationError("We could not find a document with this tracking ID.")
        self.context["document"] = document
        return document.tracking_id

    def validate(self, attrs):
        try:
            grant = read_email_verification_grant(attrs["verification_token"])
        except SupportVerificationError as exc:
            raise serializers.ValidationError({"verification_token": str(exc)}) from exc

        if grant["tracking_id"] != attrs["tracking_id"] or grant["source"] != attrs["source"]:
            raise serializers.ValidationError({"verification_token": "Email verification does not match this tracking request."})

        supplied_email = attrs.get("customer_email", "").strip().lower()
        if supplied_email and supplied_email != grant["email"]:
            raise serializers.ValidationError({"customer_email": "Use the email address that was verified."})
        attrs["customer_email"] = grant["email"]
        return attrs

    def create(self, validated_data):
        validated_data.pop("verification_token", None)
        return TrackingSupportMessage.objects.create(
            document=self.context["document"],
            **validated_data,
        )


class TrackingSupportMessageSerializer(serializers.ModelSerializer):
    document_id = serializers.UUIDField(read_only=True)
    document_name = serializers.CharField(source="document.name", read_only=True)
    source_label = serializers.CharField(source="get_source_display", read_only=True)
    conversation = serializers.SerializerMethodField()

    def get_conversation(self, obj):
        initial = []
        if obj.message:
            initial.append({
                "id": f"initial-{obj.id}",
                "direction": TrackingSupportReply.Direction.CUSTOMER,
                "body": obj.message,
                "sender_email": obj.customer_email,
                "delivery_status": TrackingSupportReply.DeliveryStatus.RECEIVED,
                "created_at": obj.created_at,
            })
        replies = TrackingSupportReplySerializer(obj.replies.all(), many=True).data
        return [*initial, *replies]

    class Meta:
        model = TrackingSupportMessage
        fields = [
            "id",
            "document_id",
            "tracking_id",
            "document_name",
            "source",
            "source_label",
            "customer_name",
            "customer_email",
            "subject",
            "message",
            "status",
            "created_at",
            "updated_at",
            "conversation",
        ]
        read_only_fields = fields


class TrackingSupportStatusSerializer(serializers.ModelSerializer):
    class Meta:
        model = TrackingSupportMessage
        fields = ["status"]


class TrackingSupportReplySerializer(serializers.ModelSerializer):
    class Meta:
        model = TrackingSupportReply
        fields = [
            "id",
            "direction",
            "body",
            "sender_email",
            "delivery_status",
            "created_at",
        ]
        read_only_fields = fields


class TrackingSupportReplyCreateSerializer(serializers.Serializer):
    body = serializers.CharField(max_length=10000, trim_whitespace=True)


class PublicTrackingSupportThreadSerializer(serializers.ModelSerializer):
    conversation = serializers.SerializerMethodField()

    def get_conversation(self, obj):
        entries = []
        if obj.message:
            entries.append({
                "id": f"initial-{obj.id}",
                "direction": TrackingSupportReply.Direction.CUSTOMER,
                "body": obj.message,
                "delivery_status": TrackingSupportReply.DeliveryStatus.RECEIVED,
                "created_at": obj.created_at,
            })
        entries.extend(
            {
                "id": str(reply.id),
                "direction": reply.direction,
                "body": reply.body,
                "delivery_status": reply.delivery_status,
                "created_at": reply.created_at,
            }
            for reply in obj.replies.all()
        )
        return entries

    class Meta:
        model = TrackingSupportMessage
        fields = [
            "id",
            "tracking_id",
            "customer_name",
            "subject",
            "status",
            "conversation",
        ]
        read_only_fields = fields

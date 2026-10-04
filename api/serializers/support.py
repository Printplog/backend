from rest_framework import serializers

from ..models import PurchasedTemplate, TrackingSupportMessage, TrackingSupportReply


class PublicTrackingSupportSerializer(serializers.Serializer):
    tracking_id = serializers.CharField(max_length=100, trim_whitespace=True)
    source = serializers.ChoiceField(choices=TrackingSupportMessage.Source.choices)
    customer_name = serializers.CharField(max_length=120, trim_whitespace=True)
    customer_email = serializers.EmailField(max_length=254)
    subject = serializers.CharField(max_length=160, trim_whitespace=True)
    message = serializers.CharField(max_length=5000, trim_whitespace=True)

    def validate_tracking_id(self, value):
        tracking_id = value.strip()
        document = PurchasedTemplate.objects.filter(tracking_id=tracking_id).first()
        if document is None:
            document = PurchasedTemplate.objects.filter(tracking_id__iexact=tracking_id).first()
        if document is None:
            raise serializers.ValidationError("We could not find a document with this tracking ID.")
        self.context["document"] = document
        return document.tracking_id

    def create(self, validated_data):
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
        initial = {
            "id": f"initial-{obj.id}",
            "direction": TrackingSupportReply.Direction.CUSTOMER,
            "body": obj.message,
            "sender_email": obj.customer_email,
            "delivery_status": TrackingSupportReply.DeliveryStatus.RECEIVED,
            "created_at": obj.created_at,
        }
        replies = TrackingSupportReplySerializer(obj.replies.all(), many=True).data
        return [initial, *replies]

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

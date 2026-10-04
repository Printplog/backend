import json
import logging
import uuid

from django.shortcuts import get_object_or_404
from rest_framework import status
from rest_framework.permissions import AllowAny, IsAuthenticated
from rest_framework.response import Response
from rest_framework.throttling import ScopedRateThrottle
from rest_framework.views import APIView

from ..models import TrackingSupportMessage, TrackingSupportReply
from ..serializers.support import (
    PublicTrackingSupportSerializer,
    TrackingSupportReplyCreateSerializer,
    TrackingSupportReplySerializer,
    TrackingSupportMessageSerializer,
    TrackingSupportStatusSerializer,
)
from ..utils.support_email import (
    SupportEmailError,
    notify_owner_of_new_ticket,
    process_inbound_email,
    send_owner_reply,
    update_delivery_status,
    verify_webhook,
)
from ..utils.integration_secrets import get_integration_secret

logger = logging.getLogger(__name__)


class PublicTrackingSupportView(APIView):
    authentication_classes = []
    permission_classes = [AllowAny]
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = "tracking_support"

    def post(self, request):
        serializer = PublicTrackingSupportSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        support_message = serializer.save()
        if get_integration_secret("resend_api_key"):
            try:
                notify_owner_of_new_ticket(support_message)
            except SupportEmailError:
                # The ticket is already safely stored and visible in the
                # dashboard. Do not make a temporary provider outage lose it.
                logger.exception("Could not email owner for support ticket %s", support_message.id)
        return Response(
            {
                "id": str(support_message.id),
                "message": "Your support request has been received.",
            },
            status=status.HTTP_201_CREATED,
        )


class TrackingSupportMessageListView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request):
        messages = TrackingSupportMessage.objects.filter(
            document__buyer=request.user,
        ).select_related("document").prefetch_related("replies").order_by("-updated_at")
        source = request.query_params.get("source", "").strip()
        message_status = request.query_params.get("status", "").strip()
        document_id = request.query_params.get("document_id", "").strip()
        if source:
            messages = messages.filter(source=source)
        if message_status:
            messages = messages.filter(status=message_status)
        if document_id:
            messages = messages.filter(document_id=document_id)
        return Response({
            "results": TrackingSupportMessageSerializer(messages, many=True).data,
            "unread_count": messages.filter(status=TrackingSupportMessage.Status.NEW).count(),
        })


class TrackingSupportMessageDetailView(APIView):
    permission_classes = [IsAuthenticated]

    def patch(self, request, message_id):
        support_message = get_object_or_404(
            TrackingSupportMessage.objects.select_related("document"),
            id=message_id,
            document__buyer=request.user,
        )
        serializer = TrackingSupportStatusSerializer(
            support_message,
            data=request.data,
            partial=True,
        )
        serializer.is_valid(raise_exception=True)
        serializer.save()
        return Response(TrackingSupportMessageSerializer(support_message).data)


class TrackingSupportReplyView(APIView):
    permission_classes = [IsAuthenticated]

    def post(self, request, message_id):
        support_message = get_object_or_404(
            TrackingSupportMessage.objects.select_related("document__buyer").prefetch_related("replies"),
            id=message_id,
            document__buyer=request.user,
        )
        serializer = TrackingSupportReplyCreateSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        reply_id = uuid.uuid4()
        try:
            resend_email_id = send_owner_reply(
                support_message,
                serializer.validated_data["body"],
                idempotency_key=f"support-dashboard-{reply_id.hex}",
            )
        except SupportEmailError as exc:
            return Response({"detail": str(exc)}, status=status.HTTP_503_SERVICE_UNAVAILABLE)

        reply = TrackingSupportReply.objects.create(
            id=reply_id,
            support_message=support_message,
            direction=TrackingSupportReply.Direction.OWNER,
            body=serializer.validated_data["body"],
            sender_email=request.user.email,
            delivery_status=TrackingSupportReply.DeliveryStatus.QUEUED,
            resend_email_id=resend_email_id,
        )
        if support_message.status == TrackingSupportMessage.Status.NEW:
            support_message.status = TrackingSupportMessage.Status.READ
        support_message.save(update_fields=["status", "updated_at"])
        return Response(TrackingSupportReplySerializer(reply).data, status=status.HTTP_201_CREATED)


class ResendWebhookView(APIView):
    authentication_classes = []
    permission_classes = [AllowAny]

    def post(self, request):
        payload = request.body
        try:
            verify_webhook(payload, request.headers)
            event = json.loads(payload)
            if not isinstance(event, dict) or not isinstance(event.get("data", {}), dict):
                raise SupportEmailError("Webhook payload must be an object.")
        except (SupportEmailError, json.JSONDecodeError) as exc:
            logger.warning("Rejected Resend webhook: %s", exc)
            return Response({"detail": "Invalid webhook."}, status=status.HTTP_400_BAD_REQUEST)
        event_type = event.get("type", "")
        data = event.get("data") or {}
        try:
            if event_type == "email.received":
                result = process_inbound_email(data)
            else:
                result = "updated" if update_delivery_status(event_type, data) else "ignored"
        except SupportEmailError:
            logger.exception("Resend webhook processing failed")
            # A non-2xx response asks Resend to retry a temporary API/provider
            # failure. Duplicate inbound messages are handled idempotently.
            return Response({"detail": "Webhook processing failed."}, status=status.HTTP_503_SERVICE_UNAVAILABLE)
        return Response({"received": True, "result": result})

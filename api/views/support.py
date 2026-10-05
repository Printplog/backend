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
    PublicTrackingSupportThreadSerializer,
    SupportEmailVerificationConfirmSerializer,
    SupportEmailVerificationRequestSerializer,
    TrackingSupportReplyCreateSerializer,
    TrackingSupportReplySerializer,
    TrackingSupportMessageSerializer,
    TrackingSupportStatusSerializer,
)
from ..utils.support_email import (
    SupportEmailError,
    notify_owner_of_customer_reply,
    notify_owner_of_new_ticket,
    process_inbound_email,
    send_owner_reply,
    update_delivery_status,
    verify_webhook,
)
from ..utils.integration_secrets import get_integration_secret
from ..utils.support_realtime import (
    authorize_private_channel,
    create_customer_access_token,
    customer_token_matches,
    owner_channel,
    public_realtime_config,
    publish_support_update,
    ticket_channel,
)
from ..utils.support_verification import (
    CHALLENGE_TTL_SECONDS,
    SupportVerificationError,
    confirm_email_verification,
    request_email_verification,
)

logger = logging.getLogger(__name__)


class SupportEmailVerificationRequestView(APIView):
    authentication_classes = []
    permission_classes = [AllowAny]
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = "support_verification"

    def post(self, request):
        serializer = SupportEmailVerificationRequestSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        try:
            challenge_id = request_email_verification(**serializer.validated_data)
        except SupportEmailError:
            logger.exception("Could not send support email verification code")
            return Response(
                {"detail": "The verification email could not be sent. Try again shortly."},
                status=status.HTTP_503_SERVICE_UNAVAILABLE,
            )
        email = serializer.validated_data["email"]
        local, _, domain = email.partition("@")
        email_hint = f"{local[:2]}{'*' * max(1, len(local) - 2)}@{domain}"
        return Response({
            "challenge_id": challenge_id,
            "email_hint": email_hint,
            "expires_in": CHALLENGE_TTL_SECONDS,
        })


class SupportEmailVerificationConfirmView(APIView):
    authentication_classes = []
    permission_classes = [AllowAny]
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = "support_verification"

    def post(self, request):
        serializer = SupportEmailVerificationConfirmSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        try:
            token, payload = confirm_email_verification(**serializer.validated_data)
        except SupportVerificationError as exc:
            return Response({"detail": str(exc)}, status=status.HTTP_400_BAD_REQUEST)
        return Response({
            "verification_token": token,
            "email": payload["email"],
        })


class PublicTrackingSupportView(APIView):
    authentication_classes = []
    permission_classes = [AllowAny]
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = "tracking_support"

    def post(self, request):
        serializer = PublicTrackingSupportSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        customer_token, customer_token_hash = create_customer_access_token()
        support_message = serializer.save(customer_access_token_hash=customer_token_hash)
        if support_message.message and get_integration_secret("resend_api_key"):
            try:
                notify_owner_of_new_ticket(support_message)
            except SupportEmailError:
                # The ticket is already safely stored and visible in the
                # dashboard. Do not make a temporary provider outage lose it.
                logger.exception("Could not email owner for support ticket %s", support_message.id)
        publish_support_update(support_message, event="support.created")
        return Response(
            {
                "id": str(support_message.id),
                "message": "Your support request has been received.",
                "access_token": customer_token,
                "channel": ticket_channel(support_message.id),
                "realtime": public_realtime_config(),
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
            "channel": owner_channel(request.user.id),
            "realtime": public_realtime_config(),
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
        publish_support_update(support_message)
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
        resend_email_id = None
        delivery_status = TrackingSupportReply.DeliveryStatus.RECEIVED
        if support_message.customer_email:
            try:
                resend_email_id = send_owner_reply(
                    support_message,
                    serializer.validated_data["body"],
                    idempotency_key=f"support-dashboard-{reply_id.hex}",
                )
                delivery_status = TrackingSupportReply.DeliveryStatus.QUEUED
            except SupportEmailError as exc:
                return Response({"detail": str(exc)}, status=status.HTTP_503_SERVICE_UNAVAILABLE)

        reply = TrackingSupportReply.objects.create(
            id=reply_id,
            support_message=support_message,
            direction=TrackingSupportReply.Direction.OWNER,
            body=serializer.validated_data["body"],
            sender_email=request.user.email,
            delivery_status=delivery_status,
            resend_email_id=resend_email_id,
        )
        if support_message.status == TrackingSupportMessage.Status.NEW:
            support_message.status = TrackingSupportMessage.Status.READ
        support_message.save(update_fields=["status", "updated_at"])
        publish_support_update(support_message)
        return Response(TrackingSupportReplySerializer(reply).data, status=status.HTTP_201_CREATED)


def _customer_ticket(request, message_id):
    ticket = get_object_or_404(
        TrackingSupportMessage.objects.select_related("document__buyer").prefetch_related("replies"),
        id=message_id,
    )
    if not customer_token_matches(ticket, request.headers.get("X-Support-Token", "")):
        # Keep unauthorized and unknown conversations indistinguishable.
        raise TrackingSupportMessage.DoesNotExist
    return ticket


class PublicTrackingSupportThreadView(APIView):
    authentication_classes = []
    permission_classes = [AllowAny]
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = "tracking_support"

    def get(self, request, message_id):
        try:
            ticket = _customer_ticket(request, message_id)
        except TrackingSupportMessage.DoesNotExist:
            return Response({"detail": "Conversation not found."}, status=status.HTTP_404_NOT_FOUND)
        data = PublicTrackingSupportThreadSerializer(ticket).data
        data["channel"] = ticket_channel(ticket.id)
        data["realtime"] = public_realtime_config()
        return Response(data)


class PublicTrackingSupportReplyView(APIView):
    authentication_classes = []
    permission_classes = [AllowAny]
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = "tracking_support"

    def post(self, request, message_id):
        try:
            ticket = _customer_ticket(request, message_id)
        except TrackingSupportMessage.DoesNotExist:
            return Response({"detail": "Conversation not found."}, status=status.HTTP_404_NOT_FOUND)

        serializer = TrackingSupportReplyCreateSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        reply = TrackingSupportReply.objects.create(
            support_message=ticket,
            direction=TrackingSupportReply.Direction.CUSTOMER,
            body=serializer.validated_data["body"],
            sender_email=ticket.customer_email,
            delivery_status=TrackingSupportReply.DeliveryStatus.RECEIVED,
        )
        if get_integration_secret("resend_api_key"):
            try:
                reply.resend_email_id = notify_owner_of_customer_reply(
                    ticket,
                    reply.body,
                    idempotency_key=f"support-widget-owner-{reply.id.hex}",
                )
                reply.delivery_status = TrackingSupportReply.DeliveryStatus.QUEUED
            except SupportEmailError:
                reply.delivery_status = TrackingSupportReply.DeliveryStatus.FAILED
                logger.exception("Could not email owner for customer reply %s", reply.id)
            reply.save(update_fields=["resend_email_id", "delivery_status"])
        ticket.status = TrackingSupportMessage.Status.NEW
        ticket.save(update_fields=["status", "updated_at"])
        publish_support_update(ticket, event="support.customer_message")
        return Response(TrackingSupportReplySerializer(reply).data, status=status.HTTP_201_CREATED)


class PublicSupportRealtimeAuthView(APIView):
    authentication_classes = []
    permission_classes = [AllowAny]
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = "tracking_support"

    def post(self, request, message_id):
        try:
            ticket = _customer_ticket(request, message_id)
        except TrackingSupportMessage.DoesNotExist:
            return Response({"detail": "Conversation not found."}, status=status.HTTP_404_NOT_FOUND)
        channel_name = str(request.data.get("channel_name", ""))
        socket_id = str(request.data.get("socket_id", ""))
        if channel_name != ticket_channel(ticket.id) or not socket_id:
            return Response({"detail": "Invalid realtime channel."}, status=status.HTTP_403_FORBIDDEN)
        try:
            return Response(authorize_private_channel(socket_id, channel_name))
        except ValueError as exc:
            return Response({"detail": str(exc)}, status=status.HTTP_503_SERVICE_UNAVAILABLE)


class OwnerSupportRealtimeAuthView(APIView):
    permission_classes = [IsAuthenticated]

    def post(self, request):
        channel_name = str(request.data.get("channel_name", ""))
        socket_id = str(request.data.get("socket_id", ""))
        if channel_name != owner_channel(request.user.id) or not socket_id:
            return Response({"detail": "Invalid realtime channel."}, status=status.HTTP_403_FORBIDDEN)
        try:
            return Response(authorize_private_channel(socket_id, channel_name))
        except ValueError as exc:
            return Response({"detail": str(exc)}, status=status.HTTP_503_SERVICE_UNAVAILABLE)


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

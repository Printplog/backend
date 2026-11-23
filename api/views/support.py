import io
import json
import logging
import re
import uuid

from django.core.files.base import ContentFile
from django.db.models import Q
from django.shortcuts import get_object_or_404
from PIL import Image, ImageOps, UnidentifiedImageError
from rest_framework import status
from rest_framework.parsers import FormParser, JSONParser, MultiPartParser
from rest_framework.permissions import AllowAny, IsAuthenticated
from rest_framework.response import Response
from rest_framework.throttling import ScopedRateThrottle
from rest_framework.views import APIView

from ..models import OwnerSupportSettings, TrackingSupportMessage, TrackingSupportReply
from ..serializers.support import (
    PublicTrackingSupportSerializer,
    PublicTrackingSupportThreadSerializer,
    SupportEmailVerificationConfirmSerializer,
    SupportEmailVerificationRequestSerializer,
    SupportTypingSerializer,
    TrackingSupportReplyCreateSerializer,
    TrackingSupportReplySerializer,
    TrackingSupportMessageSerializer,
    TrackingSupportStatusSerializer,
    owner_support_display_name,
    serialize_owner_support_profile,
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
    publish_support_typing,
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

MAX_SUPPORT_AVATAR_BYTES = 5 * 1024 * 1024
ALLOWED_AVATAR_FORMATS = {"PNG", "JPEG", "WEBP", "GIF"}
OWNER_CHANNEL_RE = re.compile(r"^private-support-owner-\d+$")


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


def _owner_support_queryset(user, *, include_admin_fernway=False):
    owner_q = Q(document__buyer=user) | Q(fernway_profile__user=user)
    if include_admin_fernway and (user.is_staff or user.is_superuser):
        owner_q = owner_q | Q(fernway_profile__isnull=False)
    return TrackingSupportMessage.objects.filter(owner_q).select_related(
        "document__buyer",
        "document__buyer__support_settings",
        "fernway_profile__user",
        "fernway_profile__user__support_settings",
    ).prefetch_related("replies")


def _process_support_avatar(uploaded_file, user_id):
    if uploaded_file.size and uploaded_file.size > MAX_SUPPORT_AVATAR_BYTES:
        raise ValueError("Support picture must be 5 MB or smaller.")
    raw = uploaded_file.read(MAX_SUPPORT_AVATAR_BYTES + 1)
    if len(raw) > MAX_SUPPORT_AVATAR_BYTES:
        raise ValueError("Support picture must be 5 MB or smaller.")
    try:
        with Image.open(io.BytesIO(raw)) as probe:
            fmt = (probe.format or "").upper()
            if fmt not in ALLOWED_AVATAR_FORMATS:
                raise ValueError("Upload a PNG, JPG, WebP, or GIF image.")
            probe.verify()
        with Image.open(io.BytesIO(raw)) as image:
            image = ImageOps.exif_transpose(image)
            side = min(320, max(1, min(image.width, image.height)))
            if image.mode in ("RGBA", "LA") or (image.mode == "P" and "transparency" in image.info):
                image = image.convert("RGBA")
            else:
                image = image.convert("RGB")
            cropped = ImageOps.fit(image, (side, side), method=Image.Resampling.LANCZOS)
            out = io.BytesIO()
            cropped.save(out, format="WEBP", quality=86)
            return ContentFile(out.getvalue(), name=f"{user_id}-{uuid.uuid4().hex[:12]}.webp")
    except ValueError:
        raise
    except (UnidentifiedImageError, OSError) as exc:
        raise ValueError("That file does not look like a valid image.") from exc


class OwnerSupportSettingsView(APIView):
    permission_classes = [IsAuthenticated]
    parser_classes = [MultiPartParser, FormParser, JSONParser]

    def get(self, request):
        return Response(serialize_owner_support_profile(request.user, request=request))

    def patch(self, request):
        settings_obj, _ = OwnerSupportSettings.objects.get_or_create(user=request.user)
        updated_fields = []

        if "display_name" in request.data:
            raw_name = str(request.data.get("display_name") or "").strip()
            if len(raw_name) > 80:
                return Response(
                    {"detail": "Display name must be 80 characters or fewer."},
                    status=status.HTTP_400_BAD_REQUEST,
                )
            settings_obj.display_name = raw_name
            updated_fields.append("display_name")

        remove_avatar_raw = request.data.get("remove_avatar")
        remove_avatar = str(remove_avatar_raw).strip().lower() in ("true", "1", "yes")
        uploaded_avatar = request.FILES.get("avatar")

        if uploaded_avatar is not None:
            try:
                processed = _process_support_avatar(uploaded_avatar, request.user.id)
            except ValueError as exc:
                return Response({"detail": str(exc)}, status=status.HTTP_400_BAD_REQUEST)
            old_avatar = settings_obj.avatar
            old_name = old_avatar.name if old_avatar else ""
            settings_obj.avatar.save(processed.name, processed, save=False)
            updated_fields.append("avatar")
            if old_name:
                try:
                    old_avatar.storage.delete(old_name)
                except Exception:
                    logger.warning("Could not delete previous support avatar %s", old_name)
        elif remove_avatar and settings_obj.avatar:
            old_avatar = settings_obj.avatar
            old_name = old_avatar.name
            settings_obj.avatar = None
            updated_fields.append("avatar")
            if old_name:
                try:
                    old_avatar.storage.delete(old_name)
                except Exception:
                    logger.warning("Could not delete support avatar %s", old_name)

        if updated_fields:
            settings_obj.save(update_fields=[*set(updated_fields), "updated_at"])
            # Refresh request.user cached relation and push live update to active chats
            request.user.support_settings = settings_obj
            active_tickets = (
                _owner_support_queryset(request.user)
                .exclude(status=TrackingSupportMessage.Status.CLOSED)
                .order_by("-updated_at")[:10]
            )
            for ticket in active_tickets:
                publish_support_update(ticket, event="support.updated")

        return Response(serialize_owner_support_profile(request.user, request=request))


class TrackingSupportMessageListView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request):
        fernway_profile_id = request.query_params.get("fernway_profile_id", "").strip()
        messages = _owner_support_queryset(
            request.user,
            include_admin_fernway=bool(fernway_profile_id),
        ).order_by("-updated_at")
        source = request.query_params.get("source", "").strip()
        message_status = request.query_params.get("status", "").strip()
        document_id = request.query_params.get("document_id", "").strip()
        if source:
            messages = messages.filter(source=source)
        if message_status:
            messages = messages.filter(status=message_status)
        if document_id:
            messages = messages.filter(document_id=document_id)
        if fernway_profile_id:
            messages = messages.filter(fernway_profile_id=fernway_profile_id)

        effective_owner = request.user
        if fernway_profile_id and (request.user.is_staff or request.user.is_superuser):
            from banking.models import FernwayProfile

            try:
                profile = (
                    FernwayProfile.objects.select_related("user", "user__support_settings")
                    .filter(id=fernway_profile_id)
                    .first()
                )
                if profile and profile.user:
                    effective_owner = profile.user
            except ValueError:
                pass

        return Response({
            "results": TrackingSupportMessageSerializer(messages, many=True).data,
            "unread_count": messages.filter(status=TrackingSupportMessage.Status.NEW).count(),
            "channel": owner_channel(effective_owner.id),
            "realtime": public_realtime_config(),
            "support_profile": serialize_owner_support_profile(effective_owner, request=request),
            "owner_email": getattr(request.user, "email", "") or "",
            "owner_email_verified": bool(
                getattr(request.user, "is_superuser", False) or getattr(request.user, "email_verified", False)
            ),
        })


class TrackingSupportMessageDetailView(APIView):
    permission_classes = [IsAuthenticated]

    def patch(self, request, message_id):
        support_message = get_object_or_404(
            _owner_support_queryset(request.user, include_admin_fernway=True),
            id=message_id,
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
            _owner_support_queryset(request.user, include_admin_fernway=True),
            id=message_id,
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


class TrackingSupportTypingView(APIView):
    permission_classes = [IsAuthenticated]
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = "support_typing"

    def post(self, request, message_id):
        support_message = get_object_or_404(
            _owner_support_queryset(request.user, include_admin_fernway=True),
            id=message_id,
        )
        if support_message.status == TrackingSupportMessage.Status.CLOSED:
            return Response(status=status.HTTP_204_NO_CONTENT)
        serializer = SupportTypingSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        publish_support_typing(
            support_message,
            sender="owner",
            is_typing=serializer.validated_data["is_typing"],
            sender_name=owner_support_display_name(support_message.owner or request.user),
        )
        return Response(status=status.HTTP_204_NO_CONTENT)


def _customer_ticket(request, message_id):
    ticket = get_object_or_404(
        TrackingSupportMessage.objects.select_related(
            "document__buyer",
            "document__buyer__support_settings",
            "fernway_profile__user",
            "fernway_profile__user__support_settings",
        ).prefetch_related("replies"),
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
    throttle_scope = "support_chat"

    def get(self, request, message_id):
        try:
            ticket = _customer_ticket(request, message_id)
        except TrackingSupportMessage.DoesNotExist:
            return Response({"detail": "Conversation not found."}, status=status.HTTP_404_NOT_FOUND)
        data = PublicTrackingSupportThreadSerializer(ticket).data
        data["channel"] = ticket_channel(ticket.id)
        data["realtime"] = public_realtime_config()
        data["support_profile"] = serialize_owner_support_profile(ticket.owner, request=request)
        return Response(data)


class PublicTrackingSupportReplyView(APIView):
    authentication_classes = []
    permission_classes = [AllowAny]
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = "support_chat"

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


class PublicTrackingSupportTypingView(APIView):
    authentication_classes = []
    permission_classes = [AllowAny]
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = "support_typing"

    def post(self, request, message_id):
        try:
            ticket = _customer_ticket(request, message_id)
        except TrackingSupportMessage.DoesNotExist:
            return Response({"detail": "Conversation not found."}, status=status.HTTP_404_NOT_FOUND)
        if ticket.status == TrackingSupportMessage.Status.CLOSED:
            return Response(status=status.HTTP_204_NO_CONTENT)
        serializer = SupportTypingSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        publish_support_typing(
            ticket,
            sender="customer",
            is_typing=serializer.validated_data["is_typing"],
            sender_name=ticket.customer_name or "Customer",
        )
        return Response(status=status.HTTP_204_NO_CONTENT)


class PublicSupportRealtimeAuthView(APIView):
    authentication_classes = []
    permission_classes = [AllowAny]
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = "support_chat"

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
        is_allowed_channel = channel_name == owner_channel(request.user.id) or (
            (request.user.is_staff or request.user.is_superuser) and bool(OWNER_CHANNEL_RE.match(channel_name))
        )
        if not is_allowed_channel or not socket_id:
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

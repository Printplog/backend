import hashlib
import json
import re
import uuid
from datetime import timedelta
from decimal import Decimal, ROUND_HALF_UP

from django.conf import settings as django_settings
from django.core.signing import BadSignature, SignatureExpired, TimestampSigner
from django.db import transaction
from django.db.models import Sum
from django.http import FileResponse
from django.shortcuts import get_object_or_404
from django.urls import reverse
from django.utils import timezone
from rest_framework import status
from rest_framework.exceptions import AuthenticationFailed, PermissionDenied, ValidationError
from rest_framework.pagination import CursorPagination
from rest_framework.permissions import AllowAny, IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView
from drf_spectacular.utils import OpenApiParameter, OpenApiTypes, extend_schema

from api.api_security import (
    DEFAULT_API_KEY_SCOPES,
    ApiKeyAuthentication,
    generate_api_key,
    generate_embed_token,
    hash_secret,
    has_active_api_entitlement,
    normalize_origin,
    require_scope,
    validate_theme,
)
from api.api_throttles import ApiKeyRateThrottle
from api.document_compiler import apply_editable_updates, compile_document_fields
from api.models import (
    ApiCustomerSettings,
    ApiEntitlement,
    ApiIdempotencyRecord,
    ApiKey,
    DocumentRenderJob,
    EmbedSession,
    PurchasedTemplate,
    SiteSettings,
    Template,
)
from api.protected_preview import (
    ProtectedPreviewError,
    build_protected_preview,
    protected_asset,
    public_protected_manifest,
)
from api.render_events import build_render_watch_url, broadcast_render_job, render_watch_ttl_seconds
from api.serializers.api_platform import (
    ApiCustomerSettingsSerializer,
    ApiDocumentSerializer,
    ApiEntitlementSerializer,
    ApiKeyCreateSerializer,
    ApiKeySerializer,
    V1DocumentPageSerializer,
    V1EmbedSessionCreateRequestSerializer,
    V1EmbedSessionEditRequestSerializer,
    V1EmbedSessionResponseSerializer,
    V1RenderCreateRequestSerializer,
    V1RenderJobSerializer,
    V1RenderWatchSerializer,
    V1TemplateListSerializer,
)
from api.serializers.base import FontSerializer
from api.utils import get_signed_url
from wallet.models import Wallet


RENDER_DOWNLOAD_TTL_SECONDS = 300


def _api_price(template, discount_percentage=None):
    base_price = template.tool.price if template.tool_id and template.tool else Decimal("5.00")
    if discount_percentage is None:
        discount_percentage = SiteSettings.get_settings().api_tool_discount_percentage
    multiplier = (Decimal("100.00") - Decimal(discount_percentage)) / Decimal("100.00")
    return (Decimal(base_price) * multiplier).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def _entitlement_or_403(user):
    if not has_active_api_entitlement(user):
        raise PermissionDenied("Activate API access before using this feature.")


def _serialize_access_status(user):
    site = SiteSettings.get_settings()
    entitlement = ApiEntitlement.objects.filter(user=user).first()
    customer_settings, _ = ApiCustomerSettings.objects.get_or_create(user=user)
    wallet, _ = Wallet.objects.get_or_create(user=user)
    return {
        "enabled": site.enable_api_access,
        "upgrade_required": site.require_api_upgrade,
        "upgrade_price": str(site.api_upgrade_price),
        "tool_discount_percentage": str(site.api_tool_discount_percentage),
        "rate_limit_per_minute": site.api_default_rate_limit,
        "session_ttl_minutes": site.api_session_ttl_minutes,
        "entitlement": ApiEntitlementSerializer(entitlement).data if entitlement else None,
        "wallet_balance": str(wallet.balance),
        "wallet_spendable_balance": str(wallet.spendable_balance),
        "configuration": ApiCustomerSettingsSerializer(customer_settings).data,
        "keys": ApiKeySerializer(ApiKey.objects.filter(user=user), many=True).data,
    }


class ApiAccessStatusView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request):
        return Response(_serialize_access_status(request.user))


class ApiActivateView(APIView):
    permission_classes = [IsAuthenticated]
    throttle_scope = "wallet_write"

    @transaction.atomic
    def post(self, request):
        user_model = request.user.__class__
        user = user_model.objects.select_for_update().get(pk=request.user.pk)
        site = SiteSettings.get_settings()
        if not site.enable_api_access:
            raise PermissionDenied("API access is currently disabled.")

        existing = ApiEntitlement.objects.filter(user=user).first()
        if existing:
            if existing.status == ApiEntitlement.Status.ACTIVE:
                return Response(_serialize_access_status(user))
            raise PermissionDenied("This API entitlement must be restored by an administrator.")

        price = site.api_upgrade_price if site.require_api_upgrade else Decimal("0.00")
        if price < 0:
            raise ValidationError("The configured API upgrade price is invalid.")

        payment = None
        if price > 0:
            wallet = Wallet.objects.select_for_update().get(user=user)
            try:
                payment = wallet.debit(price, description="SharpToolz API account upgrade")
            except Exception as exc:
                from django.core.exceptions import ValidationError as DjangoValidationError

                if isinstance(exc, DjangoValidationError):
                    raise ValidationError({"wallet": "Insufficient wallet balance."}) from exc
                raise

        ApiEntitlement.objects.create(
            user=user,
            status=ApiEntitlement.Status.ACTIVE,
            paid_amount=price,
            payment_transaction=payment,
        )
        return Response(_serialize_access_status(user), status=status.HTTP_201_CREATED)


class ApiCustomerConfigurationView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request):
        _entitlement_or_403(request.user)
        configuration, _ = ApiCustomerSettings.objects.get_or_create(user=request.user)
        return Response(ApiCustomerSettingsSerializer(configuration).data)

    def patch(self, request):
        _entitlement_or_403(request.user)
        configuration, _ = ApiCustomerSettings.objects.get_or_create(user=request.user)
        serializer = ApiCustomerSettingsSerializer(configuration, data=request.data, partial=True)
        serializer.is_valid(raise_exception=True)
        serializer.save()
        return Response(serializer.data)


class ApiKeyListCreateView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request):
        _entitlement_or_403(request.user)
        return Response(ApiKeySerializer(ApiKey.objects.filter(user=request.user), many=True).data)

    def post(self, request):
        _entitlement_or_403(request.user)
        site = SiteSettings.get_settings()
        if not site.enable_api_access:
            raise PermissionDenied("API access is currently disabled.")
        if ApiKey.objects.filter(user=request.user, revoked_at__isnull=True).count() >= 10:
            raise ValidationError("Revoke an existing key before creating another one.")
        serializer = ApiKeyCreateSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data
        customer_settings, _ = ApiCustomerSettings.objects.get_or_create(user=request.user)
        token, prefix, token_hash = generate_api_key()
        key = ApiKey.objects.create(
            user=request.user,
            name=data["name"].strip(),
            prefix=prefix,
            secret_hash=token_hash,
            scopes=data.get("scopes", DEFAULT_API_KEY_SCOPES),
            allowed_origins=data.get("allowed_origins", customer_settings.allowed_origins),
            expires_at=data.get("expires_at"),
        )
        response = ApiKeySerializer(key).data
        response["secret"] = token
        response["warning"] = "Copy this key now. SharpToolz will not show it again."
        return Response(response, status=status.HTTP_201_CREATED)


class ApiKeyRevokeView(APIView):
    permission_classes = [IsAuthenticated]

    def delete(self, request, key_id):
        key = get_object_or_404(ApiKey, pk=key_id, user=request.user)
        if key.revoked_at is None:
            revoked_at = timezone.now()
            key.revoked_at = revoked_at
            key.save(update_fields=["revoked_at"])
            EmbedSession.objects.filter(
                api_key=key,
                status=EmbedSession.Status.PENDING,
            ).update(status=EmbedSession.Status.REVOKED, revoked_at=revoked_at)
        return Response(status=status.HTTP_204_NO_CONTENT)


class V1ApiView(APIView):
    authentication_classes = [ApiKeyAuthentication]
    permission_classes = [IsAuthenticated]
    throttle_classes = [ApiKeyRateThrottle]


def _template_summary(template, request=None, discount_percentage=None):
    # A list thumbnail is public artwork, not the editable vector template.
    # Suppress legacy rows that reused svg_file as their banner.
    banner_name = str(template.banner.name or "") if template.banner else ""
    banner_url = get_signed_url(template.banner) if banner_name and not banner_name.lower().endswith(".svg") else None
    if request and banner_url and banner_url.startswith("/"):
        banner_url = request.build_absolute_uri(banner_url)
    return {
        "id": str(template.id),
        "name": template.name,
        "type": template.type,
        "price": str(_api_price(template, discount_percentage)),
        "banner_url": banner_url,
        "version": int(template.updated_at.timestamp()),
        "capabilities": {
            "hosted_form": True,
            "editable_after_creation": any(field.get("editable") for field in (template.form_fields or [])),
            "has_uploads": any((field.get("type") or "").lower() in {"upload", "file", "sign"} for field in (template.form_fields or [])),
        },
        "created_at": template.created_at,
        "updated_at": template.updated_at,
    }


class V1TemplateListView(V1ApiView):
    @extend_schema(
        operation_id="templates_list",
        summary="List active templates",
        responses=V1TemplateListSerializer,
    )
    def get(self, request):
        require_scope(request, "templates:read")
        discount_percentage = SiteSettings.get_settings().api_tool_discount_percentage
        templates = Template.objects.filter(is_active=True).select_related("tool").only(
            "id", "name", "type", "banner", "form_fields", "tool_id", "tool__price", "created_at", "updated_at"
        ).order_by("name")
        return Response({
            "results": [
                _template_summary(template, request, discount_percentage)
                for template in templates
            ]
        })


class DocumentCursorPagination(CursorPagination):
    page_size = 20
    max_page_size = 100
    ordering = "-created_at"


def _request_fingerprint(data):
    return hashlib.sha256(
        json.dumps(data, sort_keys=True, separators=(",", ":"), default=str).encode("utf-8")
    ).hexdigest()


def _required_uuid(value, field_name):
    try:
        return uuid.UUID(str(value))
    except (AttributeError, TypeError, ValueError) as exc:
        raise ValidationError({field_name: "A valid UUID is required."}) from exc


def _idempotency_key(request):
    key = request.headers.get("Idempotency-Key", "").strip()
    if not key:
        raise ValidationError({"Idempotency-Key": "This header is required."})
    if len(key) > 128 or not re.fullmatch(r"[A-Za-z0-9._:-]+", key):
        raise ValidationError({"Idempotency-Key": "Use 1-128 letters, numbers, dots, underscores, colons, or hyphens."})
    return key


class V1DocumentListCreateView(V1ApiView):
    @extend_schema(
        operation_id="documents_list",
        summary="List documents owned by the API customer",
        parameters=[
            OpenApiParameter("external_user_id", OpenApiTypes.STR, description="Your opaque end-user ID."),
            OpenApiParameter("cursor", OpenApiTypes.STR, description="Opaque pagination cursor."),
        ],
        responses=V1DocumentPageSerializer,
    )
    def get(self, request):
        require_scope(request, "documents:read")
        queryset = PurchasedTemplate.objects.filter(buyer=request.api_customer).select_related("template").only(
            "id",
            "template_id",
            "template__id",
            "template__name",
            "external_user_id",
            "name",
            "test",
            "tracking_id",
            "created_at",
            "updated_at",
        )
        external_user_id = request.query_params.get("external_user_id")
        if external_user_id is not None:
            queryset = queryset.filter(external_user_id=external_user_id)
        paginator = DocumentCursorPagination()
        page = paginator.paginate_queryset(queryset.order_by("-created_at"), request, view=self)
        results = [
            {
                "id": str(document.id),
                "template_id": str(document.template_id) if document.template_id else None,
                "template_name": document.template.name if document.template else None,
                "external_user_id": document.external_user_id,
                "name": document.name,
                "mode": "test" if document.test else "paid",
                "tracking_id": document.tracking_id,
                "created_at": document.created_at,
                "updated_at": document.updated_at,
            }
            for document in page
        ]
        return paginator.get_paginated_response(results)

class V1DocumentDetailView(V1ApiView):
    def get_object(self, request, document_id, lock=False):
        queryset = PurchasedTemplate.objects.filter(buyer=request.api_customer).select_related("template")
        if lock:
            queryset = queryset.select_for_update()
        return get_object_or_404(queryset, pk=document_id)

    @extend_schema(
        operation_id="documents_retrieve",
        summary="Retrieve one owned document",
        responses=ApiDocumentSerializer,
    )
    def get(self, request, document_id):
        require_scope(request, "documents:read")
        document = self.get_object(request, document_id)
        return Response(ApiDocumentSerializer(document, context={"request": request}).data)

    @extend_schema(
        operation_id="documents_destroy",
        summary="Delete one owned document",
        responses={204: None},
    )
    def delete(self, request, document_id):
        require_scope(request, "documents:write")
        document = self.get_object(request, document_id)
        document.delete()
        return Response(status=204)


class V1DocumentUpgradeView(V1ApiView):
    @transaction.atomic
    @extend_schema(
        operation_id="documents_upgrade",
        summary="Charge the customer wallet and upgrade a test document",
        parameters=[
            OpenApiParameter(
                "Idempotency-Key",
                OpenApiTypes.STR,
                location=OpenApiParameter.HEADER,
                required=True,
            ),
        ],
        request=None,
        responses=ApiDocumentSerializer,
    )
    def post(self, request, document_id):
        require_scope(request, "documents:write")
        idempotency_key = _idempotency_key(request)
        document = get_object_or_404(
            PurchasedTemplate.objects.select_for_update().select_related("template", "template__tool"),
            pk=document_id,
            buyer=request.api_customer,
        )
        if not document.test:
            return Response(ApiDocumentSerializer(document, context={"request": request}).data)
        request_hash = _request_fingerprint({"document_id": str(document.id)})
        record, created = ApiIdempotencyRecord.objects.get_or_create(
            api_key=request.api_key,
            operation="document.upgrade",
            key=idempotency_key,
            defaults={"request_hash": request_hash, "document": document},
        )
        if not created and (record.request_hash != request_hash or record.document_id != document.id):
            return Response({"detail": "Idempotency key was already used with a different request."}, status=409)
        price = _api_price(document.template)
        if price > 0:
            wallet = Wallet.objects.select_for_update().get(user=request.api_customer)
            try:
                wallet.debit(price, description=f"API document upgrade: {document.name}")
            except Exception as exc:
                from django.core.exceptions import ValidationError as DjangoValidationError

                if isinstance(exc, DjangoValidationError):
                    raise ValidationError({"wallet": "Insufficient wallet balance."}) from exc
                raise
        document.test = False
        document.save(update_fields=["test", "updated_at"])
        return Response(ApiDocumentSerializer(document, context={"request": request}).data)


def _render_download_url(request, job):
    if (
        job.status != DocumentRenderJob.Status.COMPLETED
        or not job.output_file
        or job.expires_at <= timezone.now()
    ):
        return None
    signature = TimestampSigner(salt="api-render-download-v1").sign(str(job.id))
    path = reverse("public-render-download", kwargs={"job_id": job.id})
    return request.build_absolute_uri(f"{path}?signature={signature}")


def _serialize_render_job(request, job):
    download_url = _render_download_url(request, job)
    return {
        "id": str(job.id),
        "document_id": str(job.document_id),
        "format": job.format,
        "status": job.status,
        "output_size": job.output_size,
        "error_code": job.error_code,
        "download_url": download_url,
        "download_url_expires_in": RENDER_DOWNLOAD_TTL_SECONDS if download_url else None,
        "expires_at": job.expires_at,
        "created_at": job.created_at,
        "started_at": job.started_at,
        "completed_at": job.completed_at,
    }


class V1DocumentRenderCreateView(V1ApiView):
    @transaction.atomic
    @extend_schema(
        operation_id="document_renders_create",
        summary="Queue an isolated server-side PDF or PNG render",
        parameters=[
            OpenApiParameter(
                "Idempotency-Key",
                OpenApiTypes.STR,
                location=OpenApiParameter.HEADER,
                required=True,
            ),
        ],
        request=V1RenderCreateRequestSerializer,
        responses={201: V1RenderJobSerializer, 200: V1RenderJobSerializer},
    )
    def post(self, request, document_id):
        require_scope(request, "documents:read")
        idempotency_key = _idempotency_key(request)
        if not isinstance(request.data, dict) or set(request.data) != {"format"}:
            raise ValidationError("Exactly one field, format, is required.")
        output_format = request.data.get("format")
        if output_format not in {DocumentRenderJob.Format.PNG, DocumentRenderJob.Format.PDF}:
            raise ValidationError({"format": "Choose png or pdf."})
        document = get_object_or_404(
            PurchasedTemplate.objects.filter(buyer=request.api_customer),
            pk=document_id,
        )

        request_hash = _request_fingerprint({"document_id": str(document.id), "format": output_format})
        record, created = ApiIdempotencyRecord.objects.get_or_create(
            api_key=request.api_key,
            operation="document.render",
            key=idempotency_key,
            defaults={"request_hash": request_hash, "document": document},
        )
        if not created:
            if record.request_hash != request_hash or record.document_id != document.id:
                return Response({"detail": "Idempotency key was already used with a different request."}, status=409)
            if record.render_job_id:
                return Response(_serialize_render_job(request, record.render_job))
            return Response({"detail": "The original render request is still being processed."}, status=409)

        active_jobs = DocumentRenderJob.objects.filter(
            requested_by_key=request.api_key,
            status__in=[DocumentRenderJob.Status.QUEUED, DocumentRenderJob.Status.RUNNING],
            expires_at__gt=timezone.now(),
        ).count()
        active_limit = max(1, min(getattr(django_settings, "API_RENDER_MAX_ACTIVE_PER_KEY", 10), 100))
        if active_jobs >= active_limit:
            raise ValidationError("This API key already has too many active render jobs.")

        active_user_limit = max(1, min(getattr(django_settings, "API_RENDER_MAX_ACTIVE_PER_USER", 20), 200))
        active_user_jobs = DocumentRenderJob.objects.filter(
            user=request.api_customer,
            status__in=[DocumentRenderJob.Status.QUEUED, DocumentRenderJob.Status.RUNNING],
            expires_at__gt=timezone.now(),
        ).count()
        if active_user_jobs >= active_user_limit:
            raise ValidationError("This API account already has too many active render jobs.")

        storage_limit = max(
            50 * 1024 * 1024,
            min(getattr(django_settings, "API_RENDER_STORAGE_BYTES_PER_USER", 1024 ** 3), 10 * 1024 ** 3),
        )
        stored_bytes = DocumentRenderJob.objects.filter(
            user=request.api_customer,
            status=DocumentRenderJob.Status.COMPLETED,
            expires_at__gt=timezone.now(),
        ).aggregate(total=Sum("output_size"))["total"] or 0
        if stored_bytes >= storage_limit:
            raise ValidationError("This API account has reached its temporary render storage limit.")

        retention_hours = max(1, min(getattr(django_settings, "API_RENDER_RETENTION_HOURS", 24), 168))
        job = DocumentRenderJob.objects.create(
            user=request.api_customer,
            document=document,
            requested_by_key=request.api_key,
            format=output_format,
            expires_at=timezone.now() + timedelta(hours=retention_hours),
        )
        record.render_job = job
        record.save(update_fields=["render_job"])

        def enqueue_render():
            from api.tasks import render_document

            try:
                render_document.delay(str(job.id))
            except Exception:
                DocumentRenderJob.objects.filter(pk=job.id).update(
                    status=DocumentRenderJob.Status.FAILED,
                    error_code="queue_unavailable",
                    completed_at=timezone.now(),
                )
                broadcast_render_job(job.id)

        transaction.on_commit(enqueue_render, robust=True)
        return Response(_serialize_render_job(request, job), status=201)


class V1RenderDetailView(V1ApiView):
    @extend_schema(
        operation_id="document_renders_retrieve",
        summary="Retrieve a render job and receive a five-minute signed download URL when complete",
        responses=V1RenderJobSerializer,
    )
    def get(self, request, job_id):
        require_scope(request, "documents:read")
        job = get_object_or_404(DocumentRenderJob, pk=job_id, user=request.api_customer)
        return Response(_serialize_render_job(request, job))


class V1RenderWatchView(V1ApiView):
    @extend_schema(
        operation_id="document_renders_watch",
        summary="Create a short-lived WebSocket ticket for render status updates",
        request=None,
        responses=V1RenderWatchSerializer,
    )
    def post(self, request, job_id):
        require_scope(request, "documents:read")
        job = get_object_or_404(DocumentRenderJob, pk=job_id, user=request.api_customer)
        return Response({
            "websocket_url": build_render_watch_url(request, job, request.api_key),
            "expires_in": render_watch_ttl_seconds(),
        })


class PublicRenderDownloadView(APIView):
    authentication_classes = []
    permission_classes = [AllowAny]

    @extend_schema(exclude=True)
    def get(self, request, job_id):
        signature = request.query_params.get("signature", "")
        try:
            signed_job_id = TimestampSigner(salt="api-render-download-v1").unsign(
                signature,
                max_age=RENDER_DOWNLOAD_TTL_SECONDS,
            )
        except (BadSignature, SignatureExpired):
            raise PermissionDenied("This download link is invalid or expired.")
        if signed_job_id != str(job_id):
            raise PermissionDenied("This download link is invalid or expired.")
        job = get_object_or_404(
            DocumentRenderJob.objects.select_related("requested_by_key", "user"),
            pk=job_id,
        )
        if job.status != DocumentRenderJob.Status.COMPLETED or not job.output_file:
            raise PermissionDenied("This render is not available.")
        if (
            not SiteSettings.get_settings().enable_api_access
            or not has_active_api_entitlement(job.user)
            or not job.requested_by_key_id
            or not job.requested_by_key.is_active
        ):
            raise PermissionDenied("This render is no longer available.")
        if job.expires_at <= timezone.now():
            return Response({"detail": "This render expired."}, status=410)
        content_type = "image/png" if job.format == DocumentRenderJob.Format.PNG else "application/pdf"
        response = FileResponse(
            job.output_file.open("rb"),
            as_attachment=True,
            filename=f"sharptoolz-{job.document_id}.{job.format}",
            content_type=content_type,
        )
        response["Cache-Control"] = "private, no-store"
        response["X-Content-Type-Options"] = "nosniff"
        if job.output_size:
            response["Content-Length"] = str(job.output_size)
        return response


def _validate_session_payload(data, api_key, user):
    if not isinstance(data, dict):
        raise ValidationError("A JSON object is required.")
    allowed = {
        "template_id", "external_user_id", "mode", "preview_mode", "origin", "theme", "ttl_minutes",
    }
    unknown = set(data) - allowed
    if unknown:
        raise ValidationError(f"Unknown fields: {', '.join(sorted(unknown))}.")
    external_user_id = data.get("external_user_id")
    if not isinstance(external_user_id, str) or not external_user_id.strip() or len(external_user_id) > 255:
        raise ValidationError({"external_user_id": "A non-empty string up to 255 characters is required."})
    try:
        origin = normalize_origin(data.get("origin", ""))
    except ValueError as exc:
        raise ValidationError({"origin": str(exc)}) from exc
    customer_settings, _ = ApiCustomerSettings.objects.get_or_create(user=user)
    allowed_origins = api_key.allowed_origins or customer_settings.allowed_origins
    if origin not in allowed_origins:
        raise PermissionDenied("This origin is not allowed for the API key.")
    mode = data.get("mode", "test")
    if mode not in {"test", "paid"}:
        raise ValidationError({"mode": "Choose test or paid."})
    preview_mode = data.get("preview_mode", "standard")
    if preview_mode not in {"standard", "protected"}:
        raise ValidationError({"preview_mode": "Choose standard or protected."})
    try:
        theme = validate_theme(data.get("theme", customer_settings.theme or None))
    except ValueError as exc:
        raise ValidationError({"theme": str(exc)}) from exc
    return external_user_id.strip(), origin, mode, preview_mode, theme


def _validate_edit_session_payload(data, api_key, user):
    if not isinstance(data, dict):
        raise ValidationError("A JSON object is required.")
    allowed = {"origin", "preview_mode", "theme", "ttl_minutes"}
    unknown = set(data) - allowed
    if unknown:
        raise ValidationError(f"Unknown fields: {', '.join(sorted(unknown))}.")
    try:
        origin = normalize_origin(data.get("origin", ""))
    except ValueError as exc:
        raise ValidationError({"origin": str(exc)}) from exc
    customer_settings, _ = ApiCustomerSettings.objects.get_or_create(user=user)
    allowed_origins = api_key.allowed_origins or customer_settings.allowed_origins
    if origin not in allowed_origins:
        raise PermissionDenied("This origin is not allowed for the API key.")
    preview_mode = data.get("preview_mode", "standard")
    if preview_mode not in {"standard", "protected"}:
        raise ValidationError({"preview_mode": "Choose standard or protected."})
    try:
        theme = validate_theme(data.get("theme", customer_settings.theme or None))
    except ValueError as exc:
        raise ValidationError({"theme": str(exc)}) from exc
    return origin, preview_mode, theme


def _embed_session_ttl(data):
    site = SiteSettings.get_settings()
    requested_ttl = data.get("ttl_minutes", site.api_session_ttl_minutes)
    try:
        ttl = int(requested_ttl)
    except (TypeError, ValueError) as exc:
        raise ValidationError({"ttl_minutes": "An integer is required."}) from exc
    if ttl < 1 or ttl > site.api_session_ttl_minutes:
        raise ValidationError({"ttl_minutes": f"Choose between 1 and {site.api_session_ttl_minutes} minutes."})
    return ttl


def _ensure_embed_capacity(api_key):
    pending_limit = max(1, min(getattr(django_settings, "API_EMBED_MAX_PENDING_PER_KEY", 500), 5_000))
    pending_sessions = EmbedSession.objects.filter(
        api_key=api_key,
        status=EmbedSession.Status.PENDING,
        expires_at__gt=timezone.now(),
    ).count()
    if pending_sessions >= pending_limit:
        raise ValidationError("This API key already has too many pending embed sessions.")


def _embed_session_response(session, token):
    frontend_url = (django_settings.FRONTEND_URL or "http://localhost:5173").rstrip("/")
    return {
        "id": str(session.id),
        "embed_url": f"{frontend_url}/embed#{token}",
        "expires_at": session.expires_at,
        "origin": session.allowed_origin,
        "operation": session.operation,
        "document_id": str(session.document_id) if session.document_id else None,
    }


class V1EmbedSessionCreateView(V1ApiView):
    @extend_schema(
        operation_id="embed_sessions_create",
        summary="Mint a short-lived, document-scoped hosted-form URL",
        request=V1EmbedSessionCreateRequestSerializer,
        responses={201: V1EmbedSessionResponseSerializer},
    )
    def post(self, request):
        require_scope(request, "sessions:write")
        external_user_id, origin, mode, preview_mode, theme = _validate_session_payload(
            request.data, request.api_key, request.api_customer
        )
        template = get_object_or_404(
            Template.objects.select_related("tool"),
            pk=_required_uuid(request.data.get("template_id"), "template_id"),
            is_active=True,
        )
        if preview_mode == EmbedSession.PreviewMode.PROTECTED:
            try:
                build_protected_preview(template)
            except ProtectedPreviewError as exc:
                raise ValidationError({"preview_mode": str(exc)}) from exc
        _ensure_embed_capacity(request.api_key)
        ttl = _embed_session_ttl(request.data)
        token, token_hash = generate_embed_token()
        session = EmbedSession.objects.create(
            user=request.api_customer,
            api_key=request.api_key,
            template=template,
            token_hash=token_hash,
            external_user_id=external_user_id,
            allowed_origin=origin,
            mode=mode,
            preview_mode=preview_mode,
            prefill={},
            theme=theme,
            expires_at=timezone.now() + timedelta(minutes=ttl),
        )
        return Response(_embed_session_response(session, token), status=201)


class V1EmbedSessionEditView(V1ApiView):
    @extend_schema(
        operation_id="document_edit_sessions_create",
        summary="Mint a short-lived hosted editor for an owned document",
        request=V1EmbedSessionEditRequestSerializer,
        responses={201: V1EmbedSessionResponseSerializer},
    )
    def post(self, request, document_id):
        require_scope(request, "sessions:write")
        document = get_object_or_404(
            PurchasedTemplate.objects.select_related("template", "template__tool"),
            pk=document_id,
            buyer=request.api_customer,
            template__isnull=False,
        )
        origin, preview_mode, theme = _validate_edit_session_payload(
            request.data, request.api_key, request.api_customer
        )
        if preview_mode == EmbedSession.PreviewMode.PROTECTED:
            try:
                build_protected_preview(document.template)
            except ProtectedPreviewError as exc:
                raise ValidationError({"preview_mode": str(exc)}) from exc
        _ensure_embed_capacity(request.api_key)
        ttl = _embed_session_ttl(request.data)
        token, token_hash = generate_embed_token()
        prefill = {
            field["id"]: field.get("currentValue", field.get("defaultValue", ""))
            for field in (document.form_fields or [])
            if field.get("id")
        }
        session = EmbedSession.objects.create(
            user=request.api_customer,
            api_key=request.api_key,
            template=document.template,
            document=document,
            token_hash=token_hash,
            external_user_id=document.external_user_id,
            allowed_origin=origin,
            operation=EmbedSession.Operation.EDIT,
            mode=EmbedSession.Mode.TEST if document.test else EmbedSession.Mode.PAID,
            preview_mode=preview_mode,
            prefill=prefill,
            theme=theme,
            expires_at=timezone.now() + timedelta(minutes=ttl),
        )
        return Response(_embed_session_response(session, token), status=201)


class V1EmbedSessionRevokeView(V1ApiView):
    @extend_schema(
        operation_id="embed_sessions_destroy",
        summary="Revoke a pending hosted-form session",
        responses={204: None},
    )
    def delete(self, request, session_id):
        require_scope(request, "sessions:write")
        session = get_object_or_404(EmbedSession, pk=session_id, user=request.api_customer)
        if session.status == EmbedSession.Status.PENDING:
            session.status = EmbedSession.Status.REVOKED
            session.revoked_at = timezone.now()
            session.save(update_fields=["status", "revoked_at", "updated_at"])
        return Response(status=204)


def _embed_origin(request):
    raw_origin = request.headers.get("X-Embed-Origin", "")
    try:
        return normalize_origin(raw_origin)
    except ValueError as exc:
        raise PermissionDenied("The embed parent origin is missing or invalid.") from exc


def _embed_session(request, lock=False):
    authorization = request.headers.get("Authorization", "")
    scheme, separator, token = authorization.partition(" ")
    if separator != " " or scheme.lower() != "embed" or not token.startswith("stz_embed_"):
        raise AuthenticationFailed("Invalid embed session.")
    queryset = EmbedSession.objects.select_related("template", "template__tool", "document", "user", "api_key")
    if lock:
        queryset = queryset.select_for_update()
    try:
        session = queryset.get(token_hash=hash_secret(token.strip()))
    except EmbedSession.DoesNotExist as exc:
        raise AuthenticationFailed("Invalid embed session.") from exc
    origin = _embed_origin(request)
    if origin != session.allowed_origin:
        raise PermissionDenied("This embed session is not valid for the current website.")
    if session.status == EmbedSession.Status.REVOKED or session.revoked_at:
        raise AuthenticationFailed("This embed session was revoked.")
    if not SiteSettings.get_settings().enable_api_access:
        raise AuthenticationFailed("API access is unavailable.")
    if not has_active_api_entitlement(session.user):
        raise AuthenticationFailed("API access is unavailable.")
    if not session.api_key_id or not session.api_key.is_active:
        raise AuthenticationFailed("This embed session was revoked.")
    if session.expires_at <= timezone.now() and session.status != EmbedSession.Status.COMPLETED:
        EmbedSession.objects.filter(pk=session.pk, status=EmbedSession.Status.PENDING).update(status=EmbedSession.Status.EXPIRED)
        raise AuthenticationFailed("This embed session expired.")
    return session


class PublicEmbedSessionView(APIView):
    authentication_classes = []
    permission_classes = [AllowAny]

    @extend_schema(exclude=True)
    def get(self, request):
        session = _embed_session(request)
        template = session.template
        form_fields = (
            session.document.form_fields
            if session.operation == EmbedSession.Operation.EDIT and session.document_id
            else template.form_fields
        )
        protected_preview = None
        svg_url = None
        if session.preview_mode == EmbedSession.PreviewMode.PROTECTED:
            try:
                protected_preview = public_protected_manifest(
                    template,
                    lambda asset_id: request.build_absolute_uri(
                        reverse("public-embed-preview-asset", kwargs={"asset_id": asset_id})
                    ),
                )
            except ProtectedPreviewError as exc:
                raise ValidationError({"preview": str(exc)}) from exc
        else:
            svg_url = get_signed_url(template.svg_file) if template.svg_file else None
            if svg_url and svg_url.startswith("/"):
                svg_url = request.build_absolute_uri(svg_url)
        return Response({
            "id": str(session.id),
            "status": session.status,
            "operation": session.operation,
            "mode": session.mode,
            "preview_mode": session.preview_mode,
            "expires_at": session.expires_at,
            "theme": session.theme,
            "document_id": str(session.document_id) if session.document_id else None,
            "document_name": session.document.name if session.document_id else None,
            "template": {
                "id": str(template.id),
                "name": template.name,
                "version": int(template.updated_at.timestamp()),
                "form_fields": form_fields,
                "svg_url": svg_url,
                "svg_patches": template.svg_patches if not protected_preview else [],
                "fonts": FontSerializer(template.fonts.all(), many=True).data if not protected_preview else [],
                "protected_preview": protected_preview,
            },
            "prefill": session.prefill,
        })


class PublicEmbedPreviewAssetView(APIView):
    authentication_classes = []
    permission_classes = [AllowAny]

    @extend_schema(exclude=True)
    def get(self, request, asset_id):
        session = _embed_session(request)
        if session.preview_mode != EmbedSession.PreviewMode.PROTECTED:
            raise PermissionDenied("This embed session does not use protected preview assets.")
        try:
            path, content_type = protected_asset(session.template, asset_id)
        except ProtectedPreviewError as exc:
            raise ValidationError({"preview": str(exc)}) from exc
        response = FileResponse(path.open("rb"), content_type=content_type)
        response["Cache-Control"] = "private, max-age=300"
        response["Content-Length"] = str(path.stat().st_size)
        response["X-Content-Type-Options"] = "nosniff"
        response["Vary"] = "Authorization, X-Embed-Origin"
        return response


class PublicEmbedFinalizeView(APIView):
    authentication_classes = []
    permission_classes = [AllowAny]

    @transaction.atomic
    @extend_schema(exclude=True)
    def post(self, request):
        unlocked = _embed_session(request)
        session = EmbedSession.objects.select_for_update().select_related("template", "template__tool", "document", "user").get(pk=unlocked.pk)
        if session.status == EmbedSession.Status.COMPLETED and session.document_id:
            return Response({"document_id": str(session.document_id), "status": "completed"})
        if session.status != EmbedSession.Status.PENDING:
            raise ValidationError("This embed session cannot be completed.")
        if session.expires_at <= timezone.now():
            session.status = EmbedSession.Status.EXPIRED
            session.save(update_fields=["status", "updated_at"])
            raise AuthenticationFailed("This embed session expired.")
        if not isinstance(request.data, dict) or set(request.data) - {"values", "barcode_images", "name"}:
            raise ValidationError("Only values, barcode_images, and name may be submitted.")
        name = request.data.get("name")
        if name is not None and (not isinstance(name, str) or not name.strip() or len(name.strip()) > 255):
            raise ValidationError({"name": "A non-empty name up to 255 characters is required."})

        if session.operation == EmbedSession.Operation.EDIT:
            document = get_object_or_404(
                PurchasedTemplate.objects.select_for_update(),
                pk=session.document_id,
                buyer=session.user,
                template=session.template,
            )
            document.form_fields = apply_editable_updates(
                document.form_fields,
                request.data.get("values", {}),
                request.data.get("barcode_images"),
            )
            update_fields = ["form_fields", "updated_at"]
            if isinstance(name, str):
                document.name = name.strip()
                update_fields.append("name")
            document.save(update_fields=update_fields)
            session.status = EmbedSession.Status.COMPLETED
            session.completed_at = timezone.now()
            session.save(update_fields=["status", "completed_at", "updated_at"])
            return Response({"document_id": str(document.id), "status": "completed"})

        compiled_fields, tracking_id = compile_document_fields(
            session.template.form_fields,
            request.data.get("values", {}),
            request.data.get("barcode_images"),
        )
        if session.mode == EmbedSession.Mode.PAID:
            price = _api_price(session.template)
            if price > 0:
                wallet = Wallet.objects.select_for_update().get(user=session.user)
                try:
                    wallet.debit(price, description=f"Embedded document: {session.template.name}")
                except Exception as exc:
                    from django.core.exceptions import ValidationError as DjangoValidationError

                    if isinstance(exc, DjangoValidationError):
                        raise ValidationError({"wallet": "The API customer's wallet balance is insufficient."}) from exc
                    raise
        document = PurchasedTemplate.objects.create(
            buyer=session.user,
            template=session.template,
            external_user_id=session.external_user_id,
            name=name.strip() if isinstance(name, str) else f"My {session.template.name}",
            test=session.mode == EmbedSession.Mode.TEST,
            tracking_id=tracking_id,
            form_fields=compiled_fields,
            svg_patches=list(session.template.svg_patches or []),
            keywords=list(session.template.keywords or []),
        )
        if session.template.fonts.exists():
            document.fonts.set(session.template.fonts.all())
        session.document = document
        session.status = EmbedSession.Status.COMPLETED
        session.completed_at = timezone.now()
        session.save(update_fields=["document", "status", "completed_at", "updated_at"])
        return Response({"document_id": str(document.id), "status": "completed"}, status=201)

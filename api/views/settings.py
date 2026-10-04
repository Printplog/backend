from django.db import transaction
from rest_framework import status, viewsets
from rest_framework.permissions import IsAuthenticated, AllowAny
from rest_framework.response import Response

from accounts.two_factor import is_enabled_for_user, verify_settings_totp_code

from ..models import SiteSettings
from ..serializers import SiteSettingsSerializer, PublicSiteSettingsSerializer
from ..utils.integration_secrets import (
    IntegrationSecretError,
    SECRET_FIELDS,
    set_integration_secret,
    validate_integration_secret,
)

class SiteSettingsViewSet(viewsets.ViewSet):
    """
    Site configuration protected by a fresh admin authenticator code.
    """
    def get_throttles(self):
        self.throttle_scope = 'admin_2fa' if self.action == 'partial_update' else None
        return super().get_throttles()
    def get_permissions(self):
        if self.action == 'list':
            return [AllowAny()]
        return [IsAuthenticated()]

    def get_serializer_class(self):
        if self.request.user and self.request.user.is_superuser:
            return SiteSettingsSerializer
        return PublicSiteSettingsSerializer

    def get_object(self):
        return SiteSettings.get_settings()

    def list(self, request):
        settings_obj = self.get_object()
        serializer_class = self.get_serializer_class()
        serializer = serializer_class(settings_obj)
        return Response(serializer.data)

    @transaction.atomic
    def partial_update(self, request, pk=None):
        if not request.user.is_superuser:
            return Response({"error": "Unauthorized"}, status=status.HTTP_403_FORBIDDEN)

        settings_obj = self.get_object()

        two_factor_code = str(request.data.get('two_factor_code', '')).strip()
        if not two_factor_code:
            return Response(
                {"error": "Authenticator code is required."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        if not is_enabled_for_user(request.user):
            return Response(
                {"error": "Two-factor authentication is not configured for this admin account."},
                status=status.HTTP_403_FORBIDDEN,
            )
        settings_data = request.data.copy()
        settings_data.pop('two_factor_code', None)
        secret_values = {}
        for field_name in SECRET_FIELDS:
            raw_value = settings_data.pop(field_name, None)
            if raw_value is None or not str(raw_value).strip():
                continue
            try:
                secret_values[field_name] = validate_integration_secret(field_name, raw_value)
            except IntegrationSecretError as exc:
                return Response({field_name: [str(exc)]}, status=status.HTTP_400_BAD_REQUEST)

        serializer = SiteSettingsSerializer(settings_obj, data=settings_data, partial=True)
        if serializer.is_valid():
            if not verify_settings_totp_code(request.user, two_factor_code):
                return Response(
                    {"error": "Invalid or already-used authenticator code."},
                    status=status.HTTP_403_FORBIDDEN,
                )
            serializer.save()
            for field_name, secret_value in secret_values.items():
                set_integration_secret(field_name, secret_value, updated_by=request.user)
            
            # Log action
            from analytics.utils import log_action
            log_action(
                actor=request.user,
                action="UPDATE_SETTINGS",
                target="Site Settings",
                ip_address=request.META.get('REMOTE_ADDR'),
                details={
                    **{key: value for key, value in settings_data.items()},
                    "integration_secrets_updated": sorted(secret_values),
                }
            )
            
            return Response(serializer.data)
        return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)

from rest_framework import viewsets, status
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.decorators import action
from django.core.cache import cache
from django.core.mail import send_mail
from django.conf import settings
import random

from ..models import SiteSettings
from ..serializers import SiteSettingsSerializer

class SiteSettingsViewSet(viewsets.ViewSet):
    """
    ViewSet for site configuration protected by email OTP (5-minute expiry).
    """
    permission_classes = [IsAuthenticated]

    def get_object(self):
        return SiteSettings.get_settings()

    def list(self, request):
        settings_obj = self.get_object()
        serializer = SiteSettingsSerializer(settings_obj)
        return Response(serializer.data)

    @action(detail=False, methods=['post'], url_path='request-code')
    def request_code(self, request):
        if not request.user.is_superuser:
            return Response({"error": "Unauthorized"}, status=status.HTTP_403_FORBIDDEN)
        
        # Generate 6-digit code
        code = ''.join([str(random.randint(0, 9)) for _ in range(6)])
        
        # Store in cache for 5 minutes (300 seconds)
        cache_key = f"admin_settings_otp_{request.user.id}"
        cache.set(cache_key, code, 300)
        
        # DEV MODE CONVENIENCE: Print to console
        if settings.DEBUG:
            print(f"=========================================")
            print(f"🔒 DEV MODE - ADMIN OTP CODE: {code}")
            print(f"=========================================")
        
        # Gather all superuser emails
        from django.contrib.auth import get_user_model
        User = get_user_model()
        superusers = User.objects.filter(is_superuser=True)
        recipient_list = [user.email for user in superusers if user.email]
        
        # Fallback if no superusers have emails
        if not recipient_list:
            if hasattr(settings, 'EMAIL_HOST_USER') and settings.EMAIL_HOST_USER:
                recipient_list = [settings.EMAIL_HOST_USER]
            else:
                recipient_list = ["corehiseven@gmail.com"] # Original fallback

        # Send email
        try:
            subject = "SECURITY ALERT: Admin Settings Modification Attempt"
            message = (
                f"Attention Admin,\n\n"
                f"User '{request.user.username}' ({request.user.email}) is attempting to modify the global Site Settings.\n\n"
                f"If this is authorized, here is the verification code: {code}\n"
                f"This code will expire in 5 minutes.\n\n"
                f"If you did not authorize this change, please check your system logs immediately."
            )
            from_email = settings.DEFAULT_FROM_EMAIL
            send_mail(subject, message, from_email, recipient_list)
            return Response({"message": "Verification code sent to email."})
        except Exception as e:
            import logging
            logger = logging.getLogger(__name__)
            logger.error(f"Failed to send verification email: {str(e)}")
            return Response({"error": f"Failed to send email: {str(e)}"}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)

    def partial_update(self, request, pk=None):
        if not request.user.is_superuser:
            return Response({"error": "Unauthorized"}, status=status.HTTP_403_FORBIDDEN)

        settings_obj = self.get_object()
        
        # Verification Step: OTP
        otp = request.data.get('otp', '').strip()

        # OTP Verification from cache
        cache_key = f"admin_settings_otp_{request.user.id}"
        cached_otp = cache.get(cache_key)
        
        if not otp:
            return Response({"error": "Verification code is required."}, status=status.HTTP_400_BAD_REQUEST)
        
        if not cached_otp or otp != cached_otp:
            return Response({"error": "Invalid or expired verification code."}, status=status.HTTP_403_FORBIDDEN)

        # Clear OTP after successful use
        cache.delete(cache_key)

        # Apply updates
        serializer = SiteSettingsSerializer(settings_obj, data=request.data, partial=True)
        if serializer.is_valid():
            serializer.save()
            
            # Log action
            from analytics.utils import log_action
            log_action(
                actor=request.user,
                action="UPDATE_SETTINGS",
                target="Site Settings",
                ip_address=request.META.get('REMOTE_ADDR'),
                details=request.data
            )
            
            return Response(serializer.data)
        return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)

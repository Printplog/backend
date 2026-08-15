# accounts/views.py
from dj_rest_auth.views import LoginView as BaseLoginView, LogoutView as BaseLogoutView
from rest_framework_simplejwt.tokens import RefreshToken
from django.http import JsonResponse
from django.contrib.auth import logout as django_logout
from rest_framework.views import APIView
from rest_framework.response import Response
from rest_framework import status, viewsets
from django.conf import settings
from .authentication import enforce_csrf_for_request
from django.middleware.csrf import get_token
from .serializers import *
from rest_framework.permissions import IsAuthenticated, AllowAny
from rest_framework.throttling import ScopedRateThrottle
from django.shortcuts import get_object_or_404
from django.contrib.auth.tokens import PasswordResetTokenGenerator
from django.core.mail import send_mail
from django.utils.http import urlsafe_base64_encode
from django.utils.encoding import force_bytes
from django.views import View
from django.http import JsonResponse
from django.conf import settings


class RegisterView(APIView):
    permission_classes = [AllowAny]
    authentication_classes = []
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = 'auth_register'
    def post(self, request):
        enforce_csrf_for_request(request)
        serializer = RegisterSerializer(data=request.data, context={'request': request})
        if serializer.is_valid():
            user = serializer.save()
            
            # Send Welcome Email
            from api.utils.email_service import EmailService
            EmailService.send_welcome_email(user)

            return Response({
                "detail": "Registration successful",
                "email": user.email, # type: ignore
            }, status=status.HTTP_201_CREATED)

        return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)


class CsrfTokenView(APIView):
    permission_classes = [AllowAny]
    authentication_classes = []

    def get(self, request):
        return Response({"csrfToken": get_token(request)})
    
class LoginView(BaseLoginView):
    permission_classes = [AllowAny]
    authentication_classes = []
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = 'auth_login'

    def post(self, request, *args, **kwargs):
        enforce_csrf_for_request(request)
        return super().post(request, *args, **kwargs)

    def get_response(self): # type: ignore
        super().get_response()  # This sets the cookies or does other side-effects if needed

        if not self.user:
            return JsonResponse({'error': 'Authentication failed'}, status=401)

        refresh = RefreshToken.for_user(self.user)
        access_token = str(refresh.access_token)
        refresh_token = str(refresh)

        # Serialize user data
        user = CustomUserDetailsSerializer(self.user, many=False)

        # Get cookie settings from settings.py
        max_age = 2 * 24 * 60 * 60  # 2 days in seconds
        cookie_settings = {
            'httponly': settings.JWT_COOKIE_HTTPONLY,
            'secure': settings.JWT_COOKIE_SECURE,
            'samesite': settings.JWT_COOKIE_SAMESITE,
            'path': settings.JWT_COOKIE_PATH,
            'max_age': max_age,
        }

        if hasattr(settings, 'JWT_COOKIE_DOMAIN') and settings.JWT_COOKIE_DOMAIN:
            cookie_settings['domain'] = settings.JWT_COOKIE_DOMAIN

        # Set access token cookie
        response = JsonResponse(user.data)
        response.set_cookie(
            key='access_token',
            value=access_token,
            **cookie_settings
        )

        # Set refresh token cookie
        response.set_cookie(
            key='refresh_token',
            value=refresh_token,
            **cookie_settings
        )

        # Get IP and User-Agent for login alert
        x_forwarded_for = self.request.META.get('HTTP_X_FORWARDED_FOR')
        if x_forwarded_for:
            ip = x_forwarded_for.split(',')[0]
        else:
            ip = self.request.META.get('REMOTE_ADDR')
        
        user_agent = self.request.META.get('HTTP_USER_AGENT', 'Unknown')

        # Send Login notification
        from api.utils.email_service import EmailService
        EmailService.send_login_notification(self.user, ip, user_agent)

        return response

class ForgotPasswordView(APIView):
    permission_classes = [AllowAny]
    authentication_classes = []
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = 'auth_password'
    def post(self, request):
        enforce_csrf_for_request(request)
        serializer = ForgotPasswordSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        email = serializer.validated_data["email"] # type: ignore
        user = User.objects.filter(email=email).first()

        if user:
            token = PasswordResetTokenGenerator().make_token(user)
            uid = urlsafe_base64_encode(force_bytes(user.pk))

            reset_url = f"{settings.FRONTEND_URL}/auth/reset-password?uid={uid}&token={token}"

            from api.utils.email_service import EmailService
            EmailService.send_password_reset(email, reset_url)

        return Response({"detail": "If this email exists, a reset link will be sent."}, status=status.HTTP_200_OK)

class ResetPasswordConfirmView(APIView):
    permission_classes = [AllowAny]
    authentication_classes = []
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = 'auth_password'
    def post(self, request):
        enforce_csrf_for_request(request)
        serializer = ResetPasswordConfirmSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        serializer.save()
        return Response({"detail": "Password reset successful."}, status=status.HTTP_200_OK)

class ChangePasswordView(APIView):
    permission_classes = [IsAuthenticated]
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = 'auth_password'

    def post(self, request):
        serializer = ChangePasswordSerializer(data=request.data, context={'request': request})
        serializer.is_valid(raise_exception=True)

        request.user.set_password(serializer.validated_data['new_password']) # type: ignore
        request.user.save()

        return Response({"detail": "Password changed successfully."})

class LogoutView(APIView):
    
    def post(self, request, *args, **kwargs):
        # Get refresh token from cookie
        refresh_token = request.COOKIES.get('refresh_token')

        # Optional: Blacklist the refresh token if using token revocation
        if refresh_token:
            try:
                token = RefreshToken(refresh_token)
                token.blacklist()
            except Exception:
                pass  # Token may already be expired or invalid

        # Prepare response
        response = JsonResponse({"detail": "Successfully logged out."})
        
        # Delete cookies
        response.delete_cookie('access_token', path='/', samesite=settings.JWT_COOKIE_SAMESITE)
        response.delete_cookie('refresh_token', path='/', samesite=settings.JWT_COOKIE_SAMESITE)

        # Django logout (for session-based auth fallback)
        django_logout(request)

        return response
    
class GoogleAuthView(APIView):
    permission_classes = [AllowAny]
    authentication_classes = []
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = 'auth_login'

    def post(self, request):
        enforce_csrf_for_request(request)
        access_token = request.data.get('access_token')
        if not access_token:
            return Response({'error': 'Google access_token is required'}, status=status.HTTP_400_BAD_REQUEST)

        import requests as http_requests
        google_resp = http_requests.get(
            'https://www.googleapis.com/oauth2/v3/userinfo',
            params={'access_token': access_token},
            timeout=5,
        )
        if google_resp.status_code != 200:
            return Response({'error': 'Invalid Google token'}, status=status.HTTP_400_BAD_REQUEST)

        idinfo = google_resp.json()
        google_id = idinfo.get('sub')
        email = idinfo.get('email', '')

        if not google_id or not email:
            return Response({'error': 'Invalid Google token'}, status=status.HTTP_400_BAD_REQUEST)

        user = User.objects.filter(google_id=google_id).first()
        if not user:
            user = User.objects.filter(email=email).first()

        if not user:
            # New user — derive a unique username from their email
            base_username = email.split('@')[0].lower().replace('.', '_')[:30]
            username = base_username
            suffix = 1
            while User.objects.filter(username=username).exists():
                username = f"{base_username}{suffix}"
                suffix += 1

            from analytics.utils import get_attribution_for_request
            attribution = get_attribution_for_request(request)

            user = User(
                username=username,
                email=email,
                google_id=google_id,
                source=attribution.get('source', 'google'),
                medium=attribution.get('medium', 'oauth'),
                campaign=attribution.get('campaign'),
            )
            user.set_unusable_password()
            user.save()

            from api.utils.email_service import EmailService
            EmailService.send_welcome_email(user)

        if not user.is_active:
            return Response({'error': 'This account has been disabled.'}, status=status.HTTP_403_FORBIDDEN)

        # Link google_id if not already set (existing email/password user signing in with Google)
        if not user.google_id:
            user.google_id = google_id
            user.save(update_fields=['google_id'])

        refresh = RefreshToken.for_user(user)
        access_token = str(refresh.access_token)
        refresh_token_str = str(refresh)

        user_data = CustomUserDetailsSerializer(user, many=False)

        max_age = 2 * 24 * 60 * 60
        cookie_settings = {
            'httponly': settings.JWT_COOKIE_HTTPONLY,
            'secure': settings.JWT_COOKIE_SECURE,
            'samesite': settings.JWT_COOKIE_SAMESITE,
            'path': settings.JWT_COOKIE_PATH,
            'max_age': max_age,
        }
        if hasattr(settings, 'JWT_COOKIE_DOMAIN') and settings.JWT_COOKIE_DOMAIN:
            cookie_settings['domain'] = settings.JWT_COOKIE_DOMAIN

        response = JsonResponse(user_data.data)
        response.set_cookie(key='access_token', value=access_token, **cookie_settings)
        response.set_cookie(key='refresh_token', value=refresh_token_str, **cookie_settings)
        return response


class RefreshTokenView(APIView):
    permission_classes = []
    authentication_classes = []
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = 'auth_login'

    def post(self, request):
        enforce_csrf_for_request(request)
        refresh_token = request.COOKIES.get('refresh_token')

        if not refresh_token:
            return Response(
                {"detail": "Refresh token not found"},
                status=status.HTTP_401_UNAUTHORIZED
            )

        try:
            refresh = RefreshToken(refresh_token)
            new_access_token = str(refresh.access_token)
        except Exception as e:
            return Response(
                {"detail": "Invalid or expired refresh token"},
                status=status.HTTP_401_UNAUTHORIZED
            )

        # Get cookie settings from settings.py (same as LoginView)
        max_age = 2 * 24 * 60 * 60  # 2 days in seconds
        cookie_settings = {
            'httponly': settings.JWT_COOKIE_HTTPONLY,
            'secure': settings.JWT_COOKIE_SECURE,
            'samesite': settings.JWT_COOKIE_SAMESITE,
            'path': settings.JWT_COOKIE_PATH,
            'max_age': max_age,
        }

        if hasattr(settings, 'JWT_COOKIE_DOMAIN') and settings.JWT_COOKIE_DOMAIN:
            cookie_settings['domain'] = settings.JWT_COOKIE_DOMAIN

        response = JsonResponse({
            "detail": "Access token refreshed",
            "access_token": new_access_token
        })
        
        response.set_cookie(
            key='access_token',
            value=new_access_token,
            **cookie_settings
        )
        
        return response
    

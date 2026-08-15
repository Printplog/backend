# accounts/views.py
from dj_rest_auth.views import LoginView as BaseLoginView
from rest_framework_simplejwt.tokens import RefreshToken
from django.http import JsonResponse
from django.contrib.auth import logout as django_logout
from django.contrib.auth import get_user_model
from rest_framework.views import APIView
from rest_framework.response import Response
from rest_framework import status
from django.conf import settings
from .authentication import enforce_csrf_for_request
from django.middleware.csrf import get_token
from .serializers import *
from rest_framework.permissions import IsAuthenticated, AllowAny
from rest_framework.throttling import ScopedRateThrottle
from django.contrib.auth.tokens import PasswordResetTokenGenerator
from django.utils.http import urlsafe_base64_encode
from django.utils.encoding import force_bytes
import time
from .two_factor import (
    TwoFactorChallengeError,
    clear_challenge_cookie,
    consume_challenge,
    create_challenge,
    decrypt_secret,
    enroll_user,
    ensure_pending_secret,
    is_enabled_for_user,
    is_privileged_user,
    load_challenge,
    profile_for_user,
    provisioning_uri,
    register_failed_attempt,
    set_challenge_cookie,
    verify_user_code,
)


User = get_user_model()


def _jwt_cookie_settings():
    cookie_settings = {
        'httponly': settings.JWT_COOKIE_HTTPONLY,
        'secure': settings.JWT_COOKIE_SECURE,
        'samesite': settings.JWT_COOKIE_SAMESITE,
        'path': settings.JWT_COOKIE_PATH,
        'max_age': 2 * 24 * 60 * 60,
    }
    if getattr(settings, 'JWT_COOKIE_DOMAIN', None):
        cookie_settings['domain'] = settings.JWT_COOKIE_DOMAIN
    return cookie_settings


def _clear_auth_cookies(response):
    cookie_options = {
        'path': settings.JWT_COOKIE_PATH,
        'samesite': settings.JWT_COOKIE_SAMESITE,
    }
    if getattr(settings, 'JWT_COOKIE_DOMAIN', None):
        cookie_options['domain'] = settings.JWT_COOKIE_DOMAIN
    response.delete_cookie('access_token', **cookie_options)
    response.delete_cookie('refresh_token', **cookie_options)


def _notify_login(user, request):
    ip = request.META.get('REMOTE_ADDR')
    user_agent = request.META.get('HTTP_USER_AGENT', 'Unknown')
    from api.utils.email_service import EmailService
    EmailService.send_login_notification(user, ip, user_agent)


def _authenticated_response(user, request, *, admin_mfa=False, recovery_codes=None, notify=True):
    refresh = RefreshToken.for_user(user)
    if admin_mfa:
        refresh['admin_mfa'] = True
        refresh['amr'] = ['password', 'otp']

    payload = dict(CustomUserDetailsSerializer(user, many=False).data)
    if recovery_codes:
        payload['recovery_codes'] = recovery_codes

    response = JsonResponse(payload)
    cookie_settings = _jwt_cookie_settings()
    response.set_cookie('access_token', str(refresh.access_token), **cookie_settings)
    response.set_cookie('refresh_token', str(refresh), **cookie_settings)
    response['Cache-Control'] = 'no-store'
    if notify:
        _notify_login(user, request)
    return response


def _admin_challenge_response(user, request):
    token = create_challenge(user, request)
    response = JsonResponse({
        'requires_two_factor': True,
        'setup_required': not is_enabled_for_user(user),
        'detail': 'Admin verification required.',
    })
    _clear_auth_cookies(response)
    set_challenge_cookie(response, token)
    response['Cache-Control'] = 'no-store'
    return response


def _challenge_error_response(message, *, clear_cookie=False):
    response = Response({'detail': message}, status=status.HTTP_400_BAD_REQUEST)
    response['Cache-Control'] = 'no-store'
    if clear_cookie:
        clear_challenge_cookie(response)
    return response


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
        if not self.user:
            return JsonResponse({'error': 'Authentication failed'}, status=401)

        if is_privileged_user(self.user):
            return _admin_challenge_response(self.user, self.request)

        super().get_response()
        return _authenticated_response(self.user, self.request)


class AdminTwoFactorSetupView(APIView):
    permission_classes = [AllowAny]
    authentication_classes = []
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = 'admin_2fa'

    def post(self, request):
        enforce_csrf_for_request(request)
        try:
            challenge = load_challenge(request)
            user = User.objects.get(pk=challenge.user_id, is_active=True)
        except (TwoFactorChallengeError, User.DoesNotExist) as exc:
            return _challenge_error_response(str(exc), clear_cookie=True)

        if not is_privileged_user(user):
            consume_challenge(challenge)
            return _challenge_error_response('This verification session is no longer valid.', clear_cookie=True)
        if profile_for_user(user):
            return _challenge_error_response('Two-factor authentication is already configured.')

        challenge, secret = ensure_pending_secret(challenge)
        response = Response({
            'provisioning_uri': provisioning_uri(user, secret),
            'manual_key': secret,
            'issuer': settings.ADMIN_2FA_ISSUER,
            'expires_in': max(0, challenge.expires_at - int(time.time())),
        })
        response['Cache-Control'] = 'no-store'
        return response


class AdminTwoFactorVerifyView(APIView):
    permission_classes = [AllowAny]
    authentication_classes = []
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = 'admin_2fa'

    def post(self, request):
        enforce_csrf_for_request(request)
        code = str(request.data.get('code', '')).strip()
        if not code:
            return _challenge_error_response('Enter your authenticator or recovery code.')

        try:
            challenge = load_challenge(request)
            user = User.objects.get(pk=challenge.user_id, is_active=True)
        except (TwoFactorChallengeError, User.DoesNotExist) as exc:
            return _challenge_error_response(str(exc), clear_cookie=True)

        if not is_privileged_user(user):
            consume_challenge(challenge)
            return _challenge_error_response('This verification session is no longer valid.', clear_cookie=True)

        recovery_codes = None
        profile = profile_for_user(user)
        if profile:
            verified = verify_user_code(user, code)
        elif challenge.pending_secret:
            recovery_codes = enroll_user(user, decrypt_secret(challenge.pending_secret), code)
            verified = recovery_codes is not None
        else:
            return _challenge_error_response('Set up your authenticator before verifying.')

        if not verified:
            try:
                register_failed_attempt(challenge)
            except TwoFactorChallengeError as exc:
                return _challenge_error_response(str(exc), clear_cookie=True)
            return _challenge_error_response('That code is incorrect or has already been used.')

        consume_challenge(challenge)
        response = _authenticated_response(
            user,
            request,
            admin_mfa=True,
            recovery_codes=recovery_codes,
        )
        clear_challenge_cookie(response)
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

        if not google_id or not email or idinfo.get('email_verified') is not True:
            return Response({'error': 'Invalid Google token'}, status=status.HTTP_400_BAD_REQUEST)

        user = User.objects.filter(google_id=google_id).first()
        if not user:
            email_user = User.objects.filter(email=email).first()
            if email_user and is_privileged_user(email_user):
                return Response(
                    {'error': 'Privileged accounts must use their password to sign in.'},
                    status=status.HTTP_403_FORBIDDEN,
                )
            user = email_user

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

        if is_privileged_user(user):
            return _admin_challenge_response(user, request)
        return _authenticated_response(user, request, notify=False)


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
            user = User.objects.filter(pk=refresh.get('user_id'), is_active=True).first()
            if not user:
                raise ValueError('Unknown user')
            if is_privileged_user(user):
                if refresh.get('admin_mfa') is not True or not is_enabled_for_user(user):
                    raise ValueError('Admin two-factor verification required')
            new_access_token = str(refresh.access_token)
        except Exception:
            return Response(
                {"detail": "Invalid or expired refresh token"},
                status=status.HTTP_401_UNAUTHORIZED
            )

        # Get cookie settings from settings.py (same as LoginView)
        cookie_settings = _jwt_cookie_settings()

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
    

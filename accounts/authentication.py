# accounts/authentication.py
import hashlib
import logging
from rest_framework_simplejwt.authentication import JWTAuthentication
from rest_framework_simplejwt.exceptions import InvalidToken, AuthenticationFailed
from rest_framework.authentication import CSRFCheck
from rest_framework.exceptions import PermissionDenied
from django.utils.translation import gettext_lazy as _
from django.contrib.auth.middleware import get_user
from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.http import JsonResponse
from django.contrib.auth.models import AnonymousUser
from channels.db import database_sync_to_async
from channels.middleware import BaseMiddleware
from analytics.utils import is_bot_user_agent
from .two_factor import is_enabled_for_user, is_privileged_user

# Short-lived cache for resolved WS-auth users.
# A reconnect storm of 10 attempts on the same access_token now costs 1 DB hit
# (or 0, on hits within the TTL). TTL bounds staleness for revoked tokens.
WS_AUTH_CACHE_TTL = 30  # seconds
_WS_AUTH_SENTINEL_ANON = "anon"

logger = logging.getLogger(__name__)


def enforce_csrf_for_request(request):
    """Apply Django's CSRF validation to DRF APIViews, which are csrf_exempt by default."""
    check = CSRFCheck(lambda django_request: None)
    django_request = getattr(request, "_request", request)
    check.process_request(django_request)
    reason = check.process_view(django_request, None, (), {})
    if reason:
        raise PermissionDenied(f"CSRF validation failed: {reason}")

class JWTAuthenticationFromCookies(JWTAuthentication):
    def authenticate(self, request):
        access_token = request.COOKIES.get('access_token')
        if not access_token:
            return None

        validated_token = self.get_validated_token(access_token)
        user = self.get_user(validated_token)

        if not user or not user.is_active:
            raise AuthenticationFailed(_('User is inactive or deleted.'))

        if is_privileged_user(user):
            if validated_token.get('admin_mfa') is not True or not is_enabled_for_user(user):
                raise AuthenticationFailed(_('Admin two-factor authentication required.'))

        if request.method not in {"GET", "HEAD", "OPTIONS", "TRACE"}:
            enforce_csrf_for_request(request)

        return (user, validated_token)

    @staticmethod
    def enforce_csrf(request):
        enforce_csrf_for_request(request)


def _parse_cookies_from_headers(scope):
    """Parse cookies directly from raw WebSocket scope headers."""
    headers = dict(scope.get('headers', []))
    cookie_header = headers.get(b'cookie', b'')
    if isinstance(cookie_header, bytes):
        cookie_header = cookie_header.decode('latin-1')
    cookies = {}
    for part in cookie_header.split(';'):
        part = part.strip()
        if '=' in part:
            key, _, value = part.partition('=')
            cookies[key.strip()] = value.strip()
    return cookies


@database_sync_to_async
def get_user_from_jwt_cookie(scope):
    # Identify bots immediately so we never even hit the DB for them.
    headers = dict(scope.get('headers', []))
    ua = headers.get(b'user-agent', b'').decode('utf-8', errors='ignore')
    if is_bot_user_agent(ua):
        return AnonymousUser()

    # Prefer CookieMiddleware-parsed cookies; fall back to parsing headers directly.
    cookies = scope.get('cookies') or _parse_cookies_from_headers(scope)
    access_token = cookies.get('access_token')
    if not access_token:
        return AnonymousUser()

    # Cache by token-digest so a reconnect storm (10 attempts × N sockets per tab)
    # against the same cookie costs at most 1 JWT-validate + 1 SELECT user.
    cache_key = f"wsjwt:{hashlib.sha256(access_token.encode()).hexdigest()[:24]}"
    cached = cache.get(cache_key)
    if cached == _WS_AUTH_SENTINEL_ANON:
        return AnonymousUser()
    if cached:
        User = get_user_model()
        user = User.objects.filter(pk=cached, is_active=True).only(
            'id', 'username', 'is_active', 'is_staff', 'is_superuser'
        ).first()
        if user:
            return user
        # cached pk no longer valid — fall through to re-validate

    try:
        auth = JWTAuthentication()
        validated_token = auth.get_validated_token(access_token)
        user = auth.get_user(validated_token)
        if user and user.is_active:
            if is_privileged_user(user):
                if validated_token.get('admin_mfa') is not True or not is_enabled_for_user(user):
                    cache.set(cache_key, _WS_AUTH_SENTINEL_ANON, timeout=WS_AUTH_CACHE_TTL)
                    return AnonymousUser()
            cache.set(cache_key, user.pk, timeout=WS_AUTH_CACHE_TTL)
            return user
        cache.set(cache_key, _WS_AUTH_SENTINEL_ANON, timeout=WS_AUTH_CACHE_TTL)
        return AnonymousUser()
    except Exception as e:
        logger.warning('[WS Auth] JWT validation failed for WebSocket: %s', e)
        cache.set(cache_key, _WS_AUTH_SENTINEL_ANON, timeout=WS_AUTH_CACHE_TTL)
        return AnonymousUser()


class JWTAuthMiddleware(BaseMiddleware):
    async def __call__(self, scope, receive, send):
        if scope['type'] == 'websocket':
            scope['user'] = await get_user_from_jwt_cookie(scope)
        await super().__call__(scope, receive, send)


def JWTAuthMiddlewareStack(inner):
    from channels.sessions import CookieMiddleware
    return CookieMiddleware(JWTAuthMiddleware(inner))

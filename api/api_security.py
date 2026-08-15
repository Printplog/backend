import hashlib
import hmac
import secrets
from datetime import timedelta
from urllib.parse import urlsplit

from django.conf import settings
from django.utils import timezone
from rest_framework import exceptions
from rest_framework.authentication import BaseAuthentication, get_authorization_header

from .models import ApiEntitlement, ApiKey, SiteSettings


API_KEY_SCOPES = frozenset({
    "templates:read",
    "documents:read",
    "documents:write",
    "sessions:write",
})

DEFAULT_API_KEY_SCOPES = [
    "templates:read",
    "documents:read",
    "documents:write",
    "sessions:write",
]

THEME_DEFAULTS = {
    "primaryColor": "#cee88c",
    "backgroundColor": "#10120f",
    "textColor": "#ffffff",
    "inputBackground": "#191c17",
    "borderColor": "#34382f",
    "borderRadius": "12px",
    "fontFamily": "Inter",
    "buttonText": "Create document",
    "appearance": "dark",
    "showSharpToolzBranding": True,
}

_THEME_KEYS = frozenset(THEME_DEFAULTS)
_COLOR_KEYS = frozenset({
    "primaryColor",
    "backgroundColor",
    "textColor",
    "inputBackground",
    "borderColor",
})


def _pepper() -> bytes:
    value = getattr(settings, "API_KEY_PEPPER", None) or settings.SECRET_KEY
    return value.encode("utf-8")


def hash_secret(value: str) -> str:
    return hmac.new(_pepper(), value.encode("utf-8"), hashlib.sha256).hexdigest()


def generate_api_key() -> tuple[str, str, str]:
    prefix = f"stz_live_{secrets.token_hex(6)}"
    token = f"{prefix}.{secrets.token_urlsafe(32)}"
    return token, prefix, hash_secret(token)


def generate_embed_token() -> tuple[str, str]:
    token = f"stz_embed_{secrets.token_urlsafe(32)}"
    return token, hash_secret(token)


def normalize_origin(value: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError("An allowed origin is required.")
    raw = value.strip()
    if "*" in raw:
        raise ValueError("Wildcard origins are not allowed.")
    parsed = urlsplit(raw)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise ValueError("Origin must be an absolute http or https URL.")
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise ValueError("Origin cannot contain credentials, a query, or a fragment.")
    if parsed.path not in {"", "/"}:
        raise ValueError("Origin cannot contain a path.")
    hostname = parsed.hostname.lower().encode("idna").decode("ascii")
    if ":" in hostname:
        hostname = f"[{hostname}]"
    try:
        port = parsed.port
    except ValueError as exc:
        raise ValueError("Origin contains an invalid port.") from exc
    default_port = (parsed.scheme == "https" and port == 443) or (parsed.scheme == "http" and port == 80)
    port_part = "" if port is None or default_port else f":{port}"
    return f"{parsed.scheme}://{hostname}{port_part}"


def normalize_origins(values) -> list[str]:
    if values is None:
        return []
    if not isinstance(values, list):
        raise ValueError("allowed_origins must be a list.")
    if len(values) > 25:
        raise ValueError("A maximum of 25 origins is allowed.")
    return list(dict.fromkeys(normalize_origin(value) for value in values))


def validate_theme(value) -> dict:
    if value is None:
        return dict(THEME_DEFAULTS)
    if not isinstance(value, dict):
        raise ValueError("theme must be an object.")
    unknown = set(value) - _THEME_KEYS
    if unknown:
        raise ValueError(f"Unsupported theme fields: {', '.join(sorted(unknown))}.")
    theme = {**THEME_DEFAULTS, **value}
    for key in _COLOR_KEYS:
        color = theme[key]
        if not isinstance(color, str) or len(color) != 7 or color[0] != "#":
            raise ValueError(f"{key} must be a six-digit hex color.")
        try:
            int(color[1:], 16)
        except ValueError as exc:
            raise ValueError(f"{key} must be a six-digit hex color.") from exc
        theme[key] = color.lower()
    radius = theme["borderRadius"]
    if not isinstance(radius, str) or not radius.endswith("px"):
        raise ValueError("borderRadius must use px units.")
    try:
        radius_value = int(radius[:-2])
    except ValueError as exc:
        raise ValueError("borderRadius must use an integer px value.") from exc
    if not 0 <= radius_value <= 32:
        raise ValueError("borderRadius must be between 0px and 32px.")
    if theme["appearance"] not in {"dark", "light"}:
        raise ValueError("appearance must be dark or light.")
    if not isinstance(theme["showSharpToolzBranding"], bool):
        raise ValueError("showSharpToolzBranding must be a boolean.")
    for key, maximum in (("fontFamily", 80), ("buttonText", 80)):
        if not isinstance(theme[key], str) or not theme[key].strip() or len(theme[key]) > maximum:
            raise ValueError(f"{key} must be a non-empty string up to {maximum} characters.")
        theme[key] = theme[key].strip()
    return theme


def has_active_api_entitlement(user) -> bool:
    return ApiEntitlement.objects.filter(user=user, status=ApiEntitlement.Status.ACTIVE).exists()


class ApiKeyAuthentication(BaseAuthentication):
    """Authenticate only high-entropy SharpToolz API keys, never browser JWT cookies."""

    def authenticate(self, request):
        header = get_authorization_header(request).decode("utf-8")
        if not header:
            return None
        scheme, separator, token = header.partition(" ")
        if separator != " " or scheme.lower() != "bearer" or not token.startswith("stz_live_"):
            raise exceptions.AuthenticationFailed("Invalid API credentials.")

        key_hash = hash_secret(token.strip())
        try:
            api_key = ApiKey.objects.select_related("user").get(secret_hash=key_hash)
        except ApiKey.DoesNotExist as exc:
            raise exceptions.AuthenticationFailed("Invalid API credentials.") from exc

        now = timezone.now()
        site_settings = SiteSettings.get_settings()
        if not site_settings.enable_api_access or not api_key.is_active:
            raise exceptions.AuthenticationFailed("API access is unavailable.")
        if not has_active_api_entitlement(api_key.user):
            raise exceptions.AuthenticationFailed("API access is unavailable.")
        if not api_key.user.is_active:
            raise exceptions.AuthenticationFailed("API access is unavailable.")

        if not api_key.last_used_at or api_key.last_used_at < now - timedelta(minutes=5):
            ApiKey.objects.filter(pk=api_key.pk).update(last_used_at=now)
            api_key.last_used_at = now

        request.api_customer = api_key.user
        request.api_key = api_key
        return api_key.user, api_key

    def authenticate_header(self, request):
        return "Bearer"


def require_scope(request, scope: str) -> None:
    api_key = getattr(request, "api_key", None)
    if not api_key or scope not in (api_key.scopes or []):
        raise exceptions.PermissionDenied("The API key does not have the required scope.")

import base64
import hashlib
import re

from cryptography.fernet import Fernet, InvalidToken
from django.conf import settings

from api.models import IntegrationSecret


class IntegrationSecretError(Exception):
    pass


SECRET_FIELDS = {
    "resend_api_key": {
        "storage_key": "resend.api_key",
        "environment_setting": "RESEND_API_KEY",
        "prefix": "re_",
    },
    "resend_webhook_secret": {
        "storage_key": "resend.webhook_secret",
        "environment_setting": "RESEND_WEBHOOK_SECRET",
        "prefix": "whsec_",
    },
    "pusher_app_id": {
        "storage_key": "pusher.app_id",
        "environment_setting": "PUSHER_APP_ID",
        "pattern": r"^[A-Za-z0-9_-]+$",
        "error": "Pusher app ID contains unsupported characters.",
    },
    "pusher_key": {
        "storage_key": "pusher.key",
        "environment_setting": "PUSHER_KEY",
        "pattern": r"^[A-Za-z0-9_-]+$",
        "error": "Pusher key contains unsupported characters.",
    },
    "pusher_secret": {
        "storage_key": "pusher.secret",
        "environment_setting": "PUSHER_SECRET",
        "pattern": r"^[A-Za-z0-9_-]+$",
        "error": "Pusher secret contains unsupported characters.",
    },
    "pusher_cluster": {
        "storage_key": "pusher.cluster",
        "environment_setting": "PUSHER_CLUSTER",
        "pattern": r"^[a-z0-9-]+$",
        "error": "Pusher cluster contains unsupported characters.",
    },
}


def _fernet():
    raw_key = settings.INTEGRATION_SECRET_ENCRYPTION_KEY.encode("utf-8")
    derived_key = hashlib.sha256(b"sharptoolz-integration-secrets\0" + raw_key).digest()
    return Fernet(base64.urlsafe_b64encode(derived_key))


def encrypt_integration_secret(value):
    if not value:
        raise IntegrationSecretError("Cannot encrypt an empty integration secret.")
    return _fernet().encrypt(value.encode("utf-8")).decode("ascii")


def decrypt_integration_secret(value):
    try:
        return _fernet().decrypt(value.encode("ascii")).decode("utf-8")
    except (InvalidToken, ValueError, UnicodeError) as exc:
        raise IntegrationSecretError("The integration secret cannot be decrypted.") from exc


def validate_integration_secret(field_name, value):
    definition = SECRET_FIELDS.get(field_name)
    if definition is None:
        raise IntegrationSecretError("Unsupported integration secret.")
    normalized = str(value).strip()
    prefix = definition.get("prefix")
    if prefix and not normalized.startswith(prefix):
        raise IntegrationSecretError(f"{field_name} must begin with {prefix}.")
    pattern = definition.get("pattern")
    if pattern and not re.fullmatch(pattern, normalized):
        raise IntegrationSecretError(definition["error"])
    return normalized


def set_integration_secret(field_name, value, *, updated_by):
    definition = SECRET_FIELDS[field_name]
    normalized = validate_integration_secret(field_name, value)
    secret, _ = IntegrationSecret.objects.update_or_create(
        key=definition["storage_key"],
        defaults={
            "encrypted_value": encrypt_integration_secret(normalized),
            "updated_by": updated_by,
        },
    )
    return secret


def get_integration_secret(field_name):
    definition = SECRET_FIELDS[field_name]
    secret = IntegrationSecret.objects.filter(key=definition["storage_key"]).only("encrypted_value").first()
    if secret is not None:
        return decrypt_integration_secret(secret.encrypted_value)
    return str(getattr(settings, definition["environment_setting"], "") or "")


def integration_secret_status(field_name):
    definition = SECRET_FIELDS[field_name]
    secret = IntegrationSecret.objects.filter(key=definition["storage_key"]).only("updated_at").first()
    if secret is not None:
        return {"configured": True, "source": "admin", "updated_at": secret.updated_at}
    environment_value = str(getattr(settings, definition["environment_setting"], "") or "")
    return {"configured": bool(environment_value), "source": "environment" if environment_value else None, "updated_at": None}

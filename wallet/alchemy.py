import hashlib
import hmac

import requests
from django.conf import settings

from wallet.providers import PaymentProviderError, validate_bep20_address


NOTIFY_ADDRESSES_URL = "https://dashboard.alchemy.com/api/update-webhook-addresses"


def webhook_configured() -> bool:
    return bool(settings.ALCHEMY_WEBHOOK_ID and settings.ALCHEMY_WEBHOOK_SIGNING_KEY)


def address_registration_configured() -> bool:
    return bool(settings.ALCHEMY_WEBHOOK_ID and settings.ALCHEMY_NOTIFY_AUTH_TOKEN)


def valid_webhook_signature(raw_body: bytes, signature: str) -> bool:
    if not settings.ALCHEMY_WEBHOOK_SIGNING_KEY or not signature:
        return False
    digest = hmac.new(
        settings.ALCHEMY_WEBHOOK_SIGNING_KEY.encode("utf-8"),
        msg=raw_body,
        digestmod=hashlib.sha256,
    ).hexdigest()
    return hmac.compare_digest(digest, signature.strip().lower())


def register_webhook_address(address: str, *, session=None) -> None:
    """Idempotently add one generated deposit address to Alchemy Notify."""
    if not address_registration_configured():
        return
    normalized_address = validate_bep20_address(address)
    http = session or requests.Session()
    try:
        response = http.patch(
            NOTIFY_ADDRESSES_URL,
            headers={
                "X-Alchemy-Token": settings.ALCHEMY_NOTIFY_AUTH_TOKEN,
                "Content-Type": "application/json",
            },
            json={
                "webhook_id": settings.ALCHEMY_WEBHOOK_ID,
                "addresses_to_add": [normalized_address],
                "addresses_to_remove": [],
            },
            timeout=settings.ALCHEMY_NOTIFY_TIMEOUT_SECONDS,
        )
        response.raise_for_status()
    except requests.RequestException as exc:
        raise PaymentProviderError("Could not register the payment address with Alchemy Notify.") from exc

import hashlib
import hmac
from urllib.parse import quote

import requests
from django.conf import settings

from wallet.providers import PaymentProviderError, validate_bep20_address


NOTIFY_ADDRESSES_URL = "https://dashboard.alchemy.com/api/update-webhook-addresses"
CUSTOM_VARIABLES_URL = "https://dashboard.alchemy.com/api/graphql/variables"
ERC20_TRANSFER_TOPIC = "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef"


def webhook_configured() -> bool:
    return bool(settings.ALCHEMY_WEBHOOK_ID and settings.ALCHEMY_WEBHOOK_SIGNING_KEY)


def address_registration_configured() -> bool:
    if not settings.ALCHEMY_WEBHOOK_ID or not settings.ALCHEMY_NOTIFY_AUTH_TOKEN:
        return False
    if settings.ALCHEMY_WEBHOOK_TYPE == "graphql":
        return bool(settings.ALCHEMY_CUSTOM_ADDRESS_VARIABLE)
    return True


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
        headers = {
            "X-Alchemy-Token": settings.ALCHEMY_NOTIFY_AUTH_TOKEN,
            "Content-Type": "application/json",
        }
        if settings.ALCHEMY_WEBHOOK_TYPE == "graphql":
            variable = quote(settings.ALCHEMY_CUSTOM_ADDRESS_VARIABLE, safe="")
            response = http.post(
                f"{CUSTOM_VARIABLES_URL}/{variable}",
                headers=headers,
                json={"items": [address_to_topic(normalized_address)]},
                timeout=settings.ALCHEMY_NOTIFY_TIMEOUT_SECONDS,
            )
        else:
            response = http.patch(
                NOTIFY_ADDRESSES_URL,
                headers=headers,
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


def address_to_topic(address: str) -> str:
    """Encode an EVM address as the indexed bytes32 value used in Transfer topic2."""
    normalized_address = validate_bep20_address(address)
    return "0x" + ("0" * 24) + normalized_address[2:].lower()


def custom_webhook_activities(payload: dict) -> list[dict[str, str]]:
    """Normalize matching ERC-20 Transfer logs from an Alchemy Custom webhook."""
    logs = (((payload.get("event") or {}).get("data") or {}).get("block") or {}).get("logs") or []
    activities = []
    for entry in logs:
        if not isinstance(entry, dict):
            continue
        topics = entry.get("topics") or []
        account = entry.get("account") or {}
        transaction = entry.get("transaction") or {}
        contract_address = str(account.get("address") or entry.get("address") or "").lower()
        transaction_hash = str(transaction.get("hash") or entry.get("transactionHash") or "").lower()
        if (
            len(topics) < 3
            or str(topics[0]).lower() != ERC20_TRANSFER_TOPIC
            or not transaction_hash
        ):
            continue
        recipient_topic = str(topics[2]).lower()
        if len(recipient_topic) != 66 or not recipient_topic.startswith("0x"):
            continue
        try:
            int(recipient_topic[2:], 16)
        except ValueError:
            continue
        activities.append(
            {
                "category": "erc20",
                "hash": transaction_hash,
                "toAddress": "0x" + recipient_topic[-40:],
                "contractAddress": contract_address,
            }
        )
    return activities

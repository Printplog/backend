import hashlib
import hmac
import json
import logging
import secrets
import time
from urllib.parse import urlencode

import requests
from django.conf import settings
from django.utils.crypto import constant_time_compare

from api.utils.integration_secrets import get_integration_secret

logger = logging.getLogger(__name__)


def create_customer_access_token():
    token = secrets.token_urlsafe(32)
    return token, hash_customer_access_token(token)


def hash_customer_access_token(token):
    pepper = (settings.API_KEY_PEPPER or settings.SECRET_KEY).encode("utf-8")
    return hmac.new(pepper, str(token).encode("utf-8"), hashlib.sha256).hexdigest()


def customer_token_matches(ticket, token):
    if not ticket.customer_access_token_hash or not token:
        return False
    return constant_time_compare(
        ticket.customer_access_token_hash,
        hash_customer_access_token(token),
    )


def ticket_channel(ticket_id):
    return f"private-support-{str(ticket_id).replace('-', '')}"


def owner_channel(user_id):
    return f"private-support-owner-{user_id}"


def _credentials():
    return {
        "app_id": get_integration_secret("pusher_app_id"),
        "key": get_integration_secret("pusher_key"),
        "secret": get_integration_secret("pusher_secret"),
        "cluster": get_integration_secret("pusher_cluster"),
    }


def public_realtime_config():
    credentials = _credentials()
    enabled = all(credentials.values())
    return {
        "enabled": enabled,
        "key": credentials["key"] if enabled else "",
        "cluster": credentials["cluster"] if enabled else "",
    }


def authorize_private_channel(socket_id, channel_name):
    credentials = _credentials()
    if not all(credentials.values()):
        raise ValueError("Realtime support is not configured.")
    signature = hmac.new(
        credentials["secret"].encode("utf-8"),
        f"{socket_id}:{channel_name}".encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()
    return {"auth": f"{credentials['key']}:{signature}"}


def publish_support_update(ticket, event="support.updated"):
    credentials = _credentials()
    if not all(credentials.values()):
        return False

    path = f"/apps/{credentials['app_id']}/events"
    event_data = json.dumps(
        {"ticket_id": str(ticket.id), "updated_at": ticket.updated_at.isoformat()},
        separators=(",", ":"),
    )
    body = json.dumps(
        {
            "name": event,
            "channels": [ticket_channel(ticket.id), owner_channel(ticket.document.buyer_id)],
            "data": event_data,
        },
        separators=(",", ":"),
    )
    params = {
        "auth_key": credentials["key"],
        "auth_timestamp": str(int(time.time())),
        "auth_version": "1.0",
        "body_md5": hashlib.md5(body.encode("utf-8"), usedforsecurity=False).hexdigest(),
    }
    query = urlencode(sorted(params.items()))
    signature = hmac.new(
        credentials["secret"].encode("utf-8"),
        f"POST\n{path}\n{query}".encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()
    params["auth_signature"] = signature

    try:
        response = requests.post(
            f"https://api-{credentials['cluster']}.pusher.com{path}",
            params=params,
            data=body,
            headers={"Content-Type": "application/json"},
            timeout=10,
        )
        response.raise_for_status()
        return True
    except requests.RequestException:
        logger.exception("Could not publish realtime support update for ticket %s", ticket.id)
        return False

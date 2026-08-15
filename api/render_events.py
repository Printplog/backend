import logging
import secrets
from urllib.parse import urlencode

from asgiref.sync import async_to_sync
from channels.layers import get_channel_layer
from django.conf import settings
from django.core import signing


logger = logging.getLogger(__name__)

RENDER_WATCH_SALT = "api-render-watch-v1"


def render_watch_ttl_seconds() -> int:
    return max(15, min(int(getattr(settings, "API_RENDER_WATCH_TTL_SECONDS", 60)), 300))


def render_group_name(job_id) -> str:
    return f"api_render_{str(job_id).replace('-', '')}"


def issue_render_watch_ticket(job, api_key) -> str:
    return signing.dumps(
        {
            "job_id": str(job.id),
            "user_id": str(job.user_id),
            "api_key_id": str(api_key.id),
            "nonce": secrets.token_urlsafe(18),
        },
        salt=RENDER_WATCH_SALT,
        compress=False,
    )


def read_render_watch_ticket(ticket: str) -> dict:
    payload = signing.loads(
        ticket,
        salt=RENDER_WATCH_SALT,
        max_age=render_watch_ttl_seconds(),
    )
    if not isinstance(payload, dict):
        raise signing.BadSignature("Invalid render watch ticket.")
    required = {"job_id", "user_id", "api_key_id", "nonce"}
    if not required.issubset(payload) or not all(isinstance(payload[key], str) for key in required):
        raise signing.BadSignature("Invalid render watch ticket.")
    return payload


def build_render_watch_url(request, job, api_key) -> str:
    scheme = "wss" if request.is_secure() else "ws"
    ticket = issue_render_watch_ticket(job, api_key)
    query = urlencode({"ticket": ticket})
    return f"{scheme}://{request.get_host()}/ws/api/v1/renders/{job.id}/?{query}"


def broadcast_render_job(job_id) -> None:
    channel_layer = get_channel_layer()
    if channel_layer is None:
        return
    try:
        async_to_sync(channel_layer.group_send)(
            render_group_name(job_id),
            {"type": "render.updated", "job_id": str(job_id)},
        )
    except Exception:
        logger.exception("Could not broadcast render job %s", job_id)

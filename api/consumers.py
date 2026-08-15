from urllib.parse import parse_qs

from channels.db import database_sync_to_async
from channels.generic.websocket import AsyncJsonWebsocketConsumer
from django.core import signing
from django.core.cache import cache

from api.api_security import has_active_api_entitlement
from api.models import DocumentRenderJob, SiteSettings
from api.render_events import (
    read_render_watch_ticket,
    render_group_name,
    render_watch_ttl_seconds,
)


TERMINAL_RENDER_STATUSES = {
    DocumentRenderJob.Status.COMPLETED,
    DocumentRenderJob.Status.FAILED,
}


class RenderJobConsumer(AsyncJsonWebsocketConsumer):
    group_name: str | None = None
    ticket_payload: dict | None = None

    async def connect(self):
        job_id = str(self.scope["url_route"]["kwargs"]["job_id"])
        params = parse_qs(self.scope.get("query_string", b"").decode("utf-8", errors="ignore"))
        ticket = (params.get("ticket") or [""])[0]
        try:
            payload = read_render_watch_ticket(ticket)
        except (signing.BadSignature, signing.SignatureExpired):
            await self.close(code=4403)
            return
        if payload["job_id"] != job_id:
            await self.close(code=4403)
            return

        consumed = await self.consume_ticket(payload["nonce"])
        if not consumed:
            await self.close(code=4409)
            return

        state = await self.get_render_state(payload)
        if state is None:
            await self.close(code=4403)
            return

        self.ticket_payload = payload
        self.group_name = render_group_name(job_id)
        await self.channel_layer.group_add(self.group_name, self.channel_name)
        await self.accept()
        await self.send_json({"type": "render.updated", "data": state})
        if state["status"] in TERMINAL_RENDER_STATUSES:
            await self.channel_layer.group_discard(self.group_name, self.channel_name)
            self.group_name = None
            await self.close(code=1000)

    async def disconnect(self, close_code):
        if self.group_name:
            await self.channel_layer.group_discard(self.group_name, self.channel_name)

    async def render_updated(self, event):
        if not self.ticket_payload or str(event.get("job_id")) != self.ticket_payload["job_id"]:
            return
        state = await self.get_render_state(self.ticket_payload)
        if state is None:
            await self.close(code=4403)
            return
        await self.send_json({"type": "render.updated", "data": state})
        if state["status"] in TERMINAL_RENDER_STATUSES:
            if self.group_name:
                await self.channel_layer.group_discard(self.group_name, self.channel_name)
                self.group_name = None
            await self.close(code=1000)

    @database_sync_to_async
    def consume_ticket(self, nonce: str) -> bool:
        return cache.add(
            f"api-render-watch-used:{nonce}",
            True,
            timeout=render_watch_ttl_seconds(),
        )

    @database_sync_to_async
    def get_render_state(self, payload: dict):
        try:
            job = DocumentRenderJob.objects.select_related("requested_by_key", "user").get(
                pk=payload["job_id"],
                user_id=payload["user_id"],
                requested_by_key_id=payload["api_key_id"],
            )
        except DocumentRenderJob.DoesNotExist:
            return None
        if (
            not SiteSettings.get_settings().enable_api_access
            or not job.requested_by_key
            or not job.requested_by_key.is_active
            or not has_active_api_entitlement(job.user)
        ):
            return None
        return {
            "id": str(job.id),
            "document_id": str(job.document_id),
            "format": job.format,
            "status": job.status,
            "output_size": job.output_size,
            "error_code": job.error_code,
            "started_at": job.started_at.isoformat() if job.started_at else None,
            "completed_at": job.completed_at.isoformat() if job.completed_at else None,
        }

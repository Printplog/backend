from datetime import timedelta
from decimal import Decimal

from asgiref.sync import async_to_sync
from channels.db import database_sync_to_async
from channels.layers import get_channel_layer
from channels.testing import WebsocketCommunicator
from django.core.cache import cache
from django.test import TransactionTestCase, override_settings
from django.utils import timezone

from accounts.models import User
from api.api_security import DEFAULT_API_KEY_SCOPES, generate_api_key
from api.models import (
    ApiEntitlement,
    ApiKey,
    DocumentRenderJob,
    PurchasedTemplate,
    SiteSettings,
    Template,
    Tool,
)
from api.render_events import issue_render_watch_ticket, render_group_name
from serverConfig.asgi import application


@override_settings(CHANNEL_LAYERS={
    "default": {"BACKEND": "channels.layers.InMemoryChannelLayer"},
})
class RenderWebSocketTests(TransactionTestCase):
    def setUp(self):
        cache.clear()
        site = SiteSettings.get_settings()
        site.enable_api_access = True
        site.save(update_fields=["enable_api_access"])
        self.customer = User.objects.create_user("socket-customer", "socket@example.com", "password")
        ApiEntitlement.objects.create(user=self.customer, status=ApiEntitlement.Status.ACTIVE)
        _, prefix, token_hash = generate_api_key()
        self.api_key = ApiKey.objects.create(
            user=self.customer,
            name="Socket key",
            prefix=prefix,
            secret_hash=token_hash,
            scopes=DEFAULT_API_KEY_SCOPES,
        )
        tool = Tool.objects.create(name="Socket Tool", price=Decimal("5.00"))
        template = Template.objects.create(name="Socket Template", type="tool", tool=tool)
        document = PurchasedTemplate.objects.create(
            buyer=self.customer,
            template=template,
            external_user_id="socket-user",
        )
        self.job = DocumentRenderJob.objects.create(
            user=self.customer,
            document=document,
            requested_by_key=self.api_key,
            format=DocumentRenderJob.Format.PDF,
            expires_at=timezone.now() + timedelta(hours=1),
        )

    def test_job_scoped_socket_receives_initial_and_completed_states_and_ticket_is_single_use(self):
        ticket = issue_render_watch_ticket(self.job, self.api_key)
        path = f"/ws/api/v1/renders/{self.job.id}/?ticket={ticket}"
        async_to_sync(self.exercise_socket)(path)

    async def exercise_socket(self, path: str):
        communicator = WebsocketCommunicator(application, path)
        connected, _ = await communicator.connect()
        self.assertTrue(connected)
        initial = await communicator.receive_json_from(timeout=2)
        self.assertEqual(initial["type"], "render.updated")
        self.assertEqual(initial["data"]["status"], DocumentRenderJob.Status.QUEUED)

        await database_sync_to_async(self.complete_job)()
        await get_channel_layer().group_send(
            render_group_name(self.job.id),
            {"type": "render.updated", "job_id": str(self.job.id)},
        )
        completed = await communicator.receive_json_from(timeout=2)
        self.assertEqual(completed["data"]["status"], DocumentRenderJob.Status.COMPLETED)
        await communicator.disconnect()

        reused = WebsocketCommunicator(application, path)
        connected_again, close_code = await reused.connect()
        self.assertFalse(connected_again)
        self.assertEqual(close_code, 4409)

    def complete_job(self):
        DocumentRenderJob.objects.filter(pk=self.job.id).update(
            status=DocumentRenderJob.Status.COMPLETED,
            completed_at=timezone.now(),
        )

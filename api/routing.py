from django.urls import re_path

from api.consumers import RenderJobConsumer


websocket_urlpatterns = [
    re_path(
        r"ws/api/v1/renders/(?P<job_id>[0-9a-fA-F-]{36})/$",
        RenderJobConsumer.as_asgi(),
    ),
]

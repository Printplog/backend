import os
import django
from django.conf import settings
from django.core.asgi import get_asgi_application
from channels.routing import ProtocolTypeRouter, URLRouter
from django.contrib.staticfiles.handlers import ASGIStaticFilesHandler # type: ignore

os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'serverConfig.settings')
django.setup()

# IMPORT AFTER DJANGO.SETUP() to avoid ImproperlyConfigured errors
from wallet.routing import websocket_urlpatterns as wallet_ws
from analytics.routing import websocket_urlpatterns as analytics_ws
from api.routing import websocket_urlpatterns as api_ws
from accounts.authentication import JWTAuthMiddlewareStack

# Create HTTP application with static files for dev
http_app = get_asgi_application()
if settings.DEBUG:
    http_app = ASGIStaticFilesHandler(http_app)

application = ProtocolTypeRouter({
    "http": http_app,
    "websocket": JWTAuthMiddlewareStack(
        URLRouter(wallet_ws + analytics_ws + api_ws)
    ),
})

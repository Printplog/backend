import time

from django.core.cache import cache
from rest_framework.throttling import BaseThrottle

from .models import SiteSettings


class ApiKeyRateThrottle(BaseThrottle):
    """Fixed one-minute window keyed by API-key UUID, never by spoofable IP headers."""

    def __init__(self):
        self._wait = None

    def allow_request(self, request, view):
        api_key = getattr(request, "api_key", None)
        if api_key is None:
            return True

        limit = max(1, min(SiteSettings.get_settings().api_default_rate_limit, 100_000))
        now = int(time.time())
        window = now // 60
        cache_key = f"stz-api-rate:{api_key.pk}:{window}"

        if cache.add(cache_key, 1, timeout=70):
            return True
        try:
            count = cache.incr(cache_key)
        except ValueError:
            cache.set(cache_key, 1, timeout=70)
            count = 1
        if count <= limit:
            return True

        self._wait = 60 - (now % 60)
        return False

    def wait(self):
        return self._wait

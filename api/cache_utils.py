from django.core.cache import cache
from functools import wraps
import hashlib
import logging

logger = logging.getLogger(__name__)


def get_cache_key(prefix, **kwargs):
    """
    Generate a consistent cache key from prefix and keyword arguments.
    """
    key_parts = [prefix]
    for key, value in sorted(kwargs.items()):
        if value is not None:
            key_parts.append(f"{key}:{value}")
    key_string = "_".join(key_parts)
    return hashlib.sha256(key_string.encode()).hexdigest()


def cache_template_list(timeout=300):
    def decorator(view_func):
        @wraps(view_func)
        def wrapper(self, request, *args, **kwargs):
            query_params = request.GET.urlencode()
            user_id = request.user.id if request.user.is_authenticated else 'anonymous'
            cache_key = get_cache_key(
                'template_list',
                query=query_params,
                user=user_id,
                action=self.action
            )
            
            cached_data = cache.get(cache_key)
            if cached_data is not None:
                from rest_framework.response import Response
                return Response(cached_data)
            
            response = view_func(self, request, *args, **kwargs)
            if request.method == 'GET' and response.status_code == 200:
                if hasattr(response, 'data'):
                    cache.set(cache_key, response.data, timeout)
            return response
        return wrapper
    return decorator


def cache_template_detail(timeout=600):
    def decorator(view_func):
        @wraps(view_func)
        def wrapper(self, request, *args, **kwargs):
            template_id = kwargs.get('pk') or kwargs.get('id')
            user_id = request.user.id if request.user.is_authenticated else 'anonymous'
            cache_key = get_cache_key(
                'template_detail',
                id=template_id,
                user=user_id
            )
            
            cached_data = cache.get(cache_key)
            if cached_data is not None:
                from rest_framework.response import Response
                return Response(cached_data)
            
            response = view_func(self, request, *args, **kwargs)
            if request.method == 'GET' and response.status_code == 200:
                if hasattr(response, 'data'):
                    cache.set(cache_key, response.data, timeout)
            return response
        return wrapper
    return decorator


def cache_template_svg(timeout=1800):
    def decorator(view_func):
        @wraps(view_func)
        def wrapper(self, request, *args, **kwargs):
            template_id = kwargs.get('pk') or kwargs.get('id')
            cache_key = get_cache_key(
                'template_svg',
                id=template_id
            )
            
            cached_data = cache.get(cache_key)
            if cached_data is not None:
                from rest_framework.response import Response
                return Response(cached_data)
            
            response = view_func(self, request, *args, **kwargs)
            if request.method == 'GET' and response.status_code == 200:
                if hasattr(response, 'data'):
                    cache.set(cache_key, response.data, timeout)
            return response
        return wrapper
    return decorator


def invalidate_template_cache():
    """
    Invalidate all template-related caches.
    """
    try:
        cache.clear()
        logger.info("[Cache] All caches cleared")
    except Exception as e:
        logger.error(f"[Cache] Failed to clear cache: {e}")


def invalidate_all_template_caches():
    invalidate_template_cache()

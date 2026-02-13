"""
Caching utilities for API responses to improve performance.
Uses Redis for distributed caching with fallback to local memory cache.
"""
from django.core.cache import cache
from django.utils.decorators import method_decorator
from django.views.decorators.cache import cache_page
from django.views.decorators.vary import vary_on_headers
from functools import wraps
import hashlib
import json


def get_cache_key(prefix, **kwargs):
    """
    Generate a consistent cache key from prefix and keyword arguments.
    
    Args:
        prefix: Cache key prefix (e.g., 'template_list')
        **kwargs: Additional parameters to include in the key
    
    Returns:
        str: MD5 hash of the cache key
    """
    key_parts = [prefix]
    for key, value in sorted(kwargs.items()):
        if value is not None:
            key_parts.append(f"{key}:{value}")
    key_string = "_".join(key_parts)
    return f"sharptoolz:{hashlib.md5(key_string.encode()).hexdigest()}"


def cache_template_list(timeout=300):
    """
    Cache decorator for template list views.
    Caches serialized response data for 5 minutes (300 seconds) by default.
    
    Args:
        timeout: Cache timeout in seconds (default: 300 = 5 minutes)
    """
    def decorator(view_func):
        @wraps(view_func)
        def wrapper(self, request, *args, **kwargs):
            # Build cache key based on query parameters and user
            query_params = request.GET.urlencode()
            user_id = request.user.id if request.user.is_authenticated else 'anonymous'
            cache_key = get_cache_key(
                'template_list',
                query=query_params,
                user=user_id,
                action=self.action
            )
            
            # Check cache
            cached_data = cache.get(cache_key)
            if cached_data is not None:
                from rest_framework.response import Response
                return Response(cached_data)
            
            # Call the view
            response = view_func(self, request, *args, **kwargs)
            
            # Cache the response data (only for GET requests with 200 status)
            if request.method == 'GET' and response.status_code == 200:
                # Cache the serialized data, not the response object
                if hasattr(response, 'data'):
                    cache.set(cache_key, response.data, timeout)
            
            return response
        return wrapper
    return decorator


def cache_template_detail(timeout=600):
    """
    Cache decorator for template detail views.
    Caches responses for 10 minutes (600 seconds) by default.
    
    Args:
        timeout: Cache timeout in seconds (default: 600 = 10 minutes)
    """
    def decorator(view_func):
        @wraps(view_func)
        def wrapper(self, request, *args, **kwargs):
            template_id = kwargs.get('pk') or kwargs.get('id')
            user_id = request.user.id if request.user.is_authenticated else 'anonymous'
            is_admin = request.user.is_staff if request.user.is_authenticated else False
            
            cache_key = get_cache_key(
                'template_detail',
                id=template_id,
                user=user_id,
                admin=is_admin
            )
            
            # Check cache
            cached_data = cache.get(cache_key)
            if cached_data is not None:
                from rest_framework.response import Response
                return Response(cached_data)
            
            # Call the view
            response = view_func(self, request, *args, **kwargs)
            
            # Cache the response data
            if request.method == 'GET' and response.status_code == 200:
                if hasattr(response, 'data'):
                    cache.set(cache_key, response.data, timeout)
            
            return response
        return wrapper
    return decorator


def cache_template_svg(timeout=1800):
    """
    Cache decorator for template SVG endpoints.
    Caches SVG content for 30 minutes (1800 seconds) by default.
    
    Args:
        timeout: Cache timeout in seconds (default: 1800 = 30 minutes)
    """
    def decorator(view_func):
        @wraps(view_func)
        def wrapper(self, request, *args, **kwargs):
            template_id = kwargs.get('pk') or kwargs.get('id')
            is_admin = request.user.is_staff if request.user.is_authenticated else False
            
            cache_key = get_cache_key(
                'template_svg',
                id=template_id,
                admin=is_admin
            )
            
            # Check cache
            cached_data = cache.get(cache_key)
            if cached_data is not None:
                from rest_framework.response import Response
                return Response(cached_data)
            
            # Call the view
            response = view_func(self, request, *args, **kwargs)
            
            # Cache the response data
            if request.method == 'GET' and response.status_code == 200:
                if hasattr(response, 'data'):
                    cache.set(cache_key, response.data, timeout)
            
            return response
        return wrapper
    return decorator


def invalidate_template_cache(template_id=None):
    """
    Invalidate template cache entries.
    If template_id is provided, only invalidate that template's cache.
    Otherwise, invalidate all template-related caches.
    
    Args:
        template_id: Optional template ID to invalidate specific template cache
    """
    try:
        # For Redis, we can use pattern matching to delete keys
        # This requires django-redis
        from django_redis import get_redis_connection
        
        redis_client = get_redis_connection("default")
        
        if template_id:
            # Invalidate specific template caches
            patterns = [
                f"sharptoolz:*template_detail*id:{template_id}*",
                f"sharptoolz:*template_svg*id:{template_id}*",
            ]
        else:
            # Invalidate all template caches
            patterns = [
                "sharptoolz:*template_list*",
                "sharptoolz:*template_detail*",
                "sharptoolz:*template_svg*",
            ]
        
        for pattern in patterns:
            # Use SCAN to find matching keys (more efficient than KEYS)
            cursor = 0
            while True:
                cursor, keys = redis_client.scan(cursor, match=pattern, count=100)
                if keys:
                    redis_client.delete(*keys)
                if cursor == 0:
                    break
                    
    except ImportError:
        # Fallback for LocMemCache: clear the entire cache since pattern matching isn't supported
        print("[Cache] LocMemCache detected, clearing all caches for invalidation.")
        cache.clear()
    except Exception as e:
        print(f"[Cache] Invalidation failed: {e}")
        # If it's a Redis error or other, try a clear as last resort
        try:
            cache.clear()
        except:
            pass


def invalidate_all_template_caches():
    """
    Invalidate all template-related caches.
    Useful when templates are created, updated, or deleted in bulk.
    """
    invalidate_template_cache()



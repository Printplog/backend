"""
Response optimization utilities for API endpoints.
Adds caching headers and optimizes response delivery.
"""
from django.utils.cache import patch_cache_control, patch_vary_headers
from rest_framework.response import Response as DRFResponse
from django.http import HttpResponse
import hashlib


def add_cache_headers(response, max_age=300, public=True):
    """
    Add cache control headers to response.
    Works with both DRF Response and Django HttpResponse.
    
    Args:
        response: DRF Response or Django HttpResponse object
        max_age: Cache max age in seconds (default: 300 = 5 minutes)
        public: Whether cache is public or private (default: True)
    """
    # Handle DRF Response objects
    if isinstance(response, DRFResponse):
        # Access the underlying HttpResponse
        http_response = response.render() if hasattr(response, 'render') else response
    else:
        http_response = response
    
    cache_control = f"{'public' if public else 'private'}, max-age={max_age}"
    patch_cache_control(http_response, cache_control=cache_control)
    
    # Add ETag for conditional requests if content is available
    if hasattr(http_response, 'content') and http_response.content:
        etag = hashlib.sha256(http_response.content).hexdigest()
        http_response['ETag'] = f'"{etag}"'
    
    return response


def add_list_response_headers(response, request, max_age=60):
    """
    Add optimized cache headers for list views.
    Shorter cache time since lists change more frequently.
    
    Args:
        response: DRF Response object
        request: Django HttpRequest object
        max_age: Cache max age in seconds (default: 60 = 1 minute)
    """
    # For DRF Response objects, set headers directly without rendering
    if isinstance(response, DRFResponse):
        # Set cache headers directly on the response object
        cache_control = f"public, max-age={max_age}, must-revalidate"
        response['Cache-Control'] = cache_control
        response['Vary'] = 'Accept, Accept-Encoding'
    else:
        # For Django HttpResponse objects, use patch functions
        cache_control = f"public, max-age={max_age}, must-revalidate"
        patch_cache_control(response, cache_control=cache_control)
        patch_vary_headers(response, ['Accept', 'Accept-Encoding'])
    
    return response

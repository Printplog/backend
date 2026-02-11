from django.conf import settings
import sys
from storages.backends.s3boto3 import S3Boto3Storage

class MediaStorage(S3Boto3Storage):
    """
    Custom S3 storage backend to ensure signatures are kept even when 
    using a custom domain (CDN).
    """
    def get_object_parameters(self, name):
        params = super().get_object_parameters(name)
        return params

    def url(self, name, parameters=None, expire=None, http_method=None):
        # We want signatures even on the custom domain because the bucket is private
        # By default S3Boto3Storage disables signatures if custom_domain is set.
        # So we temporarily unset it to get the signed URL, then restore it.
        
        orig_custom_domain = self.custom_domain
        self.custom_domain = None
        signed_url = super().url(name, parameters, expire, http_method)
        self.custom_domain = orig_custom_domain
        
        # Use the domain from settings if available
        custom_domain = getattr(settings, 'AWS_S3_CUSTOM_DOMAIN', None) or self.custom_domain
        
        if custom_domain:
            # Replace the internal B2 hostname with our Cloudflare domain
            from urllib.parse import urlparse, urlunparse
            parsed_signed = urlparse(signed_url)
            # Replace the netloc (hostname) with our custom domain
            new_parsed = parsed_signed._replace(netloc=custom_domain)
            return urlunparse(new_parsed)
            
        return signed_url

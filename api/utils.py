import boto3
from urllib.parse import urlparse, urlunparse
from django.conf import settings

def get_signed_url(file_field):
    """
    Generates a presigned URL for a private file field, compatible with
    custom domain configurations (e.g. Cloudflare + Backblaze B2).
    """
    if not file_field:
        return None
        
    try:
        # Get storage backend instance
        storage = file_field.storage
        
        # If bucket is private and we need to sign the URL
        # Generate presigned URL using the underlying boto3 client
        # This bypasses django-storages potential issue with custom domains + query/auth
        
        # Note: We assume the storage backend is S3Boto3Storage and has a connection/client
        if hasattr(storage, 'connection'):
             client = storage.connection.meta.client
        elif hasattr(storage, 'bucket'):
             client = storage.bucket.meta.client
        else:
            # Fallback for local storage or other backends
            return file_field.url

        params = {'Bucket': storage.bucket_name, 'Key': file_field.name}
        
        # Generate standard S3 presigned URL (using endpoint_url from settings)
        signed_url = client.generate_presigned_url('get_object', Params=params)
        
        # If using a custom domain (CDN), replace the host in the signed URL
        custom_domain = getattr(settings, 'AWS_S3_CUSTOM_DOMAIN', None)
        if custom_domain:
            parsed = urlparse(signed_url)
            # Reconstruct URL with new netloc (host) but keeping path and query params
            # Ensure scheme is https
            new_url = urlunparse(('https', custom_domain, parsed.path, parsed.params, parsed.query, parsed.fragment))
            return new_url
            
        return signed_url
        
    except Exception as e:
        # Fallback to default behavior if anything fails
        print(f"Error generating formatted signed URL: {e}")
        return file_field.url

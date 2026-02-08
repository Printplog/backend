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
        # So we temporarily unset it to get the signed URL, then swap the domain.
        
        orig_custom_domain = self.custom_domain
        self.custom_domain = None
        signed_url = super().url(name, parameters, expire, http_method)
        self.custom_domain = orig_custom_domain
        
        if self.custom_domain:
            # Replace the B2 endpoint with our Cloudflare CDN domain
            # B2 endpoint is usually like 's3.us-east-005.backblazeb2.com/bucketname'
            # Or 'bucketname.s3.us-east-005.backblazeb2.com'
            import re
            # Extract bucket name and endpoint for replacement
            # Standard B2 path: https://s3.region.backblazeb2.com/bucket/path
            pattern = rf'https://[^/]+/{self.bucket_name}/'
            replacement = f'https://{self.custom_domain}/'
            return re.sub(pattern, replacement, signed_url)
            
        return signed_url

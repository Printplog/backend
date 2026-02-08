import os
import sys

# 1. Set environment variables BEFORE importing Django components
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "serverConfig.settings")
os.environ["ENV"] = "production"  # Force production to use B2 S3

# 2. Setup Django
import django
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
django.setup()

# 3. Now import storage and boto3 related things
from django.core.files.storage import default_storage
from django.conf import settings

def migrate_media():
    media_root = settings.MEDIA_ROOT
    try:
        backend_name = default_storage.__class__.__name__
    except Exception:
        backend_name = "Unknown"
        
    print(f"--- Migration Tool ---")
    print(f"Using Storage Backend: {backend_name}")
    print(f"Bucket: {getattr(settings, 'AWS_STORAGE_BUCKET_NAME', 'Not set')}")
    print(f"Endpoint: {getattr(settings, 'AWS_S3_ENDPOINT_URL', 'Not set')}")
    print(f"Starting migration from {media_root} ...")
    
    if not os.path.exists(media_root):
        print(f"Error: Local media directory {media_root} does not exist.")
        return

    count = 0
    errors = 0
    skipped = 0
    
    # Walk through the media directory
    for root, dirs, files in os.walk(media_root):
        for file in files:
            # Get the relative path for the storage system
            local_path = os.path.join(root, file)
            relative_path = os.path.relpath(local_path, media_root)
            
            try:
                # Check if file already exists in B2
                if default_storage.exists(relative_path):
                    print(f"[-] Skipping: {relative_path} (exists in cloud)")
                    skipped += 1
                    continue
                
                # Open and save to default storage (which is B2 since ENV=production)
                with open(local_path, 'rb') as f:
                    default_storage.save(relative_path, f)
                
                print(f"[+] Uploaded: {relative_path}")
                count += 1
            except Exception as e:
                print(f"[!] Error uploading {relative_path}: {e}")
                errors += 1

    print("\n--- Migration Summary ---")
    print(f"Successfully uploaded: {count}")
    print(f"Skipped (already exists): {skipped}")
    print(f"Errors encountered: {errors}")

if __name__ == "__main__":
    migrate_media()

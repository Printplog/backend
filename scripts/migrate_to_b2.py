import os
import django
import sys
from django.core.files.storage import default_storage
from django.conf import settings

# Setup Django environment
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "serverConfig.settings")
django.setup()

def migrate_media():
    media_root = settings.MEDIA_ROOT
    print(f"Starting migration from {media_root} to Backblaze B2...")
    
    if not os.path.exists(media_root):
        print(f"Error: {media_root} does not exist.")
        return

    count = 0
    errors = 0
    
    # Walk through the media directory
    for root, dirs, files in os.walk(media_root):
        for file in files:
            # Get the relative path for the storage system
            local_path = os.path.join(root, file)
            relative_path = os.path.relpath(local_path, media_root)
            
            try:
                # Check if file already exists in B2
                if default_storage.exists(relative_path):
                    print(f"Skipping: {relative_path} (already exists)")
                    continue
                
                # Open and save to default storage (which is B2 if configured)
                with open(local_path, 'rb') as f:
                    default_storage.save(relative_path, f)
                
                print(f"Uploaded: {relative_path}")
                count += 1
            except Exception as e:
                print(f"Error uploading {relative_path}: {e}")
                errors += 1

    print("\nMigration Complete!")
    print(f"Successfully uploaded: {count}")
    print(f"Errors encountered: {errors}")

if __name__ == "__main__":
    # Ensure production environment is used for B2 storage
    os.environ["ENV"] = "production"
    migrate_media()

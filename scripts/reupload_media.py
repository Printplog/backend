import os
import sys
import boto3
import logging
from PIL import Image
import io
from dotenv import load_dotenv

# Setup logging
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

# Load env
base_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.append(base_dir) # Add base_dir to path so we can import api.compression
load_dotenv(os.path.join(base_dir, '.env'))

try:
    from api.compression import compress_svg_images, compress_image_data
except ImportError:
    # Fallback if django environment isn't fully set up for the script
    def compress_svg_images(text, **kwargs): return text

# B2 Config
ENDPOINT_URL = os.getenv('BACKBLAZE_S3_ENDPOINT_URL')
KEY_ID = os.getenv('BACKBLAZE_B2_KEY_ID')
APP_KEY = os.getenv('BACKBLAZE_B2_APPLICATION_KEY')
BUCKET_NAME = os.getenv('BACKBLAZE_B2_BUCKET_NAME')

if not all([ENDPOINT_URL, KEY_ID, APP_KEY, BUCKET_NAME]):
    logger.error("Missing B2 connection details in .env")
    sys.exit(1)

# Connect to S3/B2
s3 = boto3.resource('s3',
    endpoint_url=ENDPOINT_URL,
    aws_access_key_id=KEY_ID,
    aws_secret_access_key=APP_KEY
)
bucket = s3.Bucket(BUCKET_NAME)

def empty_bucket():
    logger.info(f"Checking bucket: {BUCKET_NAME}")
    try:
        # B2 buckets often have versioning enabled. Standard delete_all() only deletes the latest versions.
        # We need to delete ALL versions and ALL delete markers to truly empty a B2 bucket.
        logger.info("Starting deep deletion of all object versions...")
        
        # Collect all versions
        versions = bucket.object_versions.all()
        for version in versions:
            version.delete()
            
        logger.info("Bucket emptied successfully (all versions removed).")
    except Exception as e:
        logger.error(f"Failed to empty bucket: {e}")
        sys.exit(1)

def compress_image_bytes(file_path):
    """
    Compress image files and return bytes.
    Retains original format.
    """
    try:
        with Image.open(file_path) as img:
            output = io.BytesIO()
            img_format = img.format
            
            # Smart resize (LANZCOS) if huge
            if img.width > 2500:
                ratio = 2500 / img.width
                new_height = int(img.height * ratio)
                img = img.resize((2500, new_height), Image.Resampling.LANCZOS)
            
            if img_format == 'JPEG':
                img.save(output, format='JPEG', quality=60, optimize=True)
            elif img_format == 'PNG':
                img.save(output, format='PNG', optimize=True) # PNG is lossless, optimize flag helps
            else:
                img.save(output, format=img_format)
            
            return output.getvalue()
    except Exception as e:
        logger.warning(f"Compression failed for {file_path}, using original: {e}")
        with open(file_path, 'rb') as f:
            return f.read()

def upload_folder(local_folder):
    media_root = os.path.join(base_dir, 'media')
    
    for root, dirs, files in os.walk(local_folder):
        for file in files:
            full_path = os.path.join(root, file)
            # Calculate relative path (key)
            relative_path = os.path.relpath(full_path, media_root)
            
            content_type = ''
            body = None
            
            # Decide on processing
            is_image = file.lower().endswith(('.jpg', '.jpeg', '.png'))
            is_svg = file.lower().endswith('.svg')
            
            logger.info(f"Processing: {relative_path}")
            
            if is_image:
                body = compress_image_bytes(full_path)
                if file.lower().endswith('.png'): content_type = 'image/png'
                if file.lower().endswith(('.jpg', '.jpeg')): content_type = 'image/jpeg'
                
                # Update local file too
                with open(full_path, 'wb') as f:
                    f.write(body)
            elif is_svg:
                with open(full_path, 'r', encoding='utf-8', errors='ignore') as f:
                    svg_text = f.read()
                
                logger.info(f"Compressing SVG embedded images for {relative_path}...")
                optimized_svg = compress_svg_images(svg_text, quality=60)
                body = optimized_svg.encode('utf-8')
                content_type = 'image/svg+xml'
                
                # Update local file too
                with open(full_path, 'w', encoding='utf-8') as f:
                    f.write(optimized_svg)
            else:
                # Other files upload as-is
                with open(full_path, 'rb') as f:
                    body = f.read()
                
            extra_args = {'CacheControl': 'public, max-age=31536000'}
            if content_type:
                extra_args['ContentType'] = content_type
                
            try:
                bucket.put_object(Key=relative_path, Body=body, **extra_args)
                logger.info(f"Uploaded: {relative_path}")
            except Exception as e:
                logger.error(f"Failed to upload {relative_path}: {e}")

if __name__ == "__main__":
    confirm = input("This will DELETE ALL FILES in the B2 bucket and re-upload from local media/. Type 'DELETE' to confirm: ")
    if confirm == "DELETE":
        empty_bucket()
        upload_folder(os.path.join(base_dir, 'media'))
        logger.info("Migration Complete!")
    else:
        print("Operation cancelled.")

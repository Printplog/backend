import os
import sys
import boto3
from dotenv import load_dotenv

# Load env
base_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
load_dotenv(os.path.join(base_dir, '.env'))

# B2 Config
ENDPOINT_URL = os.getenv('BACKBLAZE_S3_ENDPOINT_URL')
KEY_ID = os.getenv('BACKBLAZE_B2_KEY_ID')
APP_KEY = os.getenv('BACKBLAZE_B2_APPLICATION_KEY')
BUCKET_NAME = os.getenv('BACKBLAZE_B2_BUCKET_NAME')

# Connect to S3/B2
s3 = boto3.resource('s3',
    endpoint_url=ENDPOINT_URL,
    aws_access_key_id=KEY_ID,
    aws_secret_access_key=APP_KEY
)
bucket = s3.Bucket(BUCKET_NAME)

# Upload the missing file
file_path = 'media/templates/svgs/5518ee0c-5c51-46c6-b866-24046ccd069b_WNo3ct3.svg'
s3_key = 'templates/svgs/5518ee0c-5c51-46c6-b866-24046ccd069b_WNo3ct3.svg'

with open(file_path, 'rb') as f:
    bucket.put_object(
        Key=s3_key,
        Body=f.read(),
        ContentType='image/svg+xml',
        CacheControl='public, max-age=31536000'
    )

print(f"Uploaded {s3_key} successfully!")

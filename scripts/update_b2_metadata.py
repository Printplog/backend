import os
from b2sdk.v2 import *
from dotenv import load_dotenv

# Load environment variables
load_dotenv('.env')

application_key_id = os.getenv('BACKBLAZE_B2_KEY_ID')
application_key = os.getenv('BACKBLAZE_B2_APPLICATION_KEY')
bucket_name = os.getenv('BACKBLAZE_B2_BUCKET_NAME')

info = {'Cache-Control': 'public, max-age=31536000'}

def update_b2_info():
    info_source = InMemoryAccountInfo()
    b2_api = B2Api(info_source)
    b2_api.authorize_account('production', application_key_id, application_key)
    
    bucket = b2_api.get_bucket_by_name(bucket_name)
    bucket.update(bucket_info=info)
    print(f"Successfully updated bucket {bucket_name} with info: {info}")

if __name__ == "__main__":
    try:
        update_b2_info()
    except Exception as e:
        print(f"Error: {e}")

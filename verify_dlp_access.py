import os
import json
from google.cloud import dlp_v2
import google.auth

# Resolve the key path from config.json (the app may have moved it to a secure
# per-user folder); fall back to a local credentials.json.
def _resolve_credentials():
    key_file = 'credentials.json'
    location = 'europe-west6'
    try:
        with open('config.json', 'r', encoding='utf-8') as f:
            cfg = json.load(f)
        gc = cfg.get('google_cloud', {})
        key_file = gc.get('service_account_key_file', key_file)
        location = gc.get('location', location)
    except Exception:
        pass
    return os.path.abspath(key_file), location

KEY_PATH, DLP_LOCATION = _resolve_credentials()
os.environ['GOOGLE_APPLICATION_CREDENTIALS'] = KEY_PATH

def verify_access():
    try:
        print(f"1. Loading Credentials from: {KEY_PATH}")
        creds, project = google.auth.default()
        print(f"   Success. Authenticated as project: {project}")
        if hasattr(creds, "service_account_email"):
            print(f"   Service Account: {creds.service_account_email}")

        print(f"\n2. Testing DLP API Access (region: {DLP_LOCATION})...")
        client = dlp_v2.DlpServiceClient()
        # Same regional parent the app uses, so this test proves data residency works.
        parent = f"projects/{project}/locations/{DLP_LOCATION}"

        print("\n3. Testing Content Inspection (Hello World)...")
        # Test a simple content inspection
        item = {"value": "My email is test@example.com"}

        inspect_config = {
            "info_types": [
                {"name": "PERSON_NAME"}, 
                {"name": "PHONE_NUMBER"}, 
                {"name": "EMAIL_ADDRESS"}, 
                {"name": "CREDIT_CARD_NUMBER"},
                {"name": "STREET_ADDRESS"},
                {"name": "PASSPORT"},
                {"name": "GERMANY_PASSPORT"},
                {"name": "IBAN_CODE"},
                {"name": "IP_ADDRESS"}
            ],
            "min_likelihood": dlp_v2.Likelihood.POSSIBLE, 
        }
        
        response = client.inspect_content(
            request={
                "parent": parent,
                "inspect_config": inspect_config,
                "item": item,
            }
        )
        print(f"   Success! InspectContent worked in {DLP_LOCATION}.")

        print("\n4. Testing RedactImage (Simple ByteItem)...")
        # Create a tiny 1x1 GIF to test image redaction
        # GIF89a header + minimal data
        # Use PNG for safety
        dummy_png = b'\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01\x08\x06\x00\x00\x00\x1f\x15\xc4\x89\x00\x00\x00\nIDATx\x9cc\x00\x01\x00\x00\x05\x00\x01\r\n-\xb4\x00\x00\x00\x00IEND\xaeB`\x82'
        
        byte_item = {"type_": dlp_v2.ByteContentItem.BytesType.IMAGE_PNG, "data": dummy_png}
        
        # Redact config
        redact_configs = [{"info_type": {"name": "EMAIL_ADDRESS"}, "redaction_color": {"red": 0, "green": 0, "blue": 0}}]

        response_redact = client.redact_image(
            request={
                "parent": parent,
                "inspect_config": inspect_config,
                "image_redaction_configs": redact_configs,
                "byte_item": byte_item
            }
        )
        print(f"   Success! RedactImage worked in {DLP_LOCATION}. Result size: {len(response_redact.redacted_image)}")

    except Exception as e:
        error_msg = f"[ERROR] Test Failed: {e}"
        print(error_msg)
        with open("error_log.txt", "w") as f:
            f.write(str(e))
            import traceback
            f.write("\n\nTRACEBACK:\n")
            traceback.print_exc(file=f)

if __name__ == "__main__":
    verify_access()

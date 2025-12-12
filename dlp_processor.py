import os
from typing import List, Optional
import google.auth
from google.cloud import dlp_v2
from google.cloud.dlp_v2 import types

class DLPProcessor:
    def __init__(self, project_id: str, credentials_file: str = None):
        """
        Initialize the Data Loss Prevention (DLP) API client.
        
        Args:
            project_id: Google Cloud Project ID.
            credentials_file: Path to service account JSON key (optional if using default auth).
        """
        self.project_id = project_id
        
        # Authenticate
        if credentials_file and os.path.exists(credentials_file):
            os.environ['GOOGLE_APPLICATION_CREDENTIALS'] = credentials_file
            
        self.dlp_client = dlp_v2.DlpServiceClient()
        self.parent = f"projects/{self.project_id}"

    def deidentify_text(self, text: str, info_types: List[str] = None):
        """
        De-identifies sensitive information in a text string.
        
        Args:
            text: The original text to anonymize.
            info_types: List of info types to redact (e.g., ["PERSON_NAME", "DATE"]). 
                        Defaults to common PII.
        
        Returns:
            The anonymized text.
        """
        if not text:
            return ""

        # Default standard sensitive info types
        if info_types is None:
            info_types = [
                "PERSON_NAME", 
                "PHONE_NUMBER", 
                "EMAIL_ADDRESS", 
                "CREDIT_CARD_NUMBER", 
                "DATE_OF_BIRTH",
                "US_SOCIAL_SECURITY_NUMBER",
                "LOCATION",
                "IP_ADDRESS",
                "DATE" # Be careful with general DATE, it catches all dates
            ]

        # Prepare configuration for inspection (what to look for)
        inspect_config = {
            "info_types": [{"name": info_type} for info_type in info_types]
        }

        # Prepare configuration for de-identification (how to mask it)
        # Here we use "replace", which replaces the finding with [INFO_TYPE]
        deidentify_config = {
            "info_type_transformations": {
                "transformations": [
                    {
                        "primitive_transformation": {
                            "replace_with_info_type_config": {} 
                        }
                    }
                ]
            }
        }

        # Construct the request
        item = {"value": text}
        
        try:
            response = self.dlp_client.deidentify_content(
                request={
                    "parent": self.parent,
                    "deidentify_config": deidentify_config,
                    "inspect_config": inspect_config,
                    "item": item,
                }
            )
            return response.item.value
        except Exception as e:
            raise Exception(f"DLP API Error: {e}")

# Example usage for testing
if __name__ == "__main__":
    # This block is for direct testing of this script
    import sys
    
    # Simple mock config retrieval for test
    PROJECT_ID = "YOUR_PROJECT_ID" # Replace or pass as arg
    if len(sys.argv) > 1:
        PROJECT_ID = sys.argv[1]
        
    print(f"Initializing DLP for project: {PROJECT_ID}...")
    try:
        # Note: Will fail if no creds are set up in environment
        processor = DLPProcessor(PROJECT_ID) 
        sample_text = "Patient John Doe visited on 2023-10-05. Phone: 555-0123."
        print(f"Original: {sample_text}")
        result = processor.deidentify_text(sample_text)
        print(f"Anonymized: {result}")
    except Exception as e:
        print(f"Test failed (expected without valid creds): {e}")

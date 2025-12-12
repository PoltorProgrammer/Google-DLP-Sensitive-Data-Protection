import argparse
import time
from googleapiclient import discovery
from google.oauth2 import service_account
import google.auth

def deidentify_fhir_store(
    project_id,
    location,
    dataset_id,
    fhir_store_id,
    destination_dataset_id,
    destination_fhir_store_id,
):
    """De-identifies data in the source FHIR store and writes it to the destination FHIR store."""
    
    # Authenticate
    # In a real environment, this will pick up GOOGLE_APPLICATION_CREDENTIALS
    credentials, _ = google.auth.default()
    
    service = discovery.build('healthcare', 'v1', credentials=credentials)
    
    parent = f"projects/{project_id}/locations/{location}/datasets/{dataset_id}/fhirStores/{fhir_store_id}"
    destination_store_path = f"projects/{project_id}/locations/{location}/datasets/{destination_dataset_id}/fhirStores/{destination_fhir_store_id}"

    # De-identification configuration
    body = {
        "destinationStore": destination_store_path,
        "config": {
            "fhir": {
                "removeHealthcares": True, # Example: basic removal
                # "skipPaths": ["Device.type"] 
            }
        }
    }

    print(f"Starting de-identification job from {parent} to {destination_store_path}...")
    
    try:
        request = service.projects().locations().datasets().fhirStores().deidentify(
            sourceStore=parent, 
            body=body
        )
        operation = request.execute()
        
        print(f"Operation name: {operation['name']}")
        
        # Wait for operation (simple polling)
        op_name = operation['name']
        while True:
            op_result = service.projects().locations().datasets().operations().get(name=op_name).execute()
            if 'done' in op_result and op_result['done']:
                if 'error' in op_result:
                     print(f"Error: {op_result['error']}")
                else:
                    print("De-identification complete.")
                break
            time.sleep(2)
            print("Processing...")

    except Exception as e:
        print(f"An error occurred: {e}")

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="De-identify a FHIR Store using Google Cloud Healthcare API")
    parser.add_argument("--project_id", required=True, help="Google Cloud Project ID")
    parser.add_argument("--location", required=True, help="Location (e.g., us-central1)")
    parser.add_argument("--dataset_id", required=True, help="Source Dataset ID")
    parser.add_argument("--fhir_store_id", required=True, help="Source FHIR Store ID")
    parser.add_argument("--destination_dataset_id", required=True, help="Destination Dataset ID")
    parser.add_argument("--destination_fhir_store_id", required=True, help="Destination FHIR Store ID")

    args = parser.parse_args()

    deidentify_fhir_store(
        args.project_id,
        args.location,
        args.dataset_id,
        args.fhir_store_id,
        args.destination_dataset_id,
        args.destination_fhir_store_id,
    )

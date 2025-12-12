#!/bin/bash

echo ""
echo "[3/4] Checking Configuration..."

if [ ! -f "config.json" ]; then
    echo "   [!] config.json not found. Creating default..."
    cat > config.json <<EOL
{
    "google_cloud": {
        "project_id": "ENTER_PROJECT_ID",
        "location": "us-central1",
        "dataset_id": "ENTER_DATASET",
        "fhir_store_id": "ENTER_SOURCE_STORE",
        "destination_dataset_id": "ENTER_DEST_DATASET",
        "destination_fhir_store_id": "ENTER_DEST_STORE",
        "service_account_key_file": "credentials.json"
    },
    "app_settings": {
        "simulation_mode": true,
        "default_save_format": "mp3",
        "tts_rate": 150,
        "tts_volume": 1.0
    }
}
EOL
fi

# Check for credentials
if [ ! -f "credentials.json" ]; then
    echo "   [INFO] 'credentials.json' not found."
    echo "          App will likely run in SIMULATION MODE only."
fi

echo "   [OK] Configuration verified."
exit 0

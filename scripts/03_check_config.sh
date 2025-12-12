#!/bin/bash

echo ""
echo "[3/4] Checking Configuration..."

if [ ! -f "config.json" ]; then
    echo "   [!] config.json not found. Creating default..."
    cat > config.json <<EOL
{
    "google_cloud": {
        "project_id": "ENTER_PROJECT_ID",
        "service_account_key_file": "credentials.json"
    },
    "app_settings": {
        "simulation_mode": true
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

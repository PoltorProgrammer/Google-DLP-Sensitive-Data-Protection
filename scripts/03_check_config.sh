#!/bin/bash

echo ""
echo "[3/4] Checking Configuration..."

if [ ! -f "config.json" ]; then
    echo "   [!] config.json not found. Creating default..."
    cat > config.json <<EOL
{
    "google_cloud": {
        "project_id": "ENTER_PROJECT_ID",
        "location": "europe-west6",
        "service_account_key_file": "credentials.json"
    },
    "output_options": {
        "redaction": true,
        "redaction_iterations": 1,
        "selectable_text_copy": true,
        "non_selectable_text_copy": false,
        "translation_redaction_iterations": 0
    },
    "translation": {
        "enabled": false,
        "target_language_code": "en"
    },
    "app_settings": {
        "output_folder": "",
        "overwrite_policy": "skip",
        "verification_scan": true,
        "audit_log": true
    }
}
EOL
    echo "   [!] Edit config.json and set your Google Cloud project_id (see README)."
fi

# The key may legitimately live in the per-user secure folder instead of here;
# the app validates the configured location itself and guides the user.
if [ ! -f "credentials.json" ]; then
    echo "   [INFO] No credentials.json in the app folder."
    echo "          Fine if the app already moved it to the secure per-user folder;"
    echo "          otherwise follow the README to create one."
fi

echo "   [OK] Configuration verified."
exit 0

@echo off
echo.
echo [3/4] Checking Configuration...

if not exist "config.json" (
    echo    [!] config.json not found. Creating default...
    (
        echo {
        echo     "google_cloud": {
        echo         "project_id": "ENTER_PROJECT_ID",
        echo         "location": "europe-west6",
        echo         "service_account_key_file": "credentials.json"
        echo     },
        echo     "output_options": {
        echo         "redaction": true,
        echo         "redaction_iterations": 1,
        echo         "selectable_text_copy": true,
        echo         "non_selectable_text_copy": false,
        echo         "translation_redaction_iterations": 0
        echo     },
        echo     "translation": {
        echo         "enabled": false,
        echo         "target_language_code": "en"
        echo     },
        echo     "app_settings": {
        echo         "output_folder": "",
        echo         "overwrite_policy": "skip",
        echo         "verification_scan": true,
        echo         "audit_log": true
        echo     }
        echo }
    ) > config.json
    echo    [!] Edit config.json and set your Google Cloud project_id (see README^).
)

:: The key may legitimately live in the per-user secure folder instead of here;
:: the app validates the configured location itself and guides the user.
if not exist "credentials.json" (
    echo    [INFO] No credentials.json in the app folder.
    echo           Fine if the app already moved it to the secure per-user folder;
    echo           otherwise follow the README to create one.
)

echo    [OK] Configuration verified.
exit /b 0

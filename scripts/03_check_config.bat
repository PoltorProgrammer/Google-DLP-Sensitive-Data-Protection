@echo off
echo.
echo [3/4] Checking Configuration...

if not exist "config.json" (
    echo    [!] config.json not found. Creating default...
    (
        echo {
        echo     "google_cloud": {
        echo         "project_id": "ENTER_PROJECT_ID",
        echo         "location": "us-central1",
        echo         "dataset_id": "ENTER_DATASET",
        echo         "fhir_store_id": "ENTER_SOURCE_STORE",
        echo         "destination_dataset_id": "ENTER_DEST_DATASET",
        echo         "destination_fhir_store_id": "ENTER_DEST_STORE",
        echo         "service_account_key_file": "credentials.json"
        echo     },
        echo     "app_settings": {
        echo         "simulation_mode": true,
        "default_save_format": "mp3",
        "tts_rate": 150,
        "tts_volume": 1.0
        echo     }
        echo }
    ) > config.json
)

:: Check for credentials used in REAL mode
set "creds_exist=false"
if exist "credentials.json" set "creds_exist=true"

:: If not found, check if simulate mode is on. 
:: We won't block execution, just warn.
if "%creds_exist%"=="false" (
    echo    [INFO] 'credentials.json' not found. 
    echo           App will likely run in SIMULATION MODE only.
    echo           (To fix: Place your Google Cloud JSON key here and rename it to credentials.json^)
)

echo    [OK] Configuration verified.
exit /b 0

@echo off
echo.
echo [3/4] Checking Configuration...

if not exist "config.json" (
    echo    [!] config.json not found. Creating default...
    (
        echo {
        echo     "google_cloud": {
        echo         "project_id": "ENTER_PROJECT_ID",
        echo         "service_account_key_file": "credentials.json"
        echo     },
        echo     "app_settings": {
        echo         "simulation_mode": true
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

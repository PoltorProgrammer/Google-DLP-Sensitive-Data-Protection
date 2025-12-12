@echo off
echo.
echo [2/4] Checking Virtual Environment & Libraries...

if not exist ".venv" (
    echo    [!] No environment found. Creating one now...
    echo    ... This may take a minute ...
    python -m venv .venv
    if %errorlevel% neq 0 (
        echo    [ERROR] Failed to create virtual environment.
        pause
        exit /b 1
    )
) else (
    echo    [OK] Virtual environment exists.
)

:: Activate the environment for the next checks
call .venv\Scripts\activate.bat

:: Check if requirements are satisfied (simple check)
:: We assume if 'google-api-python-client' is installed, we are likely good.
:: Or we just run pip install every time (it's fast if already installed).

echo    ... Verifying libraries ...
pip install -r requirements.txt >nul
if %errorlevel% neq 0 (
    echo    [ERROR] Failed to install requirements. Check internet connection.
    pause
    exit /b 1
)

echo    [OK] Environment is ready.
exit /b 0

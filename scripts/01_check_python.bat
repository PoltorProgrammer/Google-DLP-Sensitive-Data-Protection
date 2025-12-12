@echo off
echo.
echo [1/4] Checking Python System Requirements...

:: Check if Python is installed and accessible
python --version >nul 2>&1
if %errorlevel% neq 0 (
    echo    [X] Python not found.
    echo    [!] Attempting automatic installation...
    
    :: Attempt Winget Install
    winget install -e --id Python.Python.3.11
    
    :: Re-check
    python --version >nul 2>&1
    if %errorlevel% neq 0 (
        cls
        echo ==========================================================
        echo [ERROR] CRITICAL REQUIREMENT MISSING
        echo ==========================================================
        echo Python was not found and could not be installed automatically.
        echo.
        echo Please manually install Python from: https://www.python.org/downloads/
        echo IMPORTANT: Check the box "Add Python to PATH" during installation.
        echo.
        pause
        exit /b 1
    )
)

echo    [OK] Python is ready.
exit /b 0

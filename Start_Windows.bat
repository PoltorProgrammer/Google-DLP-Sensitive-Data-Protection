@echo off
setlocal EnableDelayedExpansion
TITLE Clinical Document Processor
cls

echo ==========================================================
echo          CLINICAL DOCUMENT PROCESSOR - LAUNCHER
echo ==========================================================
echo.
echo Hello! Checking your system to make sure everything is ready...
echo.

:: -------------------------------------------------------------
:: STEP A: CREATE DESKTOP SHORTCUT (First Run Only)
:: -------------------------------------------------------------

:: Use current directory for temporary script to avoid %TEMP% issues
set "PS_SCRIPT=%~dp0temp_shortcut_creator.ps1"

if not exist ".shortcut_created" (
    echo [0/4] Creating Desktop Shortcut for you...

    echo $desktop = [Environment]::GetFolderPath('Desktop'^) > "%PS_SCRIPT%"
    echo $s = ^(New-Object -COM WScript.Shell^).CreateShortcut^("$desktop\Start Clinical Processor.lnk"^) >> "%PS_SCRIPT%"
    echo $s.TargetPath = '%~dp0Start_Windows.bat' >> "%PS_SCRIPT%"
    echo $s.WorkingDirectory = '%~dp0' >> "%PS_SCRIPT%"
    echo $s.IconLocation = 'shell32.dll,238' >> "%PS_SCRIPT%"
    echo $s.Save^(^) >> "%PS_SCRIPT%"

    powershell -ExecutionPolicy Bypass -File "%PS_SCRIPT%"
    
    del "%PS_SCRIPT%" >nul 2>&1

    if exist "%USERPROFILE%\Desktop\Start Clinical Processor.lnk" (
        echo    [OK] Shortcut created on Desktop!
        echo. > .shortcut_created
    ) else (
        echo    [!] Information: Could not create desktop shortcut automatically.
        echo        (You can right-click 'Start_Windows.bat' -^> Send to -^> Desktop to do it manually^)
    )
)

:: -------------------------------------------------------------
:: STEP B: CALL CHAINED SCRIPTS
:: -------------------------------------------------------------

call scripts\01_check_python.bat
if %errorlevel% neq 0 goto :failed

call scripts\02_setup_env.bat
if %errorlevel% neq 0 goto :failed

call scripts\03_check_config.bat
if %errorlevel% neq 0 goto :failed

:: -------------------------------------------------------------
:: STEP C: LAUNCH APP
:: -------------------------------------------------------------
echo.
echo [4/4] Everything looks good. Launching Application...
echo.

:: Activate env just in case (though setup_env does it for its session, we need it here)
call .venv\Scripts\activate.bat

python app_webview.py

echo.
echo Application Closed.
pause
exit

:failed
echo.
echo ==========================================================
echo [FAILED] The setup could not complete.
echo Please review the errors above.
echo ==========================================================
pause

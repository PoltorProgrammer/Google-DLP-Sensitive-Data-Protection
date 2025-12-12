@echo off
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
set "TARGET_SCRIPT=%~dp0Start_App.bat"
set "SHORTCUT_NAME=%USERPROFILE%\Desktop\Start Clinical Processor.lnk"

:: Use a marker file to know if we already tried creating a shortcut
if not exist ".shortcut_created" (
    echo [0/4] Creating Desktop Shortcut for you...
    
    powershell -Command "$s=(New-Object -COM WScript.Shell).CreateShortcut('%SHORTCUT_NAME%');$s.TargetPath='%TARGET_SCRIPT%';$s.WorkingDirectory='%~dp0';$s.IconLocation='shell32.dll,238';$s.Save()"
    
    if exist "%SHORTCUT_NAME%" (
        echo    [OK] Shortcut created on Desktop!
        echo. > .shortcut_created
    ) else (
        echo    [!] Information: Could not create desktop shortcut automatically.
        echo        (You can right-click 'Start_App.bat' -^> Send to -^> Desktop to do it manually^)
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

python batch_processor_gui.py

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

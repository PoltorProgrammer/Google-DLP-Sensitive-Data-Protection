#!/bin/bash

# Get the directory where the script is stored
DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )" && pwd )"
cd "$DIR"

# Clear terminal
clear
echo "=========================================================="
echo "          CLINICAL DOCUMENT PROCESSOR - LAUNCHER"
echo "=========================================================="
echo ""
echo "Hello! Checking your system to make sure everything is ready..."

# -------------------------------------------------------------
# STEP 0: CREATE DESKTOP SHORTCUT (Mac Only)
# -------------------------------------------------------------
if [[ "$OSTYPE" == "darwin"* ]]; then
    SHORTCUT_MARKER=".shortcut_created"
    
    if [ ! -f "$SHORTCUT_MARKER" ]; then
        echo "[0/4] Creating Desktop Shortcut..."
        
        # AppleScript to create alias on Desktop
        SCRIPT_PATH="$DIR/Start_Mac.command"
        USER_DESKTOP="$HOME/Desktop"
        
        # We use osascript to tell Finder to create the alias
        osascript -e "tell application \"Finder\" to make alias file to POSIX file \"$SCRIPT_PATH\" at POSIX file \"$USER_DESKTOP\"" > /dev/null 2>&1
        
        if [ $? -eq 0 ]; then
             echo "   [OK] Shortcut created on Desktop!"
             touch "$SHORTCUT_MARKER"
        else
             echo "   [!] Could not auto-create shortcut (Permissions/Finder error)."
        fi
    fi
fi

# -------------------------------------------------------------
# STEP A: EXECUTE SCRIPTS
# -------------------------------------------------------------

# Ensure helpers are executable
chmod +x scripts/*.sh 2>/dev/null

./scripts/01_check_python.sh
if [ $? -ne 0 ]; then exit 1; fi

./scripts/02_setup_env.sh
if [ $? -ne 0 ]; then exit 1; fi

./scripts/03_check_config.sh
if [ $? -ne 0 ]; then exit 1; fi

# -------------------------------------------------------------
# STEP B: LAUNCH APP
# -------------------------------------------------------------
echo ""
echo "[4/4] Everything looks good. Launching Application..."
echo ""

source .venv/bin/activate
python batch_processor_gui.py

echo ""
echo "Application Closed."

# Keep window open if double-clicked
read -p "Press Enter to close..."

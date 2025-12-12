#!/bin/bash

# Ensure executed from current directory
cd "$(dirname "$0")"

clear
echo "=========================================================="
echo "          CLINICAL DOCUMENT PROCESSOR - LAUNCHER"
echo "=========================================================="
echo ""
echo "Hello! Checking your system to make sure everything is ready..."

# -------------------------------------------------------------
# STEP A: EXECUTE SCRIPTS
# -------------------------------------------------------------

# Make scripts executable just in case
chmod +x scripts/*.sh

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
read -p "Press Enter to close..."

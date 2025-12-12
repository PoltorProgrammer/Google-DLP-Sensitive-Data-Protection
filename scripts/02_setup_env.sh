#!/bin/bash

echo ""
echo "[2/4] Checking Virtual Environment & Libraries..."

if [ ! -d ".venv" ]; then
    echo "   [!] No environment found. Creating one now..."
    python3 -m venv .venv
    if [ $? -ne 0 ]; then
        echo "   [ERROR] Failed to create virtual environment."
        echo "   On Linux, you might need: sudo apt install python3-venv"
        read -p "Press Enter to exit..."
        exit 1
    fi
else
    echo "   [OK] Virtual environment exists."
fi

# Activate environment
source .venv/bin/activate

# Install Requirements
echo "   ... Verifying libraries ..."
pip install -r requirements.txt > /dev/null 2>&1
if [ $? -ne 0 ]; then
    echo "   [ERROR] Failed to install requirements."
    echo "   Please check your internet connection."
    read -p "Press Enter to exit..."
    exit 1
fi

echo "   [OK] Environment is ready."
exit 0

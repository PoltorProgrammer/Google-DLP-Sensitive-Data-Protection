#!/bin/bash

echo ""
echo "[1/4] Checking Python System Requirements..."

# Check for Python 3
if ! command -v python3 &> /dev/null; then
    echo "   [X] Python 3 not found."
    echo "   [!] Attempting to detect OS and guide installation..."

    if [[ "$OSTYPE" == "darwin"* ]]; then
        # macOS
        echo "   Please install Python 3. You can download it from https://www.python.org/downloads/"
        echo "   Or if you have Homebrew: brew install python"
    elif [[ "$OSTYPE" == "linux-gnu"* ]]; then
        # Linux
        echo "   Please install Python 3 via your package manager."
        echo "   Example (Ubuntu/Debian): sudo apt update && sudo apt install python3 python3-venv python3-tk"
    else
        echo "   Unknown OS. Please install Python 3 manually from https://www.python.org/downloads/"
    fi
    
    # Wait for user input before exit
    read -p "Press Enter to exit..."
    exit 1
fi

echo "   [OK] Python 3 is ready."
exit 0

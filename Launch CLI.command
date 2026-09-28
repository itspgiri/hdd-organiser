#!/bin/bash
# Get the directory where this script is located
DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )" && pwd )"
cd "$DIR" || exit 1

echo "Initializing Drive Organizer CLI..."

# Check if venv is fully initialized, if not create and provision it
if [ ! -f ".venv/bin/activate" ] || [ ! -f ".venv/.deps_installed" ]; then
    echo "Creating virtual environment for the first time..."
    python3 -m venv .venv || { rm -rf .venv; echo "Failed to create .venv"; exit 1; }
    
    # Install dependencies
    source .venv/bin/activate
    if pip install -i https://pypi.org/simple/ -r requirements.txt; then
        touch .venv/.deps_installed
    else
        echo "Warning: dependency installation failed."
    fi
else
    source .venv/bin/activate
fi

# Run the python script interactively in CLI mode
python3 main.py --cli "$@"

# Keep terminal open if there's an error or it finishes
echo ""
echo "Press any key to close this window..."
read -n 1

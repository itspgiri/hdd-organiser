#!/bin/bash
# Get the directory where this script is located
DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )" && pwd )"
cd "$DIR"

echo "Initializing Drive Organizer..."

# Check if venv exists, if not create it
if [ ! -d ".venv" ]; then
    echo "Creating virtual environment for the first time..."
    python3 -m venv .venv
    
    # Install dependencies
    source .venv/bin/activate
    pip install -i https://pypi.org/simple/ -r requirements.txt
else
    source .venv/bin/activate
fi

# Run the python script interactively
python3 organizer.py

# Keep terminal open if there's an error or it finishes
echo ""
echo "Press any key to close this window..."
read -n 1

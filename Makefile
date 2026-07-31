# Drive Organizer Makefile

PYTHON := .venv/bin/python3
PIP := .venv/bin/pip

.PHONY: all setup run cli preview copy clean help

help:
	@echo "Drive Organizer Commands:"
	@echo "  make setup        - Create virtualenv and install dependencies"
	@echo "  make run          - Launch Desktop GUI application"
	@echo "  make cli          - Launch Interactive CLI mode"
	@echo "  make preview      - Run dry-run preview (Usage: make preview SOURCE=/path DEST=/path)"
	@echo "  make copy         - Run full copy transfer (Usage: make copy SOURCE=/path DEST=/path)"
	@echo "  make clean        - Remove temporary files, caches, and pyc files"

setup:
	@if [ ! -d ".venv" ]; then \
		echo "Creating virtual environment..."; \
		python3 -m venv .venv; \
	fi
	@echo "Installing dependencies..."
	@$(PIP) install -r requirements.txt

run: setup
	@$(PYTHON) main.py

cli: setup
	@$(PYTHON) main.py --cli

preview: setup
	@if [ -z "$(SOURCE)" ] || [ -z "$(DEST)" ]; then \
		echo "Error: Please specify SOURCE and DEST. Example: make preview SOURCE=/path/to/source DEST=/path/to/dest"; \
		exit 1; \
	fi
	@$(PYTHON) main.py --cli "$(SOURCE)" "$(DEST)" --preview

copy: setup
	@if [ -z "$(SOURCE)" ] || [ -z "$(DEST)" ]; then \
		echo "Error: Please specify SOURCE and DEST. Example: make copy SOURCE=/path/to/source DEST=/path/to/dest"; \
		exit 1; \
	fi
	@$(PYTHON) main.py --cli "$(SOURCE)" "$(DEST)" --copy

clean:
	@echo "Cleaning up Python cache files..."
	@find . -type d -name "__pycache__" -exec rm -rf {} + 2>/dev/null || true
	@find . -type f -name "*.pyc" -delete 2>/dev/null || true
	@find . -type f -name ".DS_Store" -delete 2>/dev/null || true
	@echo "Clean completed."

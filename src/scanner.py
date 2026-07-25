import os
from typing import List, Dict, Set, Tuple
from .categorizer import Categorizer

GARBAGE_FILES = {
    ".DS_Store", ".localized", "Thumbs.db", ".Spotlight-V100", ".fseventsd"
}

GARBAGE_PREFIXES = (
    "._", "~$"
)

SKIP_SYSTEM_DIRS = {
    ".Trash", ".thumbnails", ".fseventsd", ".Spotlight-V100", ".Trashes",
    "node_modules", ".git", "__pycache__", ".venv", "venv", ".cache", "Caches", ".tmp",
    ".Duplicates_Trash"
}

class Scanner:
    def __init__(self, categorizer: Categorizer):
        self.categorizer = categorizer
        self.projects_found: List[str] = []
        self.files_to_process: List[str] = []
        self.ignored_garbage_count = 0
        self.garbage_breakdown: Dict[str, int] = {}
        self.garbage_samples: List[str] = []

    def is_garbage(self, filename: str) -> bool:
        if filename in GARBAGE_FILES:
            return True
        if filename.startswith(GARBAGE_PREFIXES):
            return True
        return False

    def classify_garbage(self, filename: str) -> str:
        if filename.startswith("._"):
            return "macOS AppleDouble Sidecars (._*)"
        if filename.startswith("~$"):
            return "Office Temp / Lock Files (~$)"
        if filename in (".DS_Store", ".localized"):
            return "macOS Finder (.DS_Store / .localized)"
        if filename in ("Thumbs.db", "Desktop.ini"):
            return "Windows System Junk (Thumbs.db)"
        return "Other System Garbage"

    def scan_directory(self, source_path, excluded_projects: Set[str] = None, progress_cb = None):
        """Recursively scan directory or multiple directories, finding files and project folders."""
        excluded = excluded_projects or set()
        if isinstance(source_path, list):
            sources = source_path
        else:
            sources = [p.strip() for p in source_path.split(',') if p.strip()]

        scan_count = 0
        for src in sources:
            if not os.path.exists(src):
                continue
            for root, dirs, files in os.walk(src):
                # 1. Prune skipped system / heavy build directories in-place BEFORE entering them
                dirs[:] = [d for d in dirs if d not in SKIP_SYSTEM_DIRS]

                # 2. Check if this is a Code Project
                items_set = set(dirs) | set(files)
                if root != src and root not in excluded and self.categorizer.is_project_root(root, items_set):
                    self.projects_found.append(root)
                    dirs.clear() # Do not traverse INSIDE the project!
                    continue

                # 3. Otherwise, process individual files
                for file in files:
                    if self.is_garbage(file):
                        self.ignored_garbage_count += 1
                        g_type = self.classify_garbage(file)
                        self.garbage_breakdown[g_type] = self.garbage_breakdown.get(g_type, 0) + 1
                        if len(self.garbage_samples) < 25:
                            self.garbage_samples.append(os.path.join(root, file))
                        continue
                        
                    full_path = os.path.join(root, file)
                    self.files_to_process.append(full_path)
                    scan_count += 1

                    if progress_cb and (scan_count % 100 == 0):
                        progress_cb(0, 0, f"🔍 Scanning: {len(self.files_to_process)} files found... ({os.path.basename(root)})")



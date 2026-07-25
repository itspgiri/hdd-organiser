import os
from typing import List, Dict, Set, Tuple
from .categorizer import Categorizer

GARBAGE_FILES = {
    ".DS_Store", ".localized", "Thumbs.db", ".Spotlight-V100", ".fseventsd"
}

GARBAGE_PREFIXES = (
    "._", "~$"
)

class Scanner:
    def __init__(self, categorizer: Categorizer):
        self.categorizer = categorizer
        self.projects_found: List[str] = []
        self.files_to_process: List[str] = []
        self.ignored_garbage_count = 0

    def is_garbage(self, filename: str) -> bool:
        if filename in GARBAGE_FILES:
            return True
        if filename.startswith(GARBAGE_PREFIXES):
            return True
        return False

    def scan_directory(self, source_path: str, excluded_projects: Set[str] = None):
        """Recursively scan directory, finding files and project folders."""
        excluded = excluded_projects or set()
        for root, dirs, files in os.walk(source_path):
            
            # 1. Skip system folders completely
            if os.path.basename(root) in {".Trash", ".thumbnails"}:
                dirs.clear() # Stop walking this branch
                continue

            # 2. Check if this is a Code Project (subdirectories only, not source root itself, and not excluded)
            if root != source_path and root not in excluded and self.categorizer.is_project_root(root):
                self.projects_found.append(root)
                dirs.clear() # Do not traverse INSIDE the project!
                continue

            # 3. Otherwise, process individual files
            for file in files:
                if self.is_garbage(file):
                    self.ignored_garbage_count += 1
                    continue
                    
                full_path = os.path.join(root, file)
                self.files_to_process.append(full_path)

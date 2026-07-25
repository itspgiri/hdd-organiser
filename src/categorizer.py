import os
import re
from typing import Optional
import json

class Categorizer:
    def __init__(self, config_path: str):
        with open(config_path, 'r') as f:
            self.config = json.load(f)
            
        self.categories = self.config.get("categories", {})
        self.project_markers = set(self.config.get("project_markers", []))
        
        # Invert categories for faster lookup {".jpg": "Media"}
        self.ext_to_category = {}
        for cat, exts in self.categories.items():
            # If it's a Media category (like Media/Photos), we want it all to go to "Media"
            # as requested by the user.
            target_cat = "Media" if cat.startswith("Media/") else cat
            
            for ext in exts:
                self.ext_to_category[ext.lower()] = target_cat
                
        # Precompile common date matching regex for filenames like IMG_20230615.jpg
        self.date_regex = re.compile(r"(?:19|20)\d{2}[-_\.]?(?:0[1-9]|1[0-2])[-_\.]?(?:0[1-9]|[12][0-9]|3[01])")
        self.screenshot_regex = re.compile(
            r"(screen\s*shot|screenshot|screen_shot|captura\s*de\s*pantalla|bildschirmfoto|capture\s*d['’]?écran)",
            re.IGNORECASE
        )

    def is_project_root(self, folder_path: str, items_set: Optional[set] = None) -> bool:
        """Checks if a folder contains any project markers."""
        try:
            if items_set is not None:
                return not self.project_markers.isdisjoint(items_set)
            items = os.listdir(folder_path)
            return not self.project_markers.isdisjoint(items)
        except (PermissionError, OSError):
            return False


    def is_screenshot(self, filename: str) -> bool:
        """Returns True if filename matches screenshot naming conventions across OSs."""
        return bool(self.screenshot_regex.search(filename))

    def get_file_category(self, filename: str) -> str:
        """Returns the primary category for a given file based on its extension."""
        ext = "." + filename.rsplit('.', 1)[-1].lower() if '.' in filename else ""
        return self.ext_to_category.get(ext, "Unsorted")

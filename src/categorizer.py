import os
import re
import json
import unicodedata
from typing import Optional

COMPOUND_ARCHIVE_EXTS = (
    ".tar.gz", ".tar.bz2", ".tar.xz", ".tar.zst",
)


def split_filename_ext(filename: str) -> tuple[str, str]:
    """Splits filename into (stem, ext), preserving compound archive extensions like .tar.gz."""
    base = os.path.basename(filename)
    lower = base.lower()
    for comp in COMPOUND_ARCHIVE_EXTS:
        if lower.endswith(comp) and len(base) > len(comp):
            return base[:-len(comp)], base[-len(comp):]
    return os.path.splitext(base)


class Categorizer:
    def __init__(self, config_path: str):
        with open(config_path, 'r', encoding='utf-8') as f:
            self.config = json.load(f)
            
        self.categories = self.config.get("categories", {})
        self.project_markers = set(self.config.get("project_markers", []))
        self.photo_extensions = {
            ext.lower() for ext in self.categories.get("Media/Photos", [])
        }
        self.video_and_raw_extensions = {
            ext.lower()
            for cat_name in ("Media/Videos", "Media/RAW")
            for ext in self.categories.get(cat_name, [])
        }
        
        # Invert categories for faster lookup {".jpg": "Media"}
        self.ext_to_category = {}
        for cat, exts in self.categories.items():
            # If it's a Media category (like Media/Photos), we want it all to go to "Media"
            # as requested by the user.
            target_cat = "Media" if cat.startswith("Media/") else cat
            
            for ext in exts:
                self.ext_to_category[ext.lower()] = target_cat
                
        # Precompile common date matching regex for filenames like IMG_20230615.jpg.
        # Non-digit lookarounds prevent matching random 8-digit substrings inside
        # 13-digit millisecond timestamps, Discord/Twitter snowflake IDs, or UUIDs,
        # and backreference \1 requires both separators to match.
        self.date_regex = re.compile(
            r"(?<!\d)(?:19|20)\d{2}([-_\.]?)(?:0[1-9]|1[0-2])\1(?:0[1-9]|[12][0-9]|3[01])(?!\d)"
        )
        self.screenshot_regex = re.compile(
            r"(screen[\s_-]*shot|captura\s*de\s*pantalla|bildschirmfoto|capture\s*d['’]?écran)",
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
        """Returns True if filename matches screenshot naming conventions across OSs.

        Normalizes macOS NFD filenames to NFC first so decomposed characters like
        'e\\u0301' in 'Capture d’écran' match reliably, and excludes video/RAW
        containers so screen recordings or RAW files are not filed as still screenshots.
        """
        base = unicodedata.normalize("NFC", os.path.basename(filename))
        _, ext = split_filename_ext(base)
        if ext and ext.lower() in self.video_and_raw_extensions:
            return False
        return bool(self.screenshot_regex.search(base))

    def get_file_category(self, filename: str) -> str:
        """Returns the primary category for a given file based on its extension."""
        base = os.path.basename(filename).strip()
        if not base:
            return "Unsorted"
        _, ext = split_filename_ext(base)
        ext = ext.lower()
        if not ext and '.' in base[1:]:
            ext = "." + base.rsplit('.', 1)[-1].lower()
        return self.ext_to_category.get(ext, "Unsorted")

import os
import re
import sys
import io
import datetime
import exifread
from typing import Optional

_DUMMY_STDERR = io.StringIO()

class DateExtractor:
    def __init__(self, categorizer):
        self.categorizer = categorizer
        # Common month names for folder generation
        self.months = (
            "January", "February", "March", "April", "May", "June",
            "July", "August", "September", "October", "November", "December"
        )

    def extract_date(self, filepath: str) -> tuple[Optional[str], Optional[str]]:
        """
        Extracts the (Year, MonthName) from a file using a 6-layer priority approach.
        Returns (None, None) if completely undetectable.
        """
        filename = os.path.basename(filepath)
        
        # 1 & 2. Try EXIF for Media files
        date_str = self._get_exif_date(filepath)
        if date_str:
            parsed = self._parse_exif_date(date_str)
            if parsed[0]:
                return parsed
                
        # 3. Filename parsing (using precompiled regex from categorizer)
        match = self.categorizer.date_regex.search(filename)
        if match:
            date_str = match.group(0).replace('-', '').replace('_', '').replace('.', '')
            if len(date_str) == 8:
                try:
                    dt = datetime.datetime.strptime(date_str, "%Y%m%d")
                    return str(dt.year), self.months[dt.month - 1]
                except ValueError:
                    pass
                    
        # 4 & 5. macOS Filesystem Fallbacks
        try:
            stat = os.stat(filepath)
            # Use birthtime if available (macOS), else mtime
            timestamp = getattr(stat, 'st_birthtime', stat.st_mtime)
            dt = datetime.datetime.fromtimestamp(timestamp)
            # Sanity check (no dates from 1970)
            if dt.year > 1980:
                return str(dt.year), self.months[dt.month - 1]
        except (OSError, ValueError):
            pass
            
        # 6. Fallback
        return None, None

    EXIF_EXTENSIONS = {".jpg", ".jpeg", ".tif", ".tiff", ".cr2", ".nef", ".arw", ".dng"}

    def _get_exif_date(self, filepath: str) -> Optional[str]:
        """Lightweight EXIF extraction reading only headers."""
        ext = "." + filepath.rsplit('.', 1)[-1].lower() if '.' in filepath else ""
        if ext not in self.EXIF_EXTENSIONS:
            return None

        try:
            old_stderr = sys.stderr
            sys.stderr = _DUMMY_STDERR
            try:
                with open(filepath, 'rb') as f:
                    tags = exifread.process_file(f, stop_tag="EXIF DateTimeOriginal", details=False, log_level="CRITICAL")
            finally:
                sys.stderr = old_stderr

            # Priority: DateTimeOriginal > DateTimeDigitized > Image DateTime
            if 'EXIF DateTimeOriginal' in tags:
                return str(tags['EXIF DateTimeOriginal'])
            elif 'EXIF DateTimeDigitized' in tags:
                return str(tags['EXIF DateTimeDigitized'])
            elif 'Image DateTime' in tags:
                return str(tags['Image DateTime'])
        except Exception:
            pass
        return None

    def _parse_exif_date(self, date_str: str) -> tuple[Optional[str], Optional[str]]:
        """Parses EXIF date string (YYYY:MM:DD HH:MM:SS)"""
        try:
            # e.g., '2023:06:15 14:32:01'
            parts = date_str.split(' ')[0].split(':')
            if len(parts) >= 2:
                year = parts[0]
                month_num = int(parts[1])
                if 1 <= month_num <= 12 and 1980 <= int(year) <= 2100:
                    return year, self.months[month_num - 1]
        except (ValueError, IndexError):
            pass
        return None, None

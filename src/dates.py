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
        
        # 1 & 2. Try EXIF, Video Header, or PDF CreationDate metadata
        date_str = self._get_exif_date(filepath) or self._get_video_date(filepath) or self._get_pdf_date(filepath)
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
                    
        # 4 & 5. macOS Filesystem Fallbacks (prefer earliest real historical date: min of mtime and birthtime)
        try:
            stat = os.stat(filepath)
            btime = getattr(stat, 'st_birthtime', stat.st_mtime)
            mtime = stat.st_mtime
            # Select the earliest valid historical timestamp (prevents newly copied birthtime from overriding original mtime)
            valid_ts = [t for t in (mtime, btime) if t > 315532800] # 1980-01-01
            if valid_ts:
                timestamp = min(valid_ts)
                dt = datetime.datetime.fromtimestamp(timestamp)
                if dt.year > 1980:
                    return str(dt.year), self.months[dt.month - 1]
        except (OSError, ValueError):
            pass

            
        # 6. Fallback
        return None, None

    EXIF_EXTENSIONS = {".jpg", ".jpeg", ".tif", ".tiff", ".cr2", ".nef", ".arw", ".dng"}
    VIDEO_EXTENSIONS = {".mp4", ".mov", ".m4v", ".3gp"}

    def _get_video_date(self, filepath: str) -> Optional[str]:
        """Lightweight QuickTime / MP4 creation date extraction from binary atom header."""
        ext = "." + filepath.rsplit('.', 1)[-1].lower() if '.' in filepath else ""
        if ext not in self.VIDEO_EXTENSIONS:
            return None
        try:
            with open(filepath, 'rb') as f:
                header = f.read(65536)
                mvhd_idx = header.find(b'mvhd')
                if mvhd_idx != -1 and mvhd_idx + 16 <= len(header):
                    import struct
                    creation_bytes = header[mvhd_idx + 8 : mvhd_idx + 12]
                    creation_time = struct.unpack(">I", creation_bytes)[0]
                    if creation_time > 2082844800:
                        unix_time = creation_time - 2082844800
                        dt = datetime.datetime.fromtimestamp(unix_time, tz=datetime.timezone.utc)
                        if 1980 <= dt.year <= 2100:
                            return f"{dt.year}:{dt.month:02d}:01 00:00:00"
        except Exception:
            pass
        return None

    EXIF_EXTENSIONS = {".jpg", ".jpeg", ".tif", ".tiff", ".cr2", ".nef", ".arw", ".dng"}
    VIDEO_EXTENSIONS = {".mp4", ".mov", ".m4v", ".3gp"}
    PDF_DATE_REGEX = re.compile(r'D:(20\d{6})')

    def _get_pdf_date(self, filepath: str) -> Optional[str]:
        """Lightweight PDF document CreationDate extraction from PDF trailer header/footer."""
        ext = "." + filepath.rsplit('.', 1)[-1].lower() if '.' in filepath else ""
        if ext != ".pdf":
            return None
        try:
            with open(filepath, 'rb') as f:
                head = f.read(8192)
                f.seek(0, os.SEEK_END)
                size = f.tell()
                tail_size = min(size, 8192)
                f.seek(size - tail_size, os.SEEK_SET)
                tail = f.read(tail_size)
                content = head + tail
                idx = content.find(b'/CreationDate')
                if idx != -1:
                    sub = content[idx:idx+40].decode('latin1', errors='ignore')
                    match = self.PDF_DATE_REGEX.search(sub)
                    if match:
                        d_str = match.group(1)
                        return f"{d_str[:4]}:{d_str[4:6]}:01 00:00:00"
        except Exception:
            pass
        return None


    def _get_exif_date(self, filepath: str) -> Optional[str]:
        """Lightweight EXIF extraction reading only headers."""
        ext = "." + filepath.rsplit('.', 1)[-1].lower() if '.' in filepath else ""
        if ext not in self.EXIF_EXTENSIONS:
            return None

        try:
            with open(filepath, 'rb') as f:
                tags = exifread.process_file(f, stop_tag="EXIF DateTimeOriginal", details=False, log_level="CRITICAL")

            # Priority: DateTimeOriginal > DateTimeDigitized > Image DateTime > GPS Date
            if 'EXIF DateTimeOriginal' in tags:
                return str(tags['EXIF DateTimeOriginal'])
            elif 'EXIF DateTimeDigitized' in tags:
                return str(tags['EXIF DateTimeDigitized'])
            elif 'Image DateTime' in tags:
                return str(tags['Image DateTime'])
            elif 'GPS GPSDate' in tags:
                return str(tags['GPS GPSDate']).replace('-', ':')
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

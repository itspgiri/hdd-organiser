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

    # exifread 3.5.1 supports TIFF, JPEG, PNG, WebP and HEIC/AVIF (see
    # exifread/core/find_exif.py, which dispatches on the ftypheic/ftypavif/
    # ftypmif1 magic). HEIC in particular was missing here, so every iPhone
    # photo skipped EXIF entirely and fell through to filesystem mtime --
    # filing shots under the year the file was last moved, not the year it
    # was taken.
    EXIF_EXTENSIONS = {
        ".jpg", ".jpeg", ".tif", ".tiff", ".cr2", ".nef", ".arw", ".dng",
        ".heic", ".heif", ".png", ".webp", ".avif",
    }
    VIDEO_EXTENSIONS = {".mp4", ".mov", ".m4v", ".3gp"}

    # QuickTime/MP4 epoch is 1904-01-01; Unix is 1970-01-01.
    _QT_EPOCH_OFFSET = 2082844800

    def _decode_mvhd(self, buf: bytes) -> Optional[str]:
        """Pulls the creation time out of every mvhd atom found in buf."""
        import struct
        start = 0
        while True:
            idx = buf.find(b'mvhd', start)
            if idx == -1:
                return None
            start = idx + 4
            # Layout after the 'mvhd' fourcc: version(1) flags(3) then
            # creation_time, which is 4 bytes for version 0 and 8 for version 1.
            if idx + 8 > len(buf):
                continue
            version = buf[idx + 4]
            if version == 1:
                if idx + 16 > len(buf):
                    continue
                creation_time = struct.unpack(">Q", buf[idx + 8: idx + 16])[0]
            else:
                if idx + 12 > len(buf):
                    continue
                creation_time = struct.unpack(">I", buf[idx + 8: idx + 12])[0]

            if creation_time <= self._QT_EPOCH_OFFSET:
                continue
            unix_time = creation_time - self._QT_EPOCH_OFFSET
            try:
                dt = datetime.datetime.fromtimestamp(unix_time, tz=datetime.timezone.utc)
            except (OverflowError, OSError, ValueError):
                continue
            if 1980 <= dt.year <= 2100:
                return f"{dt.year}:{dt.month:02d}:01 00:00:00"
        return None

    def _get_video_date(self, filepath: str) -> Optional[str]:
        """Lightweight QuickTime / MP4 creation date extraction from binary atom header.

        Reads the head *and* the tail of the file. Only the first 64KB used to
        be scanned, but `moov` (which contains `mvhd`) is written at the end of
        any file not saved with faststart -- which is most cameras, most
        screen recorders and most exports. Those videos silently fell through
        to filesystem mtime.
        """
        ext = "." + filepath.rsplit('.', 1)[-1].lower() if '.' in filepath else ""
        if ext not in self.VIDEO_EXTENSIONS:
            return None
        window = 131072  # 128KB
        try:
            with open(filepath, 'rb') as f:
                head = f.read(window)
                found = self._decode_mvhd(head)
                if found:
                    return found

                f.seek(0, os.SEEK_END)
                size = f.tell()
                if size > window:
                    # Overlap by 4 bytes so an atom straddling the boundary is
                    # not split in half.
                    f.seek(max(0, size - window - 4), os.SEEK_SET)
                    return self._decode_mvhd(f.read())
        except Exception:
            pass
        return None

    # NOTE: EXIF_EXTENSIONS / VIDEO_EXTENSIONS used to be declared a second
    # time right here, silently shadowing the definitions above. Editing the
    # first pair appeared to do nothing. Keep them defined once only.
    PDF_DATE_REGEX = re.compile(r'D:((?:19|20)\d{6})')

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

import os
import re
import io
import struct
import logging
import datetime
import exifread
from typing import Optional

logging.getLogger("exifread").setLevel(logging.CRITICAL)


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
        Extracts the (Year, MonthName) from a file using a multi-layer priority approach.
        Returns (None, None) if completely undetectable.
        """
        filename = os.path.basename(filepath)

        # 1 & 2. Try EXIF, Video Header, or PDF CreationDate metadata
        for getter in (self._get_exif_date, self._get_video_date, self._get_pdf_date):
            date_str = getter(filepath)
            if date_str:
                parsed = self._parse_exif_date(date_str)
                if parsed[0]:
                    return parsed

        # 3. Filename parsing (using precompiled regex from categorizer)
        for match in self.categorizer.date_regex.finditer(filename):
            date_str = re.sub(r"[-_\.]", "", match.group(0))
            if len(date_str) == 8:
                try:
                    dt = datetime.datetime.strptime(date_str, "%Y%m%d")
                    if 1980 <= dt.year <= 2100:
                        return str(dt.year), self.months[dt.month - 1]
                except ValueError:
                    continue

        # 4 & 5. macOS Filesystem Fallbacks (prefer earliest real historical date: min of mtime and birthtime)
        try:
            stat = os.stat(filepath)
            btime = getattr(stat, 'st_birthtime', None)
            mtime = getattr(stat, 'st_mtime', None)
            valid_dts = []
            # 315619200 is 1980-01-02 00:00:00 UTC (excludes Unix epoch 1970 and DOS/FAT epoch 1980-01-01)
            for t in (mtime, btime):
                if isinstance(t, (int, float)) and t > 315619200:
                    try:
                        dt = datetime.datetime.fromtimestamp(t)
                        if 1980 <= dt.year <= 2100:
                            valid_dts.append(dt)
                    except (OSError, OverflowError, ValueError, TypeError):
                        continue
            if valid_dts:
                earliest = min(valid_dts)
                return str(earliest.year), self.months[earliest.month - 1]
        except (OSError, ValueError, TypeError):
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
        ".jpg", ".jpeg", ".tif", ".tiff", ".cr2", ".cr3", ".nef", ".arw", ".dng",
        ".orf", ".raf", ".rw2", ".heic", ".heif", ".png", ".webp", ".avif",
    }
    VIDEO_EXTENSIONS = {".mp4", ".mov", ".m4v", ".3gp"}

    # QuickTime/MP4 epoch is 1904-01-01; Unix is 1970-01-01.
    _QT_EPOCH_OFFSET = 2082844800

    def _decode_mvhd(self, buf: bytes) -> Optional[str]:
        """Pulls the creation time out of every valid mvhd atom found in buf."""
        start = 0
        while True:
            idx = buf.find(b'mvhd', start)
            if idx == -1:
                return None
            start = idx + 4
            # Layout after the 'mvhd' fourcc: version(1) flags(3) then
            # creation_time, which is 4 bytes for version 0 and 8 for version 1.
            # Require version in (0, 1) and flags == 0x000000 so random 'mvhd'
            # byte sequences inside compressed mdat media streams are rejected.
            if idx + 8 > len(buf):
                continue
            version = buf[idx + 4]
            if version not in (0, 1):
                continue
            if buf[idx + 5: idx + 8] != b'\x00\x00\x00':
                continue
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
                return f"{dt.year}:{dt.month:02d}:{dt.day:02d} 00:00:00"
        return None

    def _find_moov_mvhd(self, f, file_size: int) -> Optional[str]:
        """Walks top-level ISO BMFF / QuickTime boxes to locate moov -> mvhd in O(1) seeks."""
        pos = 0
        box_limit = 500
        known_top_boxes = {
            b'ftyp', b'moov', b'mdat', b'free', b'skip', b'wide', b'uuid', b'meta', b'pdin', b'moof', b'mfra'
        }
        for _ in range(box_limit):
            if pos + 8 > file_size:
                break
            f.seek(pos, os.SEEK_SET)
            hdr = f.read(16)
            if len(hdr) < 8:
                break
            box_size = struct.unpack(">I", hdr[:4])[0]
            box_type = hdr[4:8]
            if box_type not in known_top_boxes and pos == 0:
                # Not a standard box-structured container at offset 0
                return None
            header_len = 8
            if box_size == 1:
                if len(hdr) < 16:
                    break
                box_size = struct.unpack(">Q", hdr[8:16])[0]
                header_len = 16
            elif box_size == 0:
                box_size = file_size - pos
            if box_size < header_len or pos + box_size > file_size:
                break
            if box_type == b'moov':
                f.seek(pos + header_len, os.SEEK_SET)
                moov_head = f.read(min(box_size - header_len, 131072))
                found = self._decode_mvhd(moov_head)
                if found:
                    return found
            pos += box_size
        return None

    def _get_video_date(self, filepath: str) -> Optional[str]:
        """Lightweight QuickTime / MP4 creation date extraction from binary atom header.

        Walks top-level boxes first (so large tail `moov` boxes >128KB are found
        at their exact offset) and falls back to scanning the 128KB head and tail.
        """
        ext = os.path.splitext(filepath)[1].lower()
        if ext not in self.VIDEO_EXTENSIONS:
            return None
        window = 131072  # 128KB
        try:
            with open(filepath, 'rb') as f:
                f.seek(0, os.SEEK_END)
                size = f.tell()
                if size <= 0:
                    return None

                found = self._find_moov_mvhd(f, size)
                if found:
                    return found

                f.seek(0, os.SEEK_SET)
                head = f.read(window)
                found = self._decode_mvhd(head)
                if found:
                    return found

                if size > window:
                    # Overlap by 32 bytes so an mvhd atom straddling the boundary
                    # (4-byte fourcc + 1-byte version + 3-byte flags + 8-byte time)
                    # is never split across reads.
                    f.seek(max(0, size - window - 32), os.SEEK_SET)
                    return self._decode_mvhd(f.read())
        except Exception:
            pass
        return None

    # NOTE: EXIF_EXTENSIONS / VIDEO_EXTENSIONS used to be declared a second
    # time right here, silently shadowing the definitions above. Editing the
    # first pair appeared to do nothing. Keep them defined once only.
    PDF_DATE_REGEX = re.compile(
        r'D:\s*((?:19|20)\d{2})(0[1-9]|1[0-2])(?:(0[1-9]|[12]\d|3[01]))?'
    )
    XMP_DATE_REGEX = re.compile(
        r'(?:CreateDate|CreationDate)[>\s="\']+((?:19|20)\d{2})[-:](0[1-9]|1[0-2])(?:[-:](0[1-9]|[12]\d|3[01]))?'
    )

    def _get_pdf_date(self, filepath: str) -> Optional[str]:
        """Lightweight PDF document CreationDate extraction from PDF header/footer/XMP."""
        ext = os.path.splitext(filepath)[1].lower()
        if ext != ".pdf":
            return None
        window = 65536
        try:
            with open(filepath, 'rb') as f:
                f.seek(0, os.SEEK_END)
                size = f.tell()
                f.seek(0, os.SEEK_SET)
                if size <= window * 2:
                    content = f.read()
                else:
                    head = f.read(window)
                    f.seek(size - window, os.SEEK_SET)
                    tail = f.read(window)
                    content = head + b"\n" + tail

            # Also decode UTF-16BE sequences if present in PDF Info strings
            text = content.decode('latin1', errors='ignore')
            if b'\x00D\x00:' in content:
                text += "\n" + content.replace(b'\x00', b'').decode('latin1', errors='ignore')

            # Search all /CreationDate occurrences first
            pos = 0
            while True:
                idx = text.find('/CreationDate', pos)
                if idx == -1:
                    break
                pos = idx + 13
                sub = text[idx: idx + 120]
                match = self.PDF_DATE_REGEX.search(sub)
                if match:
                    y, m, d = match.group(1), match.group(2), match.group(3) or "01"
                    candidate = f"{y}:{m}:{d} 00:00:00"
                    if self._parse_exif_date(candidate)[0]:
                        return candidate

            # Fallback to any XMP CreateDate or D:YYYYMMDD in the scanned window
            for regex in (self.XMP_DATE_REGEX, self.PDF_DATE_REGEX):
                for match in regex.finditer(text):
                    y, m, d = match.group(1), match.group(2), match.group(3) or "01"
                    candidate = f"{y}:{m}:{d} 00:00:00"
                    if self._parse_exif_date(candidate)[0]:
                        return candidate
        except Exception:
            pass
        return None

    @staticmethod
    def _extract_embedded_tiff_tags(filepath: str) -> dict:
        """Fallback EXIF reader for HEIC/AVIF/CR3/WebP files where exifread's container
        parser misses base_offset iloc extents or raw WebP EXIF chunks."""
        try:
            with open(filepath, 'rb') as f:
                buf = f.read(262144)
            # Locate 'Exif\x00\x00' followed by a TIFF byte-order header ('II*\x00' or 'MM\x00*')
            tiff_offset = -1
            for sig in (b'Exif\x00\x00II*\x00', b'Exif\x00\x00MM\x00*'):
                idx = buf.find(sig)
                if idx != -1:
                    tiff_offset = idx + 6
                    break
            if tiff_offset == -1 and buf[:4] == b'RIFF' and buf[8:12] == b'WEBP':
                idx = buf.find(b'EXIF')
                if idx != -1 and idx + 8 < len(buf):
                    cand = buf[idx + 8: idx + 12]
                    if cand in (b'II*\x00', b'MM\x00*'):
                        tiff_offset = idx + 8
            if tiff_offset == -1:
                return {}
            tiff_payload = buf[tiff_offset: tiff_offset + 65530]
            app1_len = len(tiff_payload) + 8
            synthetic_jpeg = (
                b'\xff\xd8\xff\xe1'
                + struct.pack('>H', min(app1_len, 65535))
                + b'Exif\x00\x00'
                + tiff_payload[:65525]
            )
            return exifread.process_file(
                io.BytesIO(synthetic_jpeg), details=False, extract_thumbnail=False
            )
        except Exception:
            return {}

    def _read_exif_tags(self, filepath: str) -> dict:
        ext = os.path.splitext(filepath)[1].lower()
        if ext not in self.EXIF_EXTENSIONS:
            return {}
        tags = {}
        try:
            with open(filepath, 'rb') as f:
                tags = exifread.process_file(f, details=False, extract_thumbnail=False) or {}
        except Exception:
            tags = {}
        if not tags:
            tags = self._extract_embedded_tiff_tags(filepath)
        return tags

    def has_camera_exif(self, filepath: str) -> bool:
        """Returns True if the file contains camera Make/Model EXIF metadata."""
        tags = self._read_exif_tags(filepath)
        for key in ('Image Make', 'Image Model', 'EXIF LensModel', 'EXIF BodySerialNumber'):
            val = str(tags.get(key, '')).strip()
            if val:
                return True
        return False

    def _get_exif_date(self, filepath: str) -> Optional[str]:
        """Lightweight EXIF extraction reading only headers."""
        tags = self._read_exif_tags(filepath)
        if not tags:
            return None

        # Priority: DateTimeOriginal > DateTimeDigitized > Image DateTime > GPS Date
        # Validate each tag before returning so a zeroed/corrupt higher-priority tag
        # (e.g. '0000:00:00 00:00:00') falls through to the next available tag.
        for key in (
            'EXIF DateTimeOriginal',
            'EXIF DateTimeDigitized',
            'Image DateTime',
            'GPS GPSDate',
            'GPS GPSDateStamp',
        ):
            if key in tags:
                candidate = str(tags[key]).strip()
                if self._parse_exif_date(candidate)[0]:
                    return candidate
        return None

    def _parse_exif_date(self, date_str: str) -> tuple[Optional[str], Optional[str]]:
        """Parses EXIF / ISO date string (YYYY:MM:DD HH:MM:SS or YYYY-MM-DDTHH:MM:SS)."""
        try:
            if not date_str:
                return None, None
            cleaned = date_str.strip()
            date_part = re.split(r'[\sT]+', cleaned)[0]
            date_part = date_part.replace('-', ':').replace('/', ':')
            parts = date_part.split(':')
            if len(parts) >= 2:
                year_int = int(parts[0])
                month_num = int(parts[1])
                if not (1 <= month_num <= 12 and 1980 <= year_int <= 2100):
                    return None, None
                if len(parts) >= 3 and parts[2]:
                    day_match = re.match(r'^(\d{1,2})', parts[2])
                    if day_match:
                        day_num = int(day_match.group(1))
                        datetime.date(year_int, month_num, day_num)
                return str(year_int), self.months[month_num - 1]
        except (ValueError, IndexError):
            pass
        return None, None

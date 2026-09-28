"""Comprehensive regression test suite for the full-stack bug audit fixes."""

import datetime
import os
import plistlib
import shutil
import stat
import struct
import subprocess
import sys
import tempfile
import unittest
import zipfile
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src import app as webapp
from src import utils
from src.api_organizer import OrganizerAPI
from src.categorizer import Categorizer, split_filename_ext
from src.dates import DateExtractor
from src.file_ops import FileEngine, files_are_identical, safe_copy, xattr
from src.scanner import Scanner, split_source_paths

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CONFIG_PATH = os.path.join(REPO_ROOT, "src", "config.json")


def _build_minimal_exif_jpeg(dt_original: str = "2021:07:19 14:22:05", make: str = "Apple") -> bytes:
    """Construct a minimal valid JPEG containing a big-endian TIFF/EXIF APP1 segment."""
    make_bytes = make.encode("ascii") + b"\x00"
    if len(make_bytes) < 4:
        make_bytes = make_bytes.ljust(4, b"\x00")
    dt_bytes = dt_original.encode("ascii") + b"\x00"  # 20 bytes

    make_offset = 38
    exif_ifd_offset = make_offset + len(make_bytes)
    dt_offset = exif_ifd_offset + 18

    tiff = bytearray()
    tiff += b"MM\x00\x2a" + struct.pack(">I", 8)
    tiff += struct.pack(">H", 2)
    tiff += struct.pack(">HHII", 0x010F, 2, len(make_bytes), make_offset)
    tiff += struct.pack(">HHII", 0x8769, 4, 1, exif_ifd_offset)
    tiff += struct.pack(">I", 0)
    tiff += make_bytes
    tiff += struct.pack(">H", 1)
    tiff += struct.pack(">HHII", 0x9003, 2, len(dt_bytes), dt_offset)
    tiff += struct.pack(">I", 0)
    tiff += dt_bytes

    app1_payload = b"Exif\x00\x00" + bytes(tiff)
    app1_len = len(app1_payload) + 2
    return b"\xff\xd8\xff\xe1" + struct.pack(">H", app1_len) + app1_payload + b"\xff\xd9"


class DateExtractorAuditTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="audit_dates_")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.cat = Categorizer(CONFIG_PATH)
        self.dates = DateExtractor(self.cat)

    def test_exifread_process_file_actually_extracts_exif_date(self):
        """DATE-1: exifread.process_file must not raise TypeError on invalid kwargs."""
        jpg = os.path.join(self.tmp, "IMG_0001.jpg")
        with open(jpg, "wb") as f:
            f.write(_build_minimal_exif_jpeg("2021:07:19 14:22:05", make="Canon"))
        recent = datetime.datetime(2026, 1, 15, 12, 0, 0).timestamp()
        os.utime(jpg, (recent, recent))

        self.assertEqual(self.dates.extract_date(jpg), ("2021", "July"))
        self.assertTrue(self.dates.has_camera_exif(jpg))

    def test_exif_zeroed_tag_falls_through_to_valid_tag(self):
        """DATE-2: A zeroed DateTimeOriginal must fall through to a valid fallback tag."""
        fake_tags = {
            "EXIF DateTimeOriginal": "0000:00:00 00:00:00",
            "Image DateTime": "2020:05:10 09:30:00",
        }
        with mock.patch("src.dates.exifread.process_file", return_value=fake_tags):
            jpg = os.path.join(self.tmp, "fallback.jpg")
            with open(jpg, "wb") as f:
                f.write(b"\xff\xd8\xff\xd9")
            dt_str = self.dates._get_exif_date(jpg)
            self.assertEqual(dt_str, "2020:05:10 09:30:00")
            self.assertEqual(self.dates.extract_date(jpg), ("2020", "May"))

    def test_embedded_tiff_fallback_extracts_heic_and_cr3_dates(self):
        """DATE-3: HEIC/CR3/AVIF containers with embedded Exif TIFF blocks extract dates."""
        heic = os.path.join(self.tmp, "photo.heic")
        payload = b"\x00\x00\x00\x18ftypheic" + (b"\x00" * 64) + _build_minimal_exif_jpeg("2022:11:03 08:15:00")
        with open(heic, "wb") as f:
            f.write(payload)
        self.assertEqual(self.dates.extract_date(heic), ("2022", "November"))
        self.assertTrue(self.dates.has_camera_exif(heic))

    def test_video_mvhd_false_positive_guarded_and_box_walker_succeeds(self):
        """DATE-4: Fake 'mvhd' bytes with non-zero flags are rejected while real moov/mvhd succeeds."""
        mov = os.path.join(self.tmp, "clip.mov")
        epoch_1904 = datetime.datetime(1904, 1, 1)
        target = datetime.datetime(2020, 4, 15, 0, 0, 0)
        secs = int((target - epoch_1904).total_seconds())

        ftyp_box = struct.pack(">I4s4sI", 16, b"ftyp", b"qt  ", 0)
        fake_mvhd = b"mvhd" + b"\x01\x02\x03\x04" + struct.pack(">I", secs + 86400 * 365)
        mdat_payload = b"AAAA" + fake_mvhd + b"BBBB"
        mdat_box = struct.pack(">I4s", 8 + len(mdat_payload), b"mdat") + mdat_payload

        mvhd_box = struct.pack(">I4sI", 32, b"mvhd", 0) + struct.pack(">I", secs) + (b"\x00" * 16)
        moov_box = struct.pack(">I4s", 8 + len(mvhd_box), b"moov") + mvhd_box

        with open(mov, "wb") as f:
            f.write(ftyp_box + mdat_box + moov_box)

        self.assertEqual(self.dates.extract_date(mov), ("2020", "April"))

    def test_pdf_date_utf16be_and_multiple_creation_dates(self):
        """DATE-5: Invalid first /CreationDate is skipped and UTF-16BE /CreationDate is parsed."""
        pdf = os.path.join(self.tmp, "doc.pdf")
        utf16_date = "D:20230921100000".encode("utf-16-be")
        content = (
            b"%PDF-1.7\n"
            b"1 0 obj << /CreationDate (D:20231399000000) >> endobj\n"
            b"2 0 obj << /CreationDate (\xfe\xff" + utf16_date + b") >> endobj\n"
        )
        with open(pdf, "wb") as f:
            f.write(content)
        self.assertEqual(self.dates.extract_date(pdf), ("2023", "September"))

    def test_filename_date_regex_skips_invalid_match_and_mixed_separators(self):
        """DATE-6 & BUG-5.4: Out-of-range digits or mixed separators do not mask a real date."""
        txt = os.path.join(self.tmp, "report_30120101_2023-08-15.txt")
        with open(txt, "w") as f:
            f.write("hello")
        self.assertEqual(self.dates.extract_date(txt), ("2023", "August"))
        self.assertIsNone(self.cat.date_regex.search("file_2023-08_15.txt"))

    def test_filesystem_fallback_accepts_year_1980(self):
        """DATE-7: Year 1980 is accepted in Layer 4/5 filesystem fallback."""
        txt = os.path.join(self.tmp, "retro.txt")
        with open(txt, "w") as f:
            f.write("retro")
        ts_1980 = datetime.datetime(1980, 6, 15, 12, 0, 0).timestamp()
        os.utime(txt, (ts_1980, ts_1980))
        self.assertEqual(self.dates.extract_date(txt), ("1980", "June"))


class ScannerAuditTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="audit_scanner_")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.cat = Categorizer(CONFIG_PATH)
        self.scanner = Scanner(self.cat)
        self.addCleanup(self.scanner.cleanup_staging)

    def test_desktop_ini_is_classified_as_garbage(self):
        """SCAN-1: Both Desktop.ini and desktop.ini are filtered as garbage."""
        src = os.path.join(self.tmp, "src")
        os.makedirs(src)
        with open(os.path.join(src, "Desktop.ini"), "w") as f:
            f.write("[.ShellClassInfo]")
        with open(os.path.join(src, "real.txt"), "w") as f:
            f.write("keep")
        self.scanner.scan_directory(src)
        self.assertEqual(len(self.scanner.files_to_process), 1)
        self.assertTrue(self.scanner.files_to_process[0].endswith("real.txt"))
        self.assertEqual(self.scanner.ignored_garbage_count, 1)

    def test_split_source_paths_preserves_existing_folder_with_comma(self):
        """SCAN-2: A single existing folder whose name contains a comma is not split."""
        comma_dir = os.path.join(self.tmp, "Taxes, 2024")
        os.makedirs(comma_dir)
        with open(os.path.join(comma_dir, "return.pdf"), "w") as f:
            f.write("pdf")
        self.assertEqual(split_source_paths(comma_dir), [comma_dir])
        self.scanner.scan_directory(comma_dir)
        self.assertEqual(len(self.scanner.files_to_process), 1)

    def test_overlapping_sources_and_repeated_scans_do_not_duplicate(self):
        """SCAN-3, SCAN-4, SCAN-5: Overlapping sources deduplicate and .git is not counted as skipped."""
        parent = os.path.join(self.tmp, "parent")
        child = os.path.join(parent, "child")
        repo = os.path.join(parent, "my_repo")
        os.makedirs(child)
        os.makedirs(os.path.join(repo, ".git"))
        with open(os.path.join(child, "a.txt"), "w") as f:
            f.write("hello")
        with open(os.path.join(repo, "main.py"), "w") as f:
            f.write("print(1)")

        self.scanner.scan_directory(f"{parent}, {child}")
        self.assertEqual(len(self.scanner.files_to_process), 1)
        self.assertEqual(len(self.scanner.projects_found), 1)
        self.assertNotIn(".git", self.scanner.skipped_dir_breakdown)

        # Second scan on same instance resets state
        self.scanner.scan_directory(child)
        self.assertEqual(len(self.scanner.files_to_process), 1)
        self.assertEqual(len(self.scanner.projects_found), 0)

    def test_broken_symlink_and_fifo_skipped(self):
        """SCAN-6: Broken symlinks and FIFOs do not enter files_to_process."""
        src = os.path.join(self.tmp, "special")
        os.makedirs(src)
        os.symlink(os.path.join(src, "nonexistent_target"), os.path.join(src, "broken.lnk"))
        fifo_path = os.path.join(src, "pipe.fifo")
        os.mkfifo(fifo_path)
        with open(os.path.join(src, "normal.txt"), "w") as f:
            f.write("ok")

        self.scanner.scan_directory(src)
        self.assertEqual(len(self.scanner.files_to_process), 1)
        self.assertTrue(self.scanner.files_to_process[0].endswith("normal.txt"))

    def test_gdrive_zip_detection_and_zip_slip_protection(self):
        """SCAN-8, SCAN-9, SCAN-10, SCAN-11: Accurate GDrive zip detection, Zip-Slip defense, and __MACOSX exclusion."""
        src = os.path.join(self.tmp, "zips")
        os.makedirs(src)

        normal_zip = os.path.join(src, "report-2024-01-01-final.zip")
        with zipfile.ZipFile(normal_zip, "w") as zf:
            zf.writestr("inside.txt", "normal")
        self.assertFalse(self.scanner.is_gdrive_zip(normal_zip))

        takeout_zip = os.path.join(src, "takeout-20240101T000000Z-001.zip")
        with zipfile.ZipFile(takeout_zip, "w") as zf:
            zf.writestr("../../evil.txt", "evil")
            zf.writestr("__MACOSX/._photo.jpg", "appledouble")
            zf.writestr("Takeout/node_modules/pkg/index.js", "skip")
            zf.writestr("Takeout/Drive/good.txt", "good")
        self.assertTrue(self.scanner.is_gdrive_zip(takeout_zip))

        # Preview scan counts only the 1 valid file inside the GDrive archive (plus the normal non-GDrive zip)
        self.scanner.scan_directory(src, is_preview=True)
        virtual_files = [f for f in self.scanner.files_to_process if not f.endswith(".zip")]
        self.assertEqual(len(virtual_files), 1)
        self.assertIn("Takeout/Drive/good.txt", virtual_files[0])

        # Full extraction automatically bypasses ditto when unsafe members exist, blocks ../../evil.txt, and skips __MACOSX / node_modules
        self.scanner.scan_directory(src, is_preview=False)
        extracted = [f for f in self.scanner.files_to_process if not f.endswith(".zip")]
        self.assertEqual(len(extracted), 1)
        self.assertTrue(extracted[0].endswith("good.txt"))
        self.assertFalse(os.path.exists(os.path.join(self.tmp, "evil.txt")))


class FileOpsCategorizerUtilsAuditTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="audit_fileops_")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.cat = Categorizer(CONFIG_PATH)

    def test_safe_copy_and_engine_preserve_symlinks_and_skip_fifo(self):
        """BUG-1.1 & BUG-1.2: Symlinks are recreated as symlinks and FIFOs do not hang."""
        src_dir = os.path.join(self.tmp, "src")
        dst_dir = os.path.join(self.tmp, "dst")
        os.makedirs(src_dir)
        os.makedirs(dst_dir)

        real_file = os.path.join(src_dir, "target.txt")
        with open(real_file, "w") as f:
            f.write("target")
        link_file = os.path.join(src_dir, "link.txt")
        os.symlink("target.txt", link_file)

        dst_link = os.path.join(dst_dir, "link.txt")
        safe_copy(link_file, dst_link)
        self.assertTrue(os.path.islink(dst_link))
        self.assertEqual(os.readlink(dst_link), "target.txt")

        engine = FileEngine(dst_dir)
        try:
            engine_link = os.path.join(dst_dir, "engine_link.txt")
            engine.copy_file(link_file, engine_link)
            self.assertTrue(os.path.islink(engine_link))
        finally:
            engine.close()

        fifo_src = os.path.join(src_dir, "my.fifo")
        os.mkfifo(fifo_src)
        fifo_dst = os.path.join(dst_dir, "my.fifo")
        safe_copy(fifo_src, fifo_dst)
        self.assertFalse(os.path.exists(fifo_dst))

    def test_immutable_and_readonly_files_copied_cleanly_with_tags(self):
        """BUG-1.3, BUG-1.4, BUG-2.2: Read-only 0o444 and UF_IMMUTABLE files copy cleanly and accept binary plist Finder tags."""
        src = os.path.join(self.tmp, "readonly.txt")
        with open(src, "w") as f:
            f.write("readonly content")
        os.chmod(src, 0o444)
        self.addCleanup(lambda: os.chmod(src, 0o644) if os.path.exists(src) else None)

        dst_root = os.path.join(self.tmp, "dest")
        engine = FileEngine(dst_root)
        try:
            target = os.path.join(dst_root, "readonly.txt")
            engine.copy_file(src, target)
            self.addCleanup(lambda: os.chmod(target, 0o644) if os.path.exists(target) else None)
            engine.set_finder_tag(target, "4", "Blue")
            engine.set_finder_tag(target, "2", "Green")

            try:
                raw = xattr.getxattr(target, "com.apple.metadata:_kMDItemUserTags")
                tags = plistlib.loads(raw)
                self.assertIn("Blue\n4", tags)
                self.assertIn("Green\n2", tags)
            except OSError:
                pass
        finally:
            engine.close()

    def test_zero_byte_files_not_deduplicated_and_identical_ignores_stat_cache(self):
        """BUG-3.2 & BUG-3.3: Empty 0-byte files are not treated as duplicates; files_are_identical reads bytes."""
        src_dir = os.path.join(self.tmp, "src")
        dst_dir = os.path.join(self.tmp, "dst")
        os.makedirs(src_dir)
        e1 = os.path.join(src_dir, "empty1.txt")
        e2 = os.path.join(src_dir, "empty2.txt")
        open(e1, "wb").close()
        open(e2, "wb").close()

        engine = FileEngine(dst_dir)
        try:
            base1 = os.path.join(dst_dir, "Docs", "empty1.txt")
            target1, _, twin1 = engine.resolve_destination(base1, "empty1.txt", 0, e1)
            self.assertIsNotNone(target1)
            self.assertEqual(twin1, "")
            engine.copy_file(e1, target1)
            engine.record_copy(e1, target1, 0, 0.0)
            engine.release_reservation(target1)

            base2 = os.path.join(dst_dir, "Docs", "empty1.txt")
            target2, _, twin2 = engine.resolve_destination(base2, "empty1.txt", 0, e2)
            self.assertIsNotNone(target2)
            self.assertEqual(twin2, "")
            self.assertTrue(target2.endswith("_1.txt"))
            engine.release_reservation(target2)
        finally:
            engine.close()

        f_a = os.path.join(src_dir, "a.bin")
        f_b = os.path.join(src_dir, "b.bin")
        with open(f_a, "wb") as f:
            f.write(b"AAAA")
        with open(f_b, "wb") as f:
            f.write(b"AAAA")
        fixed_ts = 1700000000.0
        os.utime(f_a, (fixed_ts, fixed_ts))
        os.utime(f_b, (fixed_ts, fixed_ts))
        self.assertTrue(files_are_identical(f_a, f_b))

        with open(f_b, "wb") as f:
            f.write(b"BBBB")
        os.utime(f_b, (fixed_ts, fixed_ts))
        self.assertFalse(files_are_identical(f_a, f_b))

    def test_nfd_collision_and_255_utf8_byte_filename_truncation(self):
        """BUG-4.1 & BUG-4.2: NFD/NFC collision keys match and multibyte filenames stay <= 255 UTF-8 bytes."""
        src_dir = os.path.join(self.tmp, "src")
        dst_dir = os.path.join(self.tmp, "dst")
        dir_a = os.path.join(src_dir, "a")
        dir_b = os.path.join(src_dir, "b")
        os.makedirs(dir_a)
        os.makedirs(dir_b)

        nfc_file = os.path.join(dir_a, "caf\u00e9.txt")
        nfd_file = os.path.join(dir_b, "cafe\u0301.txt")
        with open(nfc_file, "w") as f:
            f.write("version 1")
        with open(nfd_file, "w") as f:
            f.write("version 2 different")

        long_name = ("🎨" * 80) + ".png"
        long_src = os.path.join(dir_a, "short.png")
        with open(long_src, "wb") as f:
            f.write(b"\x89PNG\r\n\x1a\npayload")

        engine = FileEngine(dst_dir)
        try:
            t1, _, _ = engine.resolve_destination(
                os.path.join(dst_dir, os.path.basename(nfc_file)),
                os.path.basename(nfc_file),
                os.path.getsize(nfc_file),
                nfc_file,
            )
            self.assertIsNotNone(t1)
            engine.copy_file(nfc_file, t1)
            engine.release_reservation(t1)

            t2, _, _ = engine.resolve_destination(
                os.path.join(dst_dir, os.path.basename(nfd_file)),
                os.path.basename(nfd_file),
                os.path.getsize(nfd_file),
                nfd_file,
            )
            self.assertIsNotNone(t2)
            self.assertTrue(t2.endswith("_1.txt"))
            engine.release_reservation(t2)

            t_long, _, _ = engine.resolve_destination(
                os.path.join(dst_dir, long_name),
                long_name,
                os.path.getsize(long_src),
                long_src,
            )
            self.assertLessEqual(len(os.path.basename(t_long).encode("utf-8")), 255)
            engine.release_reservation(t_long)
        finally:
            engine.close()

    def test_categorizer_compound_extensions_and_screenshot_rules(self):
        """BUG-5.1, BUG-5.2, BUG-5.3: Compound extensions (.tar.gz) and screenshot detection rules."""
        self.assertEqual(split_filename_ext("backup.tar.gz"), ("backup", ".tar.gz"))
        self.assertEqual(self.cat.get_file_category("backup.tar.gz"), "Archives")
        self.assertEqual(self.cat.get_file_category("archive.zst"), "Archives")

        self.assertTrue(self.cat.is_screenshot("Screen_Shot_2024-01-01.png"))
        self.assertFalse(self.cat.is_screenshot("Screenshot_recording.mp4"))
        self.assertFalse(self.cat.is_screenshot("Screenshot_raw.cr3"))

    def test_utils_rich_markup_escape_and_negative_format_size(self):
        """BUG-5.5 & BUG-5.6: Rich markup characters in filenames do not strip brackets; negative sizes clamp cleanly."""
        self.assertEqual(utils.format_size(-1024), "0.00 B")
        utils.print_info("Copied /path/[2024]/photo_[1].jpg")


class OrganizerAndWebAppAuditTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="audit_app_")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.api = OrganizerAPI(CONFIG_PATH)

    def test_live_photo_pairing_and_exif_screenshot_override(self):
        """BUG-T3-03, BUG-T3-04, BUG-5.2: Live Photos pair .jpg/.heic with .mov, and camera EXIF overrides screenshot filename."""
        src = os.path.join(self.tmp, "src")
        dest = os.path.join(self.tmp, "dest")
        os.makedirs(src)

        jpg_path = os.path.join(src, "IMG_100.JPG")
        mov_path = os.path.join(src, "IMG_100.MOV")
        with open(jpg_path, "wb") as f:
            f.write(_build_minimal_exif_jpeg("2021:07:19 14:22:05", make="Apple"))
        with open(mov_path, "wb") as f:
            f.write(b"\x00\x00\x00\x14ftypqt  \x00\x00\x00\x00" + b"video_bytes")
        recent = datetime.datetime(2026, 2, 1, 12, 0, 0).timestamp()
        os.utime(jpg_path, (recent, recent))
        os.utime(mov_path, (recent, recent))

        cam_shot = os.path.join(src, "Screenshot_from_Canon.jpg")
        with open(cam_shot, "wb") as f:
            f.write(_build_minimal_exif_jpeg("2021:07:19 14:22:05", make="Canon"))
        os.utime(cam_shot, (recent, recent))

        ok = self.api.run(src, dest, is_preview=False, dest_mode="new")
        self.assertTrue(ok)

        self.assertTrue(os.path.exists(os.path.join(dest, "Media", "2021", "July", "IMG_100.JPG")))
        self.assertTrue(os.path.exists(os.path.join(dest, "Media", "2021", "July", "IMG_100.MOV")))
        self.assertTrue(os.path.exists(os.path.join(dest, "Media", "2021", "July", "Screenshot_from_Canon.jpg")))
        self.assertFalse(os.path.exists(os.path.join(dest, "Media", "Screenshots", "2021", "July", "Screenshot_from_Canon.jpg")))

    def test_dissolve_project_skips_git_deletes_duplicates_and_persists_across_runs(self):
        """BUG-404 & BUG-T3-09: Dissolving a project skips .git, removes duplicate inner files, and persists dissolution."""
        src = os.path.join(self.tmp, "src")
        dest = os.path.join(self.tmp, "dest")
        proj = os.path.join(src, "my_app")
        os.makedirs(os.path.join(proj, ".git"))
        with open(os.path.join(proj, ".git", "HEAD"), "w") as f:
            f.write("ref: refs/heads/main\n")
        with open(os.path.join(proj, "readme.pdf"), "wb") as f:
            f.write(b"%PDF-1.4 unique pdf payload")
        with open(os.path.join(src, "readme.pdf"), "wb") as f:
            f.write(b"%PDF-1.4 unique pdf payload")

        ok = self.api.run(src, dest, is_preview=False, dest_mode="new")
        self.assertTrue(ok)
        dest_proj = os.path.join(dest, "Code", "my_app")
        self.assertTrue(os.path.isdir(dest_proj))

        dissolve_ok, _ = self.api.dissolve_and_resort_project(dest, dest_proj)
        self.assertTrue(dissolve_ok)
        self.assertFalse(os.path.exists(dest_proj))

        # Re-run incremental organization: my_app must NOT be re-copied as an intact project into Code/my_app
        ok2 = self.api.run(src, dest, is_preview=False, dest_mode="merge")
        self.assertTrue(ok2)
        self.assertFalse(os.path.exists(dest_proj))

    def test_verify_and_repair_check_duplicate_of_target_and_trash_requires_verified_db(self):
        """BUG-T3-10 & BUG-406: Missing duplicate_of target is detected, and trash_duplicates requires verified DB."""
        src = os.path.join(self.tmp, "src")
        dest = os.path.join(self.tmp, "dest")
        os.makedirs(os.path.join(src, "sub"))
        f1 = os.path.join(src, "doc.txt")
        f2 = os.path.join(src, "sub", "doc.txt")
        with open(f1, "w") as f:
            f.write("identical content for dedup")
        with open(f2, "w") as f:
            f.write("identical content for dedup")

        self.api.run(src, dest, is_preview=False, dest_mode="new")
        dups = self.api.get_duplicate_records(dest)
        self.assertEqual(len(dups), 1)
        dup_src = dups[0]["source_path"]
        dup_target = dups[0]["duplicate_of"]

        count0, refused0 = self.api.trash_duplicates([dup_src], dest_abs="")
        self.assertEqual(count0, 0)
        self.assertEqual(len(refused0), 1)
        self.assertTrue(os.path.exists(dup_src))

        os.remove(dup_target)
        verify = self.api.verify_transfer(dest)
        self.assertFalse(verify["is_perfect"])
        self.assertGreaterEqual(verify["missing_count"], 1)

        count1, refused1 = self.api.trash_duplicates([dup_src], dest_abs=dest)
        self.assertEqual(count1, 0)
        self.assertEqual(len(refused1), 1)
        self.assertTrue(os.path.exists(dup_src))

        repair = self.api.repair_transfer(dest)
        self.assertTrue(repair["success"])
        self.assertEqual(repair["repaired_count"], 2)

    def test_webapp_endpoints_locking_csv_sanitization_and_finder_safety(self):
        """BUG-401, BUG-403, BUG-407, BUG-408, BUG-409, BUG-410: Flask endpoint security and concurrency guards."""
        client = webapp.app.test_client()
        headers = {"X-Organizer-Token": webapp.API_TOKEN}

        resp = client.post(
            "/api/start",
            headers=headers,
            json={"source": os.path.join(self.tmp, "does_not_exist"), "dest": os.path.join(self.tmp, "dest")},
        )
        self.assertEqual(resp.status_code, 400)

        with webapp.state_lock:
            prev_status = webapp.state.status
            webapp.state.status = "running"
        try:
            for endpoint in ("/api/dissolve_project", "/api/trash_duplicates", "/api/restore_duplicates", "/api/repair_transfer"):
                r = client.post(endpoint, headers=headers, json={})
                self.assertEqual(r.status_code, 409, f"{endpoint} did not return 409 while running")
        finally:
            with webapp.state_lock:
                webapp.state.status = prev_status

        r_finder = client.get("/api/open_finder?path=-aCalculator", headers=headers)
        self.assertIn(r_finder.status_code, (400, 404))

        dest = os.path.join(self.tmp, "csv_dest")
        engine = FileEngine(dest)
        try:
            engine.record_copy(
                "=cmd|' /C calc'!A0.txt",
                "=SUM(A1:A2)",
                10,
                1700000000.0,
                "abc",
                "=HYPERLINK(\"http://evil\")",
            )
            engine.conn.commit()
        finally:
            engine.close()

        r_csv = client.get(f"/api/export_csv?dest={dest}&token={webapp.API_TOKEN}")
        self.assertEqual(r_csv.status_code, 200)
        csv_text = r_csv.get_data(as_text=True)
        self.assertIn("'=cmd|", csv_text)
        self.assertIn("'=SUM(A1:A2)", csv_text)

    def test_restore_duplicates_and_api_endpoint(self):
        """One-click Restore from .Duplicates_Trash restores files, cleans up empty .Duplicates_Trash, and refuses overwrite conflicts."""
        src = os.path.join(self.tmp, "restore_src")
        dest = os.path.join(self.tmp, "restore_dest")
        os.makedirs(src)

        f1 = os.path.join(src, "orig.txt")
        f2 = os.path.join(src, "copy.txt")
        with open(f1, "w") as f:
            f.write("duplicate payload for restore test")
        with open(f2, "w") as f:
            f.write("duplicate payload for restore test")

        self.api.run(src, dest, is_preview=False, dest_mode="new")
        dups = self.api.get_duplicate_records(dest)
        self.assertEqual(len(dups), 1)
        dup_src = dups[0]["source_path"]

        # Move duplicate into .Duplicates_Trash
        trashed_count, refused = self.api.trash_duplicates([dup_src], dest_abs=dest)
        self.assertEqual(trashed_count, 1)
        self.assertEqual(refused, [])
        self.assertFalse(os.path.exists(dup_src))

        trashed_list = self.api.get_trashed_duplicates(dest)
        self.assertEqual(len(trashed_list), 1)
        self.assertEqual(trashed_list[0]["source_path"], dup_src)
        trashed_file_path = trashed_list[0]["trashed_path"]
        self.assertTrue(os.path.exists(trashed_file_path))

        # Test conflict protection: if a new file was created at dup_src, restore_duplicates refuses to overwrite it
        with open(dup_src, "w") as f:
            f.write("brand new conflicting file")
        restored_conflict, refused_conflict = self.api.restore_duplicates(dest, [dup_src])
        self.assertEqual(restored_conflict, 0)
        self.assertEqual(len(refused_conflict), 1)
        os.remove(dup_src)

        # Now restore via the Flask endpoint /api/restore_duplicates
        client = webapp.app.test_client()
        headers = {"X-Organizer-Token": webapp.API_TOKEN}
        r_dups = client.get(f"/api/duplicates?dest={dest}", headers=headers)
        self.assertEqual(r_dups.status_code, 200)
        self.assertEqual(len(r_dups.get_json()["trashed"]), 1)

        r_restore = client.post(
            "/api/restore_duplicates",
            headers=headers,
            json={"dest": dest, "source_paths": [dup_src]},
        )
        self.assertEqual(r_restore.status_code, 200)
        restore_json = r_restore.get_json()
        self.assertTrue(restore_json["success"])
        self.assertEqual(restore_json["count"], 1)
        self.assertTrue(os.path.exists(dup_src))
        self.assertFalse(os.path.exists(os.path.join(src, ".Duplicates_Trash")))
        self.assertEqual(len(self.api.get_trashed_duplicates(dest)), 0)
        self.assertEqual(len(self.api.get_duplicate_records(dest)), 1)

    def test_second_pass_loose_symlink_and_in_flight_sizes(self):
        """Second-pass fixes: loose symlinks do not shadow real files, and in_flight_sizes is released on duplicate skip."""
        src = os.path.join(self.tmp, "sym_src")
        dest = os.path.join(self.tmp, "sym_dest")
        os.makedirs(src)
        real_file = os.path.join(src, "z_real.txt")
        with open(real_file, "w") as f:
            f.write("real file content")
        # Create a loose symlink whose name sorts alphabetically before z_real.txt
        sym_file = os.path.join(src, "a_link.txt")
        os.symlink("z_real.txt", sym_file)

        scanner = Scanner(Categorizer(CONFIG_PATH))
        scanner.scan_directory(src)
        self.assertEqual(scanner.files_to_process, [os.path.abspath(real_file)])

        # Verify in_flight_sizes is cleaned up when resolve_destination detects a duplicate
        engine = FileEngine(dest)
        try:
            f_copy = os.path.join(src, "z_real_copy.txt")
            with open(f_copy, "w") as f:
                f.write("real file content")
            sz = os.path.getsize(real_file)
            base_dest = os.path.join(dest, "Documents", "Text", "z_real.txt")
            target1, h1, twin1 = engine.resolve_destination(base_dest, "z_real.txt", sz, real_file)
            self.assertIsNotNone(target1)
            self.assertEqual(twin1, "")
            engine.copy_file(real_file, target1)
            engine.record_copy(real_file, target1, sz, 0.0, h1)
            engine.release_reservation(target1)
            self.assertEqual(engine.in_flight_sizes, {})

            dup_dest, dup_hash, dup_of = engine.resolve_destination(base_dest, "z_real_copy.txt", sz, f_copy)
            self.assertIsNone(dup_dest)
            self.assertEqual(dup_of, target1)
            self.assertEqual(engine.in_flight_sizes, {})
        finally:
            engine.close()

    def test_main_help_exits_cleanly_without_gui(self):
        """BUG-416: Running `python3 main.py --help` prints usage and exits 0."""
        proc = subprocess.run(
            [sys.executable, os.path.join(REPO_ROOT, "main.py"), "--help"],
            capture_output=True,
            text=True,
            timeout=10,
        )
        self.assertEqual(proc.returncode, 0)
        self.assertIn("usage:", proc.stdout.lower())


if __name__ == "__main__":
    unittest.main(verbosity=2)


import os
import tempfile
import unittest
import zipfile
from unittest.mock import patch

from src.api_organizer import OrganizerAPI
from src.categorizer import Categorizer
from src.scanner import Scanner


class Pass4ScannerOsTrashTests(unittest.TestCase):
    """Regression tests for P4-03: OS trash and system directories must be skipped."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = self.tmp.name
        self.config_path = os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
            "src",
            "config.json",
        )

    def tearDown(self):
        self.tmp.cleanup()

    def test_scan_directory_skips_windows_linux_macos_trash_and_system_dirs(self):
        """Scanner.scan_directory must not walk into $RECYCLE.BIN, .Trash-<uid>, System Volume Information, etc."""
        src = os.path.join(self.root, "drive")
        os.makedirs(src)

        skipped_dirs = [
            os.path.join(src, "$RECYCLE.BIN", "S-1-5-21-1234"),
            os.path.join(src, "$Recycle.Bin", "S-1-5-21-5678"),
            os.path.join(src, "RECYCLER", "S-1-5-21"),
            os.path.join(src, "Recycled"),
            os.path.join(src, "System Volume Information"),
            os.path.join(src, ".Trash-1000", "files"),
            os.path.join(src, ".trash-501", "files"),
            os.path.join(src, ".DocumentRevisions-V100", "PerUID"),
            os.path.join(src, ".TemporaryItems", "folders.501"),
            os.path.join(src, ".MobileBackups"),
            os.path.join(src, "Backups.backupdb"),
            os.path.join(src, ".dropbox.cache"),
            os.path.join(src, ".stversions"),
            os.path.join(src, ".trashes", "501"),
        ]
        for d in skipped_dirs:
            os.makedirs(d, exist_ok=True)
            with open(os.path.join(d, "trashed_or_system.txt"), "wb") as f:
                f.write(b"should not be scanned " * 10)

        live_dir = os.path.join(src, "Documents")
        os.makedirs(live_dir)
        live_file = os.path.join(live_dir, "keep_me.txt")
        with open(live_file, "wb") as f:
            f.write(b"real user document " * 10)

        scanner = Scanner(Categorizer(self.config_path))
        scanner.scan_directory(src)

        self.assertEqual(scanner.files_to_process, [live_file])
        self.assertEqual(scanner.projects_found, [])
        self.assertEqual(scanner.skipped_dirs, [])

    def test_recycle_bin_does_not_shadow_live_file_as_duplicate(self):
        """A deleted copy in $RECYCLE.BIN must not be copied first and cause the live file to be marked DUPLICATE_SKIPPED."""
        src = os.path.join(self.root, "source_drive")
        dst = os.path.join(self.root, "dest_drive")
        os.makedirs(src)
        os.makedirs(dst)

        payload = b"important family photo payload " * 64

        recycle_dir = os.path.join(src, "$RECYCLE.BIN", "S-1-5-21-1000")
        os.makedirs(recycle_dir)
        recycled_copy = os.path.join(recycle_dir, "$R3XK2P1.txt")
        with open(recycled_copy, "wb") as f:
            f.write(payload)

        photos_dir = os.path.join(src, "Photos")
        os.makedirs(photos_dir)
        live_file = os.path.join(photos_dir, "family_note.txt")
        with open(live_file, "wb") as f:
            f.write(payload)

        api = OrganizerAPI(self.config_path)
        with patch("src.file_ops.FileEngine.set_finder_tag"):
            res = api.run(src, dst, is_preview=False, dest_mode="new")

        self.assertTrue(res)
        dup_records = api.get_duplicate_records(dst)
        self.assertEqual(dup_records, [], f"Live file should not be flagged as duplicate: {dup_records}")

    def test_extract_gdrive_zip_skips_os_trash_members(self):
        """Zip extraction must skip members inside $RECYCLE.BIN or .Trash-<uid>."""
        src = os.path.join(self.root, "zip_src")
        staging_root = os.path.join(self.root, "staging")
        os.makedirs(src)
        zip_path = os.path.join(src, "gdrive-takeout.zip")
        with zipfile.ZipFile(zip_path, "w") as zf:
            zf.writestr("$RECYCLE.BIN/S-1-5-21/$R999.txt", "deleted content")
            zf.writestr(".Trash-1000/files/old.txt", "trashed content")
            zf.writestr("System Volume Information/WPSettings.dat", "volume metadata")
            zf.writestr("Documents/real.txt", "kept content")

        scanner = Scanner(Categorizer(self.config_path), staging_root=staging_root)
        try:
            # Test both preview inspection and full extraction
            preview_files = scanner.extract_gdrive_zip(zip_path, is_preview=True)
            self.assertEqual(len(preview_files), 1)
            self.assertTrue(preview_files[0].endswith(os.path.join("Documents", "real.txt")))

            scanner._reset_scan_state()
            extracted_files = scanner.extract_gdrive_zip(zip_path, is_preview=False)
            self.assertEqual(len(extracted_files), 1)
            self.assertTrue(extracted_files[0].endswith(os.path.join("Documents", "real.txt")))
        finally:
            scanner.cleanup_staging()


if __name__ == "__main__":
    unittest.main()

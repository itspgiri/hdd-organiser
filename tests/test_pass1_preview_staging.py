"""Pass-1 audit, finding P1-05: preview deleted an archive staging folder.

A real transfer unpacks Google Drive / Takeout archives into
<destination>/.organizer_staging/<archive>_<key>/ and keeps that folder if the
run is cancelled, so the next run can resume without unzipping again. When
preview (dry run) met such an archive and could not open it (a truncated
download, say), extract_gdrive_zip()'s error handler removed the staging
folder with rmtree, even in preview. Preview must write nothing; if the
destination is on the source drive, that is a write to the source drive.

Run with:  .venv/bin/python -m unittest tests.test_pass1_preview_staging -v
"""

import hashlib
import os
import sys
import zipfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from pass1_helpers import Pass1Case, snapshot_tree, unique_payload, write_file  # noqa: E402

ARCHIVE = "takeout-20230101-001.zip"


class PreviewStagingTests(Pass1Case):

    def _leftover_staging(self, zip_path):
        """What a cancelled real run leaves behind for zip_path. The scanner
        keys the folder by the archive's absolute path; create it for both the
        path as given and its realpath (/var vs /private/var on macOS)."""
        stem = os.path.splitext(os.path.basename(zip_path))[0]
        for p in {os.path.abspath(zip_path), os.path.realpath(zip_path)}:
            key = hashlib.sha256(p.encode("utf-8")).hexdigest()[:16]
            staging = os.path.join(self.dest, ".organizer_staging", f"{stem}_{key}")
            write_file(os.path.join(staging, "Takeout", "photo.jpg"), unique_payload("photo", 5000))
            write_file(os.path.join(staging, ".unzip_completed"), b"COMPLETED")

    def _preview_must_not_touch_destination(self):
        before = snapshot_tree(self.dest)
        ok = self.make_api().run(self.src, self.dest, is_preview=True, dest_mode="merge")
        self.assertTrue(ok, "\n".join(self.logs[-20:]))
        self.assertEqual(snapshot_tree(self.dest), before, "preview changed the destination")

    def test_preview_keeps_staging_folder_of_unreadable_archive(self):
        zip_path = write_file(os.path.join(self.src, ARCHIVE), b"PK\x03\x04 cut-off download")
        self._leftover_staging(zip_path)
        self._preview_must_not_touch_destination()

    def test_preview_keeps_staging_folder_of_readable_archive(self):
        zip_path = os.path.join(self.src, ARCHIVE)
        with zipfile.ZipFile(zip_path, "w") as zf:
            zf.writestr("Takeout/photo.jpg", unique_payload("photo", 5000))
        self._leftover_staging(zip_path)
        self._preview_must_not_touch_destination()

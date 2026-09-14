"""Regression tests for file metadata preservation during transfers.

Background
----------
The exFAT hardening work replaced ``shutil.copy2`` with ``shutil.copyfile`` to
avoid Errno 22 (Invalid argument) when writing extended attributes and ACLs to
non-APFS volumes. ``shutil.copyfile`` copies bytes only, so every transferred
file silently had its modification time reset to the moment of the copy. That
destroyed the historical dates the organizer sorts by: a genuine 2019 photo
landed in ``Media/2026/September``.

These tests lock in the fix (``restore_timestamps`` / ``safe_copy``) and will
fail again if any copy path reverts to a metadata-less copy.

Run with:  .venv/bin/python -m unittest discover -s tests -v
"""

import datetime
import os
import shutil
import sys
import tempfile
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src import file_ops
from src.file_ops import FileEngine, safe_copy, restore_timestamps
from src.categorizer import Categorizer
from src.dates import DateExtractor

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CONFIG_PATH = os.path.join(REPO_ROOT, "src", "config.json")

# A real historical date, well before any plausible "now".
HISTORICAL = time.mktime(datetime.datetime(2019, 3, 14, 10, 30, 0).timetuple())
TOLERANCE_SECONDS = 2


class MetadataPreservationTestCase(unittest.TestCase):
    """Shared fixture: a source file stamped with a 2019 modification time."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="organizer_mdtest_")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        # Always restore the module flag; individual tests may disable it.
        self._native_flag = file_ops._HAS_NATIVE_COPYFILE
        self.addCleanup(self._restore_native_flag)

    def _restore_native_flag(self):
        file_ops._HAS_NATIVE_COPYFILE = self._native_flag

    def make_source(self, name="photo.jpg", subdir="source"):
        d = os.path.join(self.tmp, subdir)
        os.makedirs(d, exist_ok=True)
        path = os.path.join(d, name)
        with open(path, "wb") as f:
            # Minimal JPEG magic bytes plus filler, with no EXIF block, so the
            # DateExtractor is forced down to the filesystem-timestamp layer.
            f.write(b"\xff\xd8\xff\xe0" + b"PAYLOAD" * 64)
        os.utime(path, (HISTORICAL, HISTORICAL))
        return path

    def assertMtimePreserved(self, path, msg=""):
        actual = os.path.getmtime(path)
        drift = abs(actual - HISTORICAL)
        self.assertLess(
            drift,
            TOLERANCE_SECONDS,
            f"{msg} Expected mtime {datetime.datetime.fromtimestamp(HISTORICAL):%Y-%m-%d}, "
            f"got {datetime.datetime.fromtimestamp(actual):%Y-%m-%d} "
            f"(drift {drift:.0f}s). Metadata was overwritten by the copy.",
        )


class TestSafeCopy(MetadataPreservationTestCase):

    def test_preserves_modification_time(self):
        src = self.make_source()
        dst = os.path.join(self.tmp, "copy.jpg")
        safe_copy(src, dst)
        self.assertMtimePreserved(dst, "safe_copy did not preserve the timestamp.")

    def test_copies_file_contents_intact(self):
        src = self.make_source()
        dst = os.path.join(self.tmp, "copy.jpg")
        safe_copy(src, dst)
        with open(src, "rb") as a, open(dst, "rb") as b:
            self.assertEqual(a.read(), b.read(), "safe_copy corrupted file contents.")

    def test_returns_target_path_for_copytree_compatibility(self):
        # shutil.copytree expects copy_function to behave like shutil.copy2.
        src = self.make_source()
        dst = os.path.join(self.tmp, "copy.jpg")
        self.assertEqual(safe_copy(src, dst), dst)


class TestRestoreTimestamps(MetadataPreservationTestCase):

    def test_is_best_effort_and_never_raises(self):
        # exFAT/FAT32 and read-only mounts can reject utime; a failure here must
        # never abort an in-flight transfer.
        src = self.make_source()
        missing = os.path.join(self.tmp, "does_not_exist.jpg")
        try:
            restore_timestamps(src, missing)
            restore_timestamps(missing, src)
        except Exception as exc:  # pragma: no cover - guard against regressions
            self.fail(f"restore_timestamps must swallow errors, but raised: {exc!r}")


class TestFileEngineCopyFile(MetadataPreservationTestCase):

    def _copy_via_engine(self, src, dest_dirname):
        dest_root = os.path.join(self.tmp, dest_dirname)
        engine = FileEngine(dest_root)
        try:
            target = os.path.join(dest_root, os.path.basename(src))
            engine.copy_file(src, target)
        finally:
            engine.close()
        return target

    def test_native_syscall_path_preserves_mtime(self):
        src = self.make_source(subdir="src_native")
        target = self._copy_via_engine(src, "dest_native")
        self.assertMtimePreserved(target, "Native copyfile path lost the timestamp.")

    def test_fallback_path_preserves_mtime(self):
        """The actual regression: exFAT/FAT32 drives take this branch."""
        file_ops._HAS_NATIVE_COPYFILE = False
        src = self.make_source(subdir="src_fallback")
        target = self._copy_via_engine(src, "dest_fallback")
        self.assertMtimePreserved(
            target, "shutil.copyfile fallback lost the timestamp."
        )

    def test_fallback_path_preserves_file_size(self):
        file_ops._HAS_NATIVE_COPYFILE = False
        src = self.make_source(subdir="src_size")
        target = self._copy_via_engine(src, "dest_size")
        self.assertEqual(os.path.getsize(src), os.path.getsize(target))


class TestCodeProjectCopytree(MetadataPreservationTestCase):

    def test_copytree_with_safe_copy_preserves_mtime(self):
        proj = os.path.join(self.tmp, "my_project")
        os.makedirs(os.path.join(proj, "nested"))
        for rel in ("main.py", os.path.join("nested", "utils.py")):
            path = os.path.join(proj, rel)
            with open(path, "w") as f:
                f.write("print('hello')\n")
            os.utime(path, (HISTORICAL, HISTORICAL))

        dest = os.path.join(self.tmp, "my_project_copy")
        shutil.copytree(
            proj, dest, dirs_exist_ok=True, copy_function=safe_copy,
            ignore_dangling_symlinks=True,
        )

        for rel in ("main.py", os.path.join("nested", "utils.py")):
            self.assertMtimePreserved(
                os.path.join(dest, rel), f"Code project file {rel} lost its timestamp."
            )


class TestDateSortingEndToEnd(MetadataPreservationTestCase):
    """The user-visible symptom: files sorted into the wrong Year/Month folder."""

    def setUp(self):
        super().setUp()
        if not os.path.exists(CONFIG_PATH):
            self.skipTest(f"config.json not found at {CONFIG_PATH}")
        self.dates = DateExtractor(Categorizer(CONFIG_PATH))

    def test_copy_is_sorted_into_original_year_not_today(self):
        file_ops._HAS_NATIVE_COPYFILE = False
        src = self.make_source(name="no_exif_scan.jpg", subdir="src_sort")
        dest_root = os.path.join(self.tmp, "dest_sort")
        engine = FileEngine(dest_root)
        try:
            target = os.path.join(dest_root, "no_exif_scan.jpg")
            engine.copy_file(src, target)
        finally:
            engine.close()

        self.assertEqual(
            self.dates.extract_date(src),
            self.dates.extract_date(target),
            "The copy sorts into a different Year/Month folder than its source.",
        )
        self.assertEqual(
            self.dates.extract_date(target),
            ("2019", "March"),
            "Copied file was not sorted into its true historical folder.",
        )


if __name__ == "__main__":
    unittest.main(verbosity=2)

"""Regression tests for the issues found in the full-codebase audit.

Each test below corresponds to a specific defect that was verified to exist
before the fix landed. They are grouped by the symptom a user would actually
notice, and each class docstring records what was measured.

Run with:  .venv/bin/python -m unittest discover -s tests -v
"""

import datetime
import os
import shutil
import sqlite3
import stat
import sys
import tempfile
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import xattr

from src import file_ops
from src.file_ops import (
    FileEngine, safe_copy, native_copy, restore_mode, restore_xattrs,
)
from src.categorizer import Categorizer
from src.dates import DateExtractor
from src.api_organizer import OrganizerAPI, compute_relative_destination
from src.scanner import Scanner, SKIP_BUILD_DIRS

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CONFIG_PATH = os.path.join(REPO_ROOT, "src", "config.json")


class CopyfileConstantTests(unittest.TestCase):
    """_COPYFILE_ALL was 32767, which sets 11 bits Apple has never defined.

    Verified against /usr/include/copyfile.h: COPYFILE_ALL is
    COPYFILE_METADATA|COPYFILE_DATA == (ACL|STAT|XATTR|DATA) == 15.
    """

    @unittest.skipUnless(file_ops._HAS_NATIVE_COPYFILE, "macOS only")
    def test_copyfile_all_is_the_documented_value(self):
        self.assertEqual(file_ops._COPYFILE_ALL, 15)

    @unittest.skipUnless(file_ops._HAS_NATIVE_COPYFILE, "macOS only")
    def test_no_undefined_flag_bits_are_set(self):
        defined = (1 << 0) | (1 << 1) | (1 << 2) | (1 << 3)
        self.assertEqual(file_ops._COPYFILE_ALL & ~defined, 0,
                         "undefined copyfile flag bits are being passed to the syscall")

    @unittest.skipUnless(file_ops._HAS_NATIVE_COPYFILE, "macOS only")
    def test_clone_flag_is_bit_24(self):
        self.assertEqual(file_ops._COPYFILE_CLONE, 1 << 24)

    @unittest.skipUnless(file_ops._HAS_NATIVE_COPYFILE, "macOS only")
    def test_move_and_unlink_bits_are_never_set(self):
        """COPYFILE_MOVE unlinks the source. It must never be set."""
        combined = file_ops._COPYFILE_ALL | file_ops._COPYFILE_CLONE
        self.assertEqual(combined & (1 << 20), 0, "COPYFILE_MOVE would delete the source")
        self.assertEqual(combined & (1 << 21), 0, "COPYFILE_UNLINK would delete the destination")


class CodeProjectMetadataTests(unittest.TestCase):
    """safe_copy was a bare shutil.copyfile, so every file in every copied
    Code/ project lost its executable bit (measured 0o755 -> 0o644) and all of
    its extended attributes -- despite being advertised as "100% INTACT".
    """

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="audit_proj_")
        self.src = os.path.join(self.tmp, "build.sh")
        with open(self.src, "wb") as f:
            f.write(b"#!/bin/sh\necho hello\n")
        os.chmod(self.src, 0o755)
        xattr.setxattr(self.src, "user.projectTag", b"keep-me")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_safe_copy_preserves_executable_bit(self):
        dst = os.path.join(self.tmp, "out.sh")
        safe_copy(self.src, dst)
        self.assertEqual(stat.S_IMODE(os.stat(dst).st_mode), 0o755,
                         "a copied shell script arrived non-executable")

    @unittest.skipUnless(file_ops._HAS_NATIVE_COPYFILE, "macOS only")
    def test_safe_copy_preserves_extended_attributes(self):
        dst = os.path.join(self.tmp, "out2.sh")
        safe_copy(self.src, dst)
        self.assertIn("user.projectTag", xattr.listxattr(dst))

    def test_safe_copy_still_returns_target_for_copytree(self):
        dst = os.path.join(self.tmp, "out3.sh")
        self.assertEqual(safe_copy(self.src, dst), dst)

    def test_copytree_preserves_executable_bit(self):
        """The end-to-end path: copy_project_intact -> copytree -> safe_copy."""
        proj = os.path.join(self.tmp, "proj")
        os.makedirs(proj)
        script = os.path.join(proj, "configure")
        with open(script, "wb") as f:
            f.write(b"#!/bin/sh\n")
        os.chmod(script, 0o755)

        dest = os.path.join(self.tmp, "dest_proj")
        ok, err = file_ops.copy_project_intact(proj, dest)
        self.assertTrue(ok, err)
        self.assertEqual(stat.S_IMODE(os.stat(os.path.join(dest, "configure")).st_mode), 0o755)

    def test_fallback_path_still_restores_mode(self):
        """exFAT/FAT32 take the non-native branch; mode must survive there too."""
        original = file_ops._HAS_NATIVE_COPYFILE
        file_ops._HAS_NATIVE_COPYFILE = False
        try:
            dst = os.path.join(self.tmp, "out4.sh")
            safe_copy(self.src, dst)
            self.assertEqual(stat.S_IMODE(os.stat(dst).st_mode), 0o755)
        finally:
            file_ops._HAS_NATIVE_COPYFILE = original


class ExifCoverageTests(unittest.TestCase):
    """HEIC (the default iPhone format), PNG and WebP were excluded from the
    EXIF reader even though the installed exifread supports them, so those
    photos were dated by filesystem mtime instead of capture time.
    """

    def test_heic_reaches_the_exif_reader(self):
        self.assertIn(".heic", DateExtractor.EXIF_EXTENSIONS)

    def test_other_supported_formats_reach_the_exif_reader(self):
        for ext in (".heif", ".png", ".webp", ".avif"):
            self.assertIn(ext, DateExtractor.EXIF_EXTENSIONS, ext)

    def test_original_formats_are_not_regressed(self):
        for ext in (".jpg", ".jpeg", ".tif", ".tiff", ".cr2", ".nef", ".arw", ".dng"):
            self.assertIn(ext, DateExtractor.EXIF_EXTENSIONS, ext)

    def test_extension_sets_are_defined_only_once(self):
        """They used to be declared twice, the second silently shadowing the
        first -- so editing the visible one appeared to do nothing."""
        with open(os.path.join(REPO_ROOT, "src", "dates.py"), encoding="utf-8") as f:
            body = f.read()
        self.assertEqual(body.count("EXIF_EXTENSIONS = {"), 1)
        self.assertEqual(body.count("VIDEO_EXTENSIONS = {"), 1)

    def test_heic_is_categorised_as_media(self):
        cat = Categorizer(CONFIG_PATH)
        self.assertEqual(cat.get_file_category("IMG_0001.heic"), "Media")

    def test_avif_is_no_longer_unsorted(self):
        cat = Categorizer(CONFIG_PATH)
        self.assertEqual(cat.get_file_category("photo.avif"), "Media")


class LivePhotoPairingTests(unittest.TestCase):
    """The pairing table was only consulted when extract_date returned nothing,
    but extract_date always succeeds via the mtime fallback. The lookup was
    therefore unreachable and the two halves of a Live Photo were filed apart.
    """

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="audit_lp_")
        self.cat = Categorizer(CONFIG_PATH)
        self.dates = DateExtractor(self.cat)
        self.heic = os.path.join(self.tmp, "IMG_1234.heic")
        self.mov = os.path.join(self.tmp, "IMG_1234.mov")
        for p in (self.heic, self.mov):
            with open(p, "wb") as f:
                f.write(b"\x00" * 64)
        # The still kept its real date; the clip's mtime got reset by some
        # earlier copy, which is exactly the messy-drive situation.
        old = time.mktime(datetime.datetime(2021, 8, 14, 9, 0, 0).timetuple())
        os.utime(self.heic, (old, old))
        os.utime(self.mov, (time.time(), time.time()))

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_video_inherits_the_still_capture_date(self):
        pairing = {(self.tmp, "IMG_1234"): ("2021", "August")}
        rel = compute_relative_destination(self.cat, self.dates, self.mov, pairing)
        self.assertEqual(rel, os.path.join("Media", "2021", "August", "IMG_1234.mov"))

    def test_pair_lands_in_the_same_folder(self):
        pairing = {(self.tmp, "IMG_1234"): ("2021", "August")}
        rel_photo = compute_relative_destination(self.cat, self.dates, self.heic, pairing)
        rel_video = compute_relative_destination(self.cat, self.dates, self.mov, pairing)
        self.assertEqual(os.path.dirname(rel_photo), os.path.dirname(rel_video))

    def test_unpaired_video_still_uses_its_own_date(self):
        """Negative control: a video with no matching still must not be
        dragged into some unrelated folder."""
        lone = os.path.join(self.tmp, "clip.mov")
        with open(lone, "wb") as f:
            f.write(b"\x00" * 64)
        old = time.mktime(datetime.datetime(2018, 2, 3, 9, 0, 0).timetuple())
        os.utime(lone, (old, old))
        pairing = {(self.tmp, "IMG_1234"): ("2021", "August")}
        rel = compute_relative_destination(self.cat, self.dates, lone, pairing)
        self.assertEqual(rel, os.path.join("Media", "2018", "February", "clip.mov"))

    def test_non_media_is_unaffected(self):
        doc = os.path.join(self.tmp, "notes.pdf")
        with open(doc, "wb") as f:
            f.write(b"%PDF-1.4\n")
        rel = compute_relative_destination(self.cat, self.dates, doc)
        self.assertEqual(rel, os.path.join("Documents/PDF", "notes.pdf"))


class DuplicateTrashSafetyTests(unittest.TestCase):
    """A DUPLICATE_SKIPPED row recorded no pointer to the file it matched, so
    'Move Duplicates to Trash' could not confirm the surviving copy still
    existed before removing the user's original.
    """

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="audit_dup_")
        self.dest = os.path.join(self.tmp, "dest")
        self.src_dir = os.path.join(self.tmp, "src")
        os.makedirs(self.src_dir)
        self.api = OrganizerAPI(CONFIG_PATH, lambda m: None, lambda *a, **k: None)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _make_duplicate_pair(self):
        engine = FileEngine(self.dest)
        payload = b"identical bytes" * 100

        first = os.path.join(self.src_dir, "a.txt")
        second = os.path.join(self.src_dir, "b.txt")
        for p in (first, second):
            with open(p, "wb") as f:
                f.write(payload)

        target = os.path.join(self.dest, "Documents", "Text", "a.txt")
        dest_path, part_hash, twin = engine.resolve_destination(
            target, "a.txt", len(payload), first)
        self.assertIsNotNone(dest_path)
        engine.copy_file(first, dest_path)
        engine.record_copy(first, dest_path, len(payload), 0.0, part_hash)
        engine.release_reservation(dest_path)

        target2 = os.path.join(self.dest, "Documents", "Text", "b.txt")
        dest2, hash2, twin2 = engine.resolve_destination(
            target2, "b.txt", len(payload), second)
        engine.close()
        return first, second, dest_path, dest2, twin2, hash2, len(payload)

    def test_duplicate_records_which_file_it_matched(self):
        first, second, kept, dest2, twin2, hash2, size = self._make_duplicate_pair()
        self.assertIsNone(dest2, "second identical file should have been skipped")
        self.assertEqual(twin2, kept, "the surviving twin was not reported")

    def test_twin_is_persisted_to_the_database(self):
        first, second, kept, dest2, twin2, hash2, size = self._make_duplicate_pair()
        engine = FileEngine(self.dest)
        engine.record_copy(second, "DUPLICATE_SKIPPED", size, 0.0, hash2, duplicate_of=twin2)
        engine.close()

        records = self.api.get_duplicate_records(self.dest)
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0]["duplicate_of"], kept)

    def test_refuses_to_trash_when_the_surviving_copy_is_gone(self):
        first, second, kept, dest2, twin2, hash2, size = self._make_duplicate_pair()
        engine = FileEngine(self.dest)
        engine.record_copy(second, "DUPLICATE_SKIPPED", size, 0.0, hash2, duplicate_of=twin2)
        engine.close()

        # Simulate the destination copy being lost after the fact.
        os.remove(kept)

        count, refused = self.api.trash_duplicates([second], dest_abs=self.dest)
        self.assertEqual(count, 0)
        self.assertEqual(len(refused), 1)
        self.assertTrue(os.path.exists(second),
                        "the user's only remaining copy was trashed")

    def test_trashes_when_the_surviving_copy_is_verified(self):
        """Negative control: the feature must still work."""
        first, second, kept, dest2, twin2, hash2, size = self._make_duplicate_pair()
        engine = FileEngine(self.dest)
        engine.record_copy(second, "DUPLICATE_SKIPPED", size, 0.0, hash2, duplicate_of=twin2)
        engine.close()

        count, refused = self.api.trash_duplicates([second], dest_abs=self.dest)
        self.assertEqual(count, 1, f"refused: {refused}")
        self.assertFalse(os.path.exists(second))
        self.assertTrue(os.path.exists(kept))

    def test_refuses_when_source_no_longer_matches(self):
        first, second, kept, dest2, twin2, hash2, size = self._make_duplicate_pair()
        engine = FileEngine(self.dest)
        engine.record_copy(second, "DUPLICATE_SKIPPED", size, 0.0, hash2, duplicate_of=twin2)
        engine.close()

        # The source was edited after being marked a duplicate.
        with open(second, "wb") as f:
            f.write(b"completely different content now")

        count, refused = self.api.trash_duplicates([second], dest_abs=self.dest)
        self.assertEqual(count, 0)
        self.assertTrue(os.path.exists(second))


class DedupQueryTests(unittest.TestCase):
    """The lookup fetched every completed row sharing a size and filtered in
    Python, which is quadratic on same-size clusters. Measured 10.7ms -> 0.05ms
    per file on a 40,000-file cluster once part_hash moved into the WHERE.
    """

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="audit_dedup_")
        self.dest = os.path.join(self.tmp, "dest")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_uses_the_size_hash_index(self):
        engine = FileEngine(self.dest)
        plan = engine.conn.execute(
            "EXPLAIN QUERY PLAN SELECT dest_path FROM copies "
            "WHERE size = ? AND part_hash = ? AND status = 'completed' "
            "AND dest_path NOT IN ('DUPLICATE_SKIPPED', '')", (1, "x")
        ).fetchall()
        engine.close()
        self.assertIn("idx_size_hash", " ".join(str(r) for r in plan),
                      "dedup query is not using the (size, part_hash) index")

    def test_still_detects_a_genuine_duplicate(self):
        """Negative control: the optimisation must not break correctness."""
        engine = FileEngine(self.dest)
        payload = b"x" * 5000
        a = os.path.join(self.tmp, "a.bin")
        b = os.path.join(self.tmp, "b.bin")
        for p in (a, b):
            with open(p, "wb") as f:
                f.write(payload)

        kept = os.path.join(self.dest, "a.bin")
        shutil.copyfile(a, kept)
        engine.record_copy(a, kept, len(payload), 0.0)

        is_dup, _h, twin = engine.is_content_duplicate(b, len(payload))
        engine.close()
        self.assertTrue(is_dup)
        self.assertEqual(twin, kept)

    def test_different_content_same_size_is_not_a_duplicate(self):
        engine = FileEngine(self.dest)
        a = os.path.join(self.tmp, "a.bin")
        b = os.path.join(self.tmp, "b.bin")
        with open(a, "wb") as f:
            f.write(b"a" * 5000)
        with open(b, "wb") as f:
            f.write(b"b" * 5000)

        kept = os.path.join(self.dest, "a.bin")
        shutil.copyfile(a, kept)
        engine.record_copy(a, kept, 5000, 0.0)

        is_dup, _h, twin = engine.is_content_duplicate(b, 5000)
        engine.close()
        self.assertFalse(is_dup)
        self.assertEqual(twin, "")


class AutoRepairCostTests(unittest.TestCase):
    """auto_repair_if_needed re-hashed every file already at the destination on
    every merge/resume run: ~2.2 hours on an external HDD holding 400k files,
    before the progress bar moved at all.
    """

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="audit_repair_")
        self.dest = os.path.join(self.tmp, "dest")
        self.api = OrganizerAPI(CONFIG_PATH, lambda m: None, lambda *a, **k: None)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _seed(self):
        engine = FileEngine(self.dest)
        payload = b"z" * 4096
        for i in range(5):
            p = os.path.join(self.dest, f"f{i}.bin")
            with open(p, "wb") as f:
                f.write(payload)
            engine.record_copy(f"/src/f{i}.bin", p, len(payload), 0.0)
        engine.close()

    def test_shallow_repair_does_not_hash(self):
        self._seed()
        calls = []
        original = FileEngine._get_part_hash

        def spy(self_, path, size):
            calls.append(path)
            return original(self_, path, size)

        FileEngine._get_part_hash = spy
        try:
            res = self.api.repair_transfer(self.dest, deep=False)
        finally:
            FileEngine._get_part_hash = original
        self.assertTrue(res["success"])
        self.assertEqual(calls, [], "shallow repair still hashed destination files")

    def test_deep_repair_does_hash(self):
        """The explicit Verify & Repair button must still verify content."""
        self._seed()
        calls = []
        original = FileEngine._get_part_hash

        def spy(self_, path, size):
            calls.append(path)
            return original(self_, path, size)

        FileEngine._get_part_hash = spy
        try:
            res = self.api.repair_transfer(self.dest, deep=True)
        finally:
            FileEngine._get_part_hash = original
        self.assertTrue(res["success"])
        self.assertGreater(len(calls), 0, "deep repair stopped verifying content")

    def test_shallow_repair_still_catches_a_missing_file(self):
        self._seed()
        os.remove(os.path.join(self.dest, "f2.bin"))
        res = self.api.repair_transfer(self.dest, deep=False)
        self.assertTrue(res["success"])
        self.assertEqual(res["repaired_count"], 1)


class SilentDirectorySkipTests(unittest.TestCase):
    """Folders named venv / Caches / .cache / .tmp were pruned anywhere in the
    tree, were not counted as garbage, and never appeared in the preview -- so
    a user who trusted the preview and wiped the source lost them.
    """

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="audit_skip_")
        self.cat = Categorizer(CONFIG_PATH)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_skipped_build_folders_are_reported(self):
        src = os.path.join(self.tmp, "src")
        cache = os.path.join(src, "Caches")
        os.makedirs(cache)
        with open(os.path.join(cache, "important.txt"), "wb") as f:
            f.write(b"data the user may care about")
        with open(os.path.join(src, "normal.txt"), "wb") as f:
            f.write(b"ok")

        scanner = Scanner(self.cat, staging_root=os.path.join(self.tmp, "stage"))
        scanner.scan_directory(src)

        self.assertEqual(scanner.skipped_dir_breakdown.get("Caches"), 1)
        self.assertTrue(any("Caches" in p for p in scanner.skipped_dirs))

    def test_contents_are_still_skipped(self):
        """Behaviour is unchanged -- they are reported, not suddenly copied."""
        src = os.path.join(self.tmp, "src2")
        nm = os.path.join(src, "node_modules")
        os.makedirs(nm)
        with open(os.path.join(nm, "dep.js"), "wb") as f:
            f.write(b"x")

        scanner = Scanner(self.cat, staging_root=os.path.join(self.tmp, "stage2"))
        scanner.scan_directory(src)
        self.assertFalse(any("node_modules" in p for p in scanner.files_to_process))

    def test_os_junk_is_not_reported_as_a_skipped_folder(self):
        """.Spotlight-V100 is never user data; reporting it would be noise."""
        self.assertNotIn(".Spotlight-V100", SKIP_BUILD_DIRS)
        self.assertNotIn(".Trash", SKIP_BUILD_DIRS)

    def test_ordinary_folder_is_not_skipped(self):
        src = os.path.join(self.tmp, "src3")
        keep = os.path.join(src, "My Photos")
        os.makedirs(keep)
        with open(os.path.join(keep, "a.jpg"), "wb") as f:
            f.write(b"x")

        scanner = Scanner(self.cat, staging_root=os.path.join(self.tmp, "stage3"))
        scanner.scan_directory(src)
        self.assertEqual(scanner.skipped_dir_breakdown, {})
        self.assertTrue(any("a.jpg" in p for p in scanner.files_to_process))


class VideoDateTests(unittest.TestCase):
    """_get_video_date only scanned the first 64KB, but moov/mvhd is written at
    the END of any file not saved with faststart. Version-1 (64-bit) mvhd
    atoms were also misread.
    """

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="audit_vid_")
        self.dates = DateExtractor(Categorizer(CONFIG_PATH))

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    @staticmethod
    def _mvhd_v0(when):
        import struct
        qt = int(when.timestamp()) + 2082844800
        return b"mvhd" + bytes([0]) + b"\x00\x00\x00" + struct.pack(">I", qt)

    @staticmethod
    def _mvhd_v1(when):
        import struct
        qt = int(when.timestamp()) + 2082844800
        return b"mvhd" + bytes([1]) + b"\x00\x00\x00" + struct.pack(">Q", qt)

    def _write(self, name, atom, lead=0):
        p = os.path.join(self.tmp, name)
        with open(p, "wb") as f:
            f.write(b"\x00" * lead)
            f.write(atom)
            f.write(b"\x00" * 64)
        return p

    def test_reads_mvhd_at_the_start(self):
        when = datetime.datetime(2020, 5, 10, 12, 0, 0, tzinfo=datetime.timezone.utc)
        p = self._write("head.mp4", self._mvhd_v0(when))
        self.assertEqual(self.dates.extract_date(p), ("2020", "May"))

    def test_reads_mvhd_at_the_end(self):
        """The regression: moov at the tail, past the old 64KB window."""
        when = datetime.datetime(2017, 11, 2, 12, 0, 0, tzinfo=datetime.timezone.utc)
        p = self._write("tail.mp4", self._mvhd_v0(when), lead=400_000)
        self.assertEqual(self.dates.extract_date(p), ("2017", "November"))

    def test_reads_version_1_64bit_mvhd(self):
        when = datetime.datetime(2022, 7, 19, 12, 0, 0, tzinfo=datetime.timezone.utc)
        p = self._write("v1.mp4", self._mvhd_v1(when))
        self.assertEqual(self.dates.extract_date(p), ("2022", "July"))

    def test_garbage_atom_falls_back_instead_of_inventing_a_date(self):
        p = os.path.join(self.tmp, "junk.mp4")
        with open(p, "wb") as f:
            f.write(b"mvhd" + b"\x00" * 32)
        old = time.mktime(datetime.datetime(2015, 4, 1).timetuple())
        os.utime(p, (old, old))
        self.assertEqual(self.dates.extract_date(p), ("2015", "April"))


class CancelStateTests(unittest.TestCase):
    """/api/cancel set a terminal status synchronously while the worker was
    still copying, so the UI re-enabled Start and a second organizer thread
    could be launched against the same checkpoint database.
    """

    def setUp(self):
        from src import app as app_module
        self.app_module = app_module
        self.client = app_module.app.test_client()
        self.token = app_module.API_TOKEN
        app_module.state.status = "idle"

    def tearDown(self):
        self.app_module.state.status = "idle"

    def _post(self, path, payload=None):
        return self.client.post(path, json=payload or {},
                                headers={"X-Organizer-Token": self.token})

    def test_cancel_sets_interim_state_not_terminal(self):
        self.app_module.state.status = "running"
        self._post("/api/cancel")
        self.assertEqual(self.app_module.state.status, "cancelling")

    def test_start_is_refused_while_cancelling(self):
        self.app_module.state.status = "cancelling"
        res = self._post("/api/start", {"source": "/tmp/a", "dest": "/tmp/b"})
        self.assertFalse(res.get_json()["success"])

    def test_cancel_does_not_resurrect_a_finished_run(self):
        self.app_module.state.status = "complete"
        self._post("/api/cancel")
        self.assertEqual(self.app_module.state.status, "complete")


class ApiAuthTests(unittest.TestCase):
    """The local server is unauthenticated to anything that can reach the port,
    so every /api/ route must require the per-launch token.
    """

    def setUp(self):
        from src import app as app_module
        self.app_module = app_module
        self.client = app_module.app.test_client()

    def test_api_routes_reject_a_missing_token(self):
        self.assertEqual(self.client.get("/api/status").status_code, 403)

    def test_api_routes_reject_a_wrong_token(self):
        res = self.client.get("/api/status", headers={"X-Organizer-Token": "nope"})
        self.assertEqual(res.status_code, 403)

    def test_trash_duplicates_requires_a_token(self):
        """The most destructive endpoint."""
        res = self.client.post("/api/trash_duplicates", json={"source_paths": ["/tmp/x"]})
        self.assertEqual(res.status_code, 403)


if __name__ == "__main__":
    unittest.main()

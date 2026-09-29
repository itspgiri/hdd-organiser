"""Pass-1 audit: the three organize-flow guarantees, end to end.

  G1  No organize-flow operation deletes or overwrites the last remaining copy
      of a file.
  G2  Preview (dry run) writes nothing to the source drive.
  G3  Cancelling or crashing mid-transfer never leaves a file lost, or
      half-copied at its final destination.

These tests exercise the real OrganizerAPI.run() on synthetic trees. They are
guarantee checks rather than regression tests for a specific fix, so most of
them also pass on 92ed5c1; the regression tests for individual findings live in
the other tests/test_pass1_*.py files. See AUDIT_REPORT.md, "Pass 1".

Run with:  .venv/bin/python -m unittest discover -s tests -p 'test_pass1_*.py' -v
"""

import hashlib
import os
import plistlib
import sys
import zipfile
from unittest import mock

import xattr

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from pass1_helpers import (  # noqa: E402
    OLD_MTIME, Pass1Case, dest_data_files, read_file, sha256_file,
    snapshot_tree, unique_payload, write_file,
)
from src import file_ops  # noqa: E402

TAKEOUT_MEMBERS = {
    "Takeout/Google Photos/trip.jpg": unique_payload("trip", 5000),
    "Takeout/Drive/report.docx": unique_payload("docx", 3000),
}


def build_rich_source(root):
    """A small tree that exercises every scanner/organizer branch: photos,
    a Live Photo pair, a screenshot, documents, identical duplicates,
    unknown extensions, a code project with .git, a skip-listed folder,
    system garbage, a Google Takeout archive, a read-only file and a file
    carrying a Finder tag."""
    files = {}

    def add(rel, data):
        files[rel] = write_file(os.path.join(root, rel), data)

    add("Photos/IMG_0001.jpg", unique_payload("jpg1", 40_000))
    add("Photos/IMG_0002.HEIC", unique_payload("heic", 30_000))
    add("Photos/IMG_0002.MOV", unique_payload("mov", 60_000))
    add("Photos/Screenshot 2021-05-04 at 10.00.00.png", unique_payload("shot", 10_000))
    add("Docs/tax return.pdf", unique_payload("pdf", 20_000))
    add("Docs/notes.txt", b"plain notes\n")
    add("Docs/Old/notes.txt", b"plain notes\n")  # identical content: de-duplicated
    add("Docs/Other/notes.txt", b"different notes\n")  # same name, different content
    add("Misc/settings.json", b'{"a": 1}')
    add("Misc/LICENSE", b"MIT")
    add("Caches/keep-me.txt", b"a folder that happens to be named Caches")
    add(".DS_Store", b"\0\0\0\1Bud1")
    add("Photos/._IMG_0001.jpg", b"\0\5\x16\7AppleDouble")
    add("webapp/package.json", b'{"name": "webapp"}')
    add("webapp/src/index.js", b"console.log('hi')")
    add("webapp/.git/HEAD", b"ref: refs/heads/main\n")

    zip_path = os.path.join(root, "takeout-20230101T000000Z-001.zip")
    with zipfile.ZipFile(zip_path, "w") as zf:
        for name, data in TAKEOUT_MEMBERS.items():
            zf.writestr(name, data)
    os.utime(zip_path, (OLD_MTIME, OLD_MTIME))
    files["takeout-20230101T000000Z-001.zip"] = zip_path

    os.chmod(files["Docs/tax return.pdf"], 0o444)
    xattr.setxattr(files["Docs/notes.txt"], "com.apple.metadata:_kMDItemUserTags",
                   plistlib.dumps(["Red\n6"], fmt=plistlib.FMT_BINARY))
    return files


class PreviewWritesNothingTests(Pass1Case):
    """G2."""

    def test_preview_writes_nothing_to_source_or_existing_destination(self):
        # The destination already holds an earlier organize (with its
        # checkpoint database), as it would for a merge preview.
        earlier = os.path.join(self.tmp, "earlier_source")
        write_file(os.path.join(earlier, "old.txt"), b"organized last month")
        self.organize(src=earlier)

        build_rich_source(self.src)
        src_before = snapshot_tree(self.src)
        dest_before = snapshot_tree(self.dest)

        ok = self.make_api().run(self.src, self.dest, is_preview=True, dest_mode="merge")

        self.assertTrue(ok, "\n".join(self.logs[-20:]))
        self.assertEqual(snapshot_tree(self.src), src_before, "preview changed the source")
        self.assertEqual(snapshot_tree(self.dest), dest_before, "preview changed the destination")

    def test_preview_into_missing_destination_creates_nothing(self):
        build_rich_source(self.src)
        src_before = snapshot_tree(self.src)
        dest = os.path.join(self.tmp, "not_created_yet")

        ok = self.make_api().run(self.src, dest, is_preview=True, dest_mode="new")

        self.assertTrue(ok, "\n".join(self.logs[-20:]))
        self.assertFalse(os.path.lexists(dest), "preview created the destination folder")
        self.assertEqual(snapshot_tree(self.src), src_before, "preview changed the source")


class NeverDestroysLastCopyTests(Pass1Case):
    """G1 for the transfer itself."""

    def test_full_run_leaves_every_source_file_untouched(self):
        build_rich_source(self.src)
        before = snapshot_tree(self.src)

        self.organize()

        self.assertEqual(snapshot_tree(self.src), before, "a real transfer modified the source")

    def test_every_transferable_source_file_reaches_the_destination(self):
        files = build_rich_source(self.src)
        self.organize()

        dest_hashes = set()
        for rel in dest_data_files(self.dest):
            dest_hashes.add(sha256_file(os.path.join(self.dest, rel)))

        # Not transferred by design (reported in the preview instead):
        # skip-listed folders, system garbage, and the Takeout archive itself,
        # which is replaced by its extracted members (see finding P1-08).
        by_design = {"Caches/keep-me.txt", ".DS_Store", "Photos/._IMG_0001.jpg",
                     "takeout-20230101T000000Z-001.zip"}
        for rel, path in files.items():
            if rel in by_design:
                continue
            self.assertIn(sha256_file(path), dest_hashes, f"{rel} never reached the destination")
        for name, data in TAKEOUT_MEMBERS.items():
            self.assertIn(hashlib.sha256(data).hexdigest(), dest_hashes,
                          f"archive member {name} never reached the destination")

    def test_merge_run_never_overwrites_existing_destination_files(self):
        write_file(os.path.join(self.src, "Letters", "readme.txt"), b"source readme")
        write_file(os.path.join(self.src, "Letters", "same.txt"), b"identical content")
        write_file(os.path.join(self.src, "blob.dat"), b"new unsorted blob")
        write_file(os.path.join(self.src, "proj", "package.json"), b'{"name": "proj"}')
        write_file(os.path.join(self.src, "proj", "index.js"), b"console.log(1)")

        # The destination already has different files at exactly the paths the
        # organizer will want, plus one identical file.
        existing = {
            os.path.join("Documents", "Text", "readme.txt"): b"precious existing readme",
            os.path.join("Documents", "Text", "same.txt"): b"identical content",
            os.path.join("Unsorted", "blob.dat"): b"a different blob",
            os.path.join("Code", "proj", "package.json"): b'{"name": "someone else"}',
        }
        for rel, data in existing.items():
            write_file(os.path.join(self.dest, rel), data)

        self.organize(mode="merge")

        for rel, data in existing.items():
            path = os.path.join(self.dest, rel)
            self.assertTrue(os.path.exists(path), f"{rel} was deleted")
            self.assertEqual(read_file(path), data, f"{rel} was overwritten")
        self.assertEqual(read_file(os.path.join(self.dest, "Documents", "Text", "readme_1.txt")),
                         b"source readme")
        self.assertEqual(read_file(os.path.join(self.dest, "Unsorted", "blob_1.dat")),
                         b"new unsorted blob")
        self.assertTrue(os.path.exists(os.path.join(self.dest, "Code", "proj_1", "index.js")))


class CancelAndCrashTests(Pass1Case):
    """G3 for ordinary files. (Code projects: test_pass1_project_copy_atomic.py.)"""

    N_FILES = 12

    def _make_text_files(self):
        expected = {}
        for i in range(self.N_FILES):
            name = f"file{i:02d}.txt"
            src = write_file(os.path.join(self.src, f"batch{i % 3}", name),
                             unique_payload(name, 64_000 + i * 1000))
            expected[os.path.join("Documents", "Text", name)] = src
        return expected

    def _assert_nothing_half_copied(self, expected):
        for rel, src in expected.items():
            final = os.path.join(self.dest, rel)
            if os.path.exists(final):
                self.assertEqual(sha256_file(final), sha256_file(src),
                                 f"{rel} is half-copied at its final destination")

    def _assert_complete(self, expected):
        self._assert_nothing_half_copied(expected)
        self.assertEqual(dest_data_files(self.dest), sorted(expected),
                         "missing files, leftovers, or renamed duplicates after resume")
        verify = self.make_api().verify_transfer(self.dest)
        self.assertTrue(verify["is_perfect"], verify)

    def test_cancel_mid_transfer_leaves_no_partial_file_and_resume_completes(self):
        expected = self._make_text_files()
        src_before = snapshot_tree(self.src)
        api = self.make_api()
        real_native_copy = file_ops.native_copy
        calls = [0]

        def cancel_during_copy(source_path, target_path):
            calls[0] += 1
            if calls[0] == 4:
                api.cancel()
            return real_native_copy(source_path, target_path)

        with mock.patch.object(file_ops, "native_copy", cancel_during_copy):
            ok = api.run(self.src, self.dest, is_preview=False, dest_mode="new")

        self.assertFalse(ok, "a cancelled run reported success")
        self.assertLess(len(dest_data_files(self.dest)), self.N_FILES, "cancel did not stop the run")
        self._assert_nothing_half_copied(expected)
        self.assertEqual(set(dest_data_files(self.dest)) - set(expected), set(),
                         "a cancelled run left temporary files behind")
        self.assertEqual(snapshot_tree(self.src), src_before)

        self.organize(mode="merge")
        self._assert_complete(expected)
        self.assertEqual(snapshot_tree(self.src), src_before)

    def test_crash_mid_file_copy_leaves_no_partial_at_final_name_and_resume_completes(self):
        expected = self._make_text_files()
        src_before = snapshot_tree(self.src)

        self.run_until_crash(crash_after=5)

        self.assertLess(len([r for r in expected if os.path.exists(os.path.join(self.dest, r))]),
                        self.N_FILES, "the crash happened after the transfer finished")
        self._assert_nothing_half_copied(expected)
        self.assertEqual(snapshot_tree(self.src), src_before, "crash damaged the source")

        self.organize(mode="merge")
        self._assert_complete(expected)
        self.assertEqual(snapshot_tree(self.src), src_before)

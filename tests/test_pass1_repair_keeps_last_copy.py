"""Pass-1 audit, finding P1-01: repair deleted the last copy of a file.

repair_transfer() (the "Verify & Repair" button, and auto_repair_if_needed(),
which runs before every real transfer into a destination that already has a
checkpoint database) permanently deleted any destination file whose size, or
sampled hash, no longer matched its checkpoint record. It never checked that
the source still existed, or that the file was really a broken copy rather
than a file the owner had edited since organizing it. Either way, the deleted
file could be the only copy of that data.

After the fix, repair deletes a file only when it is a cut-short (or complete)
copy of a source file that still exists, so a later run can copy it again.
Anything else is kept, and only its checkpoint record is cleared.

Run with:  .venv/bin/python -m unittest tests.test_pass1_repair_keeps_last_copy -v
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from pass1_helpers import Pass1Case, read_file, unique_payload, write_file  # noqa: E402


class RepairKeepsLastCopyTests(Pass1Case):

    def _organize_one(self, rel, data):
        src_file = write_file(os.path.join(self.src, rel), data)
        self.organize()
        dest_file = self.dest_of(src_file)
        self.assertEqual(read_file(dest_file), data)
        return src_file, dest_file

    def test_repair_keeps_changed_copy_when_source_is_gone(self):
        # Organized, then the source drive was wiped, then the owner kept
        # working on the organized copy.
        src_file, dest_file = self._organize_one("Docs/diary.txt", unique_payload("diary", 20_000))
        os.remove(src_file)
        edited = b"rewritten after the source drive was wiped"
        write_file(dest_file, edited, mtime=None)

        result = self.make_api().repair_transfer(self.dest)

        self.assertTrue(result["success"], result)
        self.assertTrue(os.path.exists(dest_file), "repair deleted the only remaining copy")
        self.assertEqual(read_file(dest_file), edited)
        self.assertEqual(result.get("deleted_count"), 0)
        self.assertEqual(result.get("kept_count"), 1)

    def test_auto_repair_keeps_file_edited_after_organizing(self):
        original = unique_payload("letter", 30_000)
        _src_file, dest_file = self._organize_one("Docs/letter.txt", original)
        # The owner appends to the organized copy. The appended text exists
        # nowhere else.
        with open(dest_file, "ab") as fh:
            fh.write(b"\nP.S. added after organizing")
        edited = read_file(dest_file)

        # Any later real transfer into the same destination runs auto-repair
        # first.
        other = os.path.join(self.tmp, "second_source")
        write_file(os.path.join(other, "new.txt"), b"a new file")
        self.organize(src=other, mode="merge")

        self.assertTrue(os.path.exists(dest_file), "auto-repair deleted a file edited after organizing")
        self.assertEqual(read_file(dest_file), edited)

    def test_deep_repair_keeps_same_size_edit(self):
        original = unique_payload("sheet", 40_000)
        _src_file, dest_file = self._organize_one("Docs/budget.txt", original)
        edited = bytearray(original)
        edited[100:110] = b"EDITED!!!!"
        write_file(dest_file, bytes(edited), mtime=None)

        result = self.make_api().repair_transfer(self.dest, deep=True)

        self.assertTrue(result["success"], result)
        self.assertTrue(os.path.exists(dest_file), "deep repair deleted an edited file")
        self.assertEqual(read_file(dest_file), bytes(edited))
        self.assertEqual(result.get("deleted_count"), 0)

    def test_repair_still_removes_truncated_copy_and_resync_restores_it(self):
        """Negative control: a genuinely cut-short copy is still removed and
        re-copied (passes before and after the fix)."""
        data = unique_payload("video", 50_000)
        src_file, dest_file = self._organize_one("Media/clip.txt", data)
        with open(dest_file, "r+b") as fh:
            fh.truncate(len(data) // 2)

        result = self.make_api().repair_transfer(self.dest)

        self.assertTrue(result["success"], result)
        self.assertEqual(result.get("deleted_count"), 1)
        self.assertFalse(os.path.exists(dest_file))

        self.organize(mode="merge")
        self.assertEqual(self.dest_of(src_file), dest_file)
        self.assertEqual(read_file(dest_file), data)

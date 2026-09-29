"""Pass-1 audit, finding P1-02: organizing deleted the owner's own *.tmp files.

copy_file() wrote each file to "<final name>.tmp" and renamed it into place.
To clean up after an interrupted run, resolve_destination() deleted any
existing "<name>.tmp" whenever it was about to place "<name>" and "<name>"
did not exist yet. But ".tmp" is an ordinary extension: files like
"settings.json.tmp" are organized into the flat Unsorted/ folder, next to
where a later "settings.json" lands. The organizer mistook them for its own
leftovers and permanently deleted them (via _force_remove, bypassing the
Trash and clearing a Finder lock).

After the fix, in-progress copies use an app-specific suffix
(".organizer-partial"), and only files with that suffix are ever cleaned up.

Run with:  .venv/bin/python -m unittest tests.test_pass1_partial_file_naming -v
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from pass1_helpers import Pass1Case, dest_data_files, read_file, write_file  # noqa: E402
from src import file_ops  # noqa: E402


class PartialFileNamingTests(Pass1Case):

    def test_existing_dot_tmp_file_in_destination_is_not_deleted(self):
        # The destination already holds the owner's file, e.g. from an old
        # drive organized last year whose source is long gone.
        users_tmp = write_file(os.path.join(self.dest, "Unsorted", "settings.json.tmp"),
                               b"the only copy of an old settings backup")
        write_file(os.path.join(self.src, "app", "settings.json"), b'{"theme": "dark"}')

        self.organize(mode="merge")

        self.assertTrue(os.path.exists(users_tmp), "organizing deleted an existing .tmp file")
        self.assertEqual(read_file(users_tmp), b"the only copy of an old settings backup")
        self.assertEqual(read_file(os.path.join(self.dest, "Unsorted", "settings.json")),
                         b'{"theme": "dark"}')

    def test_tmp_file_organized_earlier_survives_a_later_run(self):
        old_drive = os.path.join(self.tmp, "old_drive")
        write_file(os.path.join(old_drive, "exports", "data.json.tmp"), b"half-finished export")
        self.organize(src=old_drive)
        organized = os.path.join(self.dest, "Unsorted", "data.json.tmp")
        self.assertEqual(read_file(organized), b"half-finished export")

        write_file(os.path.join(self.src, "exports", "data.json"), b'{"rows": 3}')
        self.organize(mode="merge")

        self.assertTrue(os.path.exists(organized), "a later run deleted a file organized earlier")
        self.assertEqual(read_file(organized), b"half-finished export")
        self.assertEqual(dest_data_files(os.path.join(self.dest, "Unsorted")),
                         ["data.json", "data.json.tmp"])

    def test_leftover_partial_copy_is_still_cleaned_up(self):
        """The fix must not break the cleanup it narrowed: the organizer's own
        leftover from an interrupted copy (new suffix) is still removed before
        the file is placed."""
        write_file(os.path.join(self.src, "exports", "data.json"), b'{"rows": 3}')
        final = os.path.join(self.dest, "Unsorted", "data.json")
        leftover = file_ops.partial_path_for(final)
        write_file(leftover, b'{"ro')

        self.organize()

        self.assertEqual(read_file(final), b'{"rows": 3}')
        self.assertFalse(os.path.exists(leftover), "the interrupted copy's leftover was not removed")

    def test_partial_name_fits_macos_name_limit(self):
        long_name = ("\u00e9" * 120) + ".json"  # 245 bytes in UTF-8
        final = os.path.join(self.dest, "Unsorted", long_name)
        partial = file_ops.partial_path_for(final)
        self.assertEqual(os.path.dirname(partial), os.path.dirname(final))
        self.assertLessEqual(len(os.fsencode(os.path.basename(partial))), 255)
        self.assertTrue(partial.endswith(file_ops.PARTIAL_SUFFIX))
        self.assertNotEqual(partial, final)

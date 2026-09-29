"""P2-03: the kept copy could be the one in another system's trash.

The scan skipped macOS's `.Trash`/`.Trashes`, but not the Windows Recycle
Bin (`$RECYCLE.BIN`) or the Linux desktop trash (`.Trash-1000`), which are
common on an exFAT drive shared with other computers. The keep heuristic
then preferred the recycled copy: Windows renames it to `$R3XK2P1.JPG`, and
the live `IMG_1234.JPG` "looks like a copy" because its name ends in `_1234`.
So "delete all redundant" deleted the live photo and kept the one in the
Recycle Bin, which Windows empties on its own.
"""

import os
import unittest

from pass2_helpers import Pass2Case, payload, write_file

SID = "S-1-5-21-1004336348-1177238915-682003330-1001"


class OtherSystemsTrashTest(Pass2Case):

    def delete_all_redundant(self, groups):
        """What "delete all redundant" does: all but the first file of each group."""
        paths = [f for g in groups for f in g["files"][1:]]
        return self.api().trash_inplace_duplicates(
            paths, self.root, permanent_delete=True, groups=groups)

    def assert_live_copy_survives(self, live, trash_folder):
        groups = self.scan()
        offered = [f for g in groups for f in g["files"]]
        self.assertFalse(
            [f for f in offered if trash_folder in f],
            "files inside another system's trash were grouped with live files")
        self.delete_all_redundant(groups)
        self.assertTrue(os.path.exists(live), "the live copy was deleted")

    def test_windows_recycle_bin_copy_is_not_kept_instead_of_live_photo(self):
        data = payload("p2-03 windows", 8192)
        live = write_file(self.path("Photos", "IMG_1234.JPG"), data)
        write_file(self.path("$RECYCLE.BIN", SID, "$R3XK2P1.JPG"), data)
        write_file(self.path("$RECYCLE.BIN", SID, "$I3XK2P1.JPG"), b"\x02" * 100)

        self.assert_live_copy_survives(live, "$RECYCLE.BIN")

    def test_linux_trash_copy_is_not_kept_instead_of_live_photo(self):
        data = payload("p2-03 linux", 8192)
        live = write_file(self.path("Photos", "2023", "06", "IMG_2001.JPG"), data)
        write_file(self.path(".Trash-1000", "files", "IMG_2001.JPG"), data)

        self.assert_live_copy_survives(live, ".Trash-1000")

    def test_other_self_emptying_folders_are_skipped(self):
        # Folders whose contents another system deletes on its own schedule.
        for i, folder in enumerate(("RECYCLER", "$Recycle.Bin", ".stversions",
                                    ".dropbox.cache", ".TemporaryItems",
                                    "System Volume Information")):
            with self.subTest(folder=folder):
                data = payload(f"p2-03 {folder}", 4096 + i)
                write_file(self.path("Docs", f"report{i}.pdf"), data)
                write_file(self.path(folder, "x", f"report{i}.pdf"), data)
                groups = self.scan()
                self.assertEqual(
                    [f for g in groups for f in g["files"] if folder in f], [])

    def test_duplicates_in_ordinary_folders_are_still_found(self):
        data = payload("p2-03 control", 4096)
        a = write_file(self.path("Trash talk", "clip.mp4"), data)
        b = write_file(self.path("Videos", "clip.mp4"), data)

        groups = self.scan()

        self.assertEqual([sorted(g["files"]) for g in groups], [sorted([a, b])])


if __name__ == "__main__":
    unittest.main()

"""P2-11: a refused quarantine left a full copy of the file in .Duplicates_Trash.

Quarantine moved files with `shutil.move`. When the rename fails, for example
because the file is locked in the Finder or its folder is read-only,
`shutil.move` falls back to copying the file into the trash and then deleting
the original. The delete fails for the same reason, so the file was reported
as "could not be moved" while a full copy of it had been written to the
trash: the opposite of freeing space, and on a nearly full drive the copy
fails with ENOSPC and the message blames free space instead of the lock.

Quarantine now only renames within the drive, which needs no free space.
"""

import os
import stat
import unittest

from pass2_helpers import Pass2Case, payload, write_file


class RefusedQuarantineTest(Pass2Case):

    def setUp(self):
        super().setUp()
        data = payload("p2-11", 200_000)
        self.keep = write_file(self.path("Album", "a.jpg"), data)
        self.dup = write_file(self.path("Backup", "old", "a.jpg"), data)
        self.trash = self.path(".Duplicates_Trash")

    def quarantine(self):
        api = self.api()
        groups = api.find_duplicates_inplace(self.root)
        return api.trash_inplace_duplicates([self.dup], self.root, False, groups)

    def trash_contents(self):
        found = []
        for dirpath, _dirs, files in os.walk(self.trash):
            found += [os.path.join(dirpath, f) for f in files]
        return found

    def test_locked_file_is_refused_without_a_copy_in_the_trash(self):
        os.chflags(self.dup, stat.UF_IMMUTABLE)
        removed, refused, _size = self.quarantine()
        self.assertEqual((removed, len(refused)), (0, 1))
        self.assertTrue(os.path.exists(self.dup))
        self.assertEqual(self.trash_contents(), [])

    @unittest.skipIf(os.geteuid() == 0, "root can move files out of a read-only folder")
    def test_file_in_read_only_folder_is_refused_without_a_copy_in_the_trash(self):
        os.chmod(os.path.dirname(self.dup), 0o500)
        removed, refused, _size = self.quarantine()
        self.assertEqual((removed, len(refused)), (0, 1))
        self.assertTrue(os.path.exists(self.dup))
        self.assertEqual(self.trash_contents(), [])

    def test_ordinary_duplicate_is_still_quarantined(self):
        # Control: passes before and after the fix.
        removed, refused, size = self.quarantine()
        self.assertEqual((removed, refused, size), (1, [], 200_000))
        self.assertFalse(os.path.exists(self.dup))
        self.assertEqual(self.trash_contents(), [os.path.join(self.trash, "a.jpg")])
        self.assertTrue(os.path.exists(self.keep))


if __name__ == "__main__":
    unittest.main()

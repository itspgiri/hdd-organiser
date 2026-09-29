"""P2-05: "freed X of disk space" counted file sizes, not space actually freed.

On APFS, a copy made in Finder (or with `cp -c`) is a clone that shares its
blocks with the original. Deleting it frees almost nothing, but permanent
delete and "Empty .Duplicates_Trash" reported the clone's full size as
reclaimed. These tests run on a real APFS disk image.
"""

import os
import shutil
import subprocess
import unittest

from pass2_helpers import (
    MB, DiskImage, free_bytes, make_api, payload, write_file,
)

# One APFS allocation block of slack for metadata the delete itself frees.
TOLERANCE = 64 * 1024


class ReportedSpaceMatchesDiskTest(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.image = DiskImage("APFS", 64, "P2SPACE").open()

    @classmethod
    def tearDownClass(cls):
        cls.image.close()

    def setUp(self):
        self.root = os.path.join(self.image.mountpoint, self._testMethodName)
        os.makedirs(self.root)
        self.addCleanup(shutil.rmtree, self.root, True)
        self.data = payload(self._testMethodName, 4 * MB)
        self.original = write_file(os.path.join(self.root, "Album", "clip.mov"),
                                   self.data)
        self.copy = os.path.join(self.root, "Backup", "clip.mov")
        os.makedirs(os.path.dirname(self.copy))

    def make_clone(self):
        # What Finder's Duplicate or copy on the same APFS volume does.
        subprocess.run(["cp", "-c", self.original, self.copy], check=True)

    def make_real_copy(self):
        write_file(self.copy, self.data)

    def delete_copy(self):
        api = make_api()
        groups = api.find_duplicates_inplace(self.root)
        self.assertEqual([g["files"] for g in groups], [[self.original, self.copy]])
        before = free_bytes(self.root)
        count, refused, reported = api.trash_inplace_duplicates(
            [self.copy], self.root, permanent_delete=True, groups=groups)
        freed = free_bytes(self.root) - before
        self.assertEqual((count, refused), (1, []))
        return reported, freed

    def test_permanent_delete_of_clone_does_not_claim_space(self):
        self.make_clone()

        reported, freed = self.delete_copy()

        self.assertLess(freed, MB, "deleting a clone should free almost nothing")
        self.assertLessEqual(reported, max(freed, 0) + TOLERANCE)

    def test_emptying_trash_of_clone_does_not_claim_space(self):
        self.make_clone()
        api = make_api()
        groups = api.find_duplicates_inplace(self.root)
        count, _, _ = api.trash_inplace_duplicates(
            [self.copy], self.root, permanent_delete=False, groups=groups)
        self.assertEqual(count, 1)

        before = free_bytes(self.root)
        files_deleted, reported, err = api.empty_duplicates_trash(self.root)
        freed = free_bytes(self.root) - before

        self.assertEqual((files_deleted, err), (1, ""))
        self.assertLess(freed, MB)
        self.assertLessEqual(reported, max(freed, 0) + TOLERANCE)

    def test_permanent_delete_of_real_copy_reports_its_size(self):
        self.make_real_copy()

        reported, freed = self.delete_copy()

        self.assertGreaterEqual(freed, len(self.data) - TOLERANCE)
        self.assertEqual(reported, len(self.data))


if __name__ == "__main__":
    unittest.main()

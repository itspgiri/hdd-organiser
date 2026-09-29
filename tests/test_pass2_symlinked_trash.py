"""P2-07: emptying the trash followed a symlinked .Duplicates_Trash.

`empty_duplicates_trash` walked `<root>/.Duplicates_Trash` with `os.walk`,
which follows the top folder if it is a symbolic link, and deleted every file
it found. If `.Duplicates_Trash` was a link to another folder (for example
one made to put the trash on another drive when this one is full), emptying
the trash permanently deleted that folder's contents.
"""

import os
import unittest

from pass2_helpers import Pass2Case, payload, read_file, write_file


class SymlinkedTrashTest(Pass2Case):

    def setUp(self):
        super().setUp()
        self.elsewhere = os.path.join(self.sandbox, "Documents")
        self.precious = write_file(os.path.join(self.elsewhere, "thesis.docx"),
                                   payload("p2-07 thesis", 5000))
        os.symlink(self.elsewhere, self.path(".Duplicates_Trash"))

    def test_emptying_a_symlinked_trash_deletes_nothing_behind_it(self):
        files_deleted, freed, err = self.api().empty_duplicates_trash(self.root)

        self.assertTrue(os.path.exists(self.precious))
        self.assertEqual(read_file(self.precious), payload("p2-07 thesis", 5000))
        self.assertEqual((files_deleted, freed), (0, 0))
        self.assertIn("symbolic link", err)

    def test_quarantine_does_not_move_files_through_a_symlinked_trash(self):
        data = payload("p2-07 dup", 4096)
        keep = write_file(self.path("a", "x.bin"), data)
        dup = write_file(self.path("b", "x.bin"), data)
        groups = self.api().find_duplicates_inplace(self.root)

        count, refused, _ = self.api().trash_inplace_duplicates(
            [dup], self.root, permanent_delete=False, groups=groups)

        self.assertEqual(count, 0)
        self.assertTrue(refused)
        self.assertTrue(os.path.exists(dup) and os.path.exists(keep))
        self.assertEqual(sorted(os.listdir(self.elsewhere)), ["thesis.docx"])


class OrdinaryTrashTest(Pass2Case):

    def test_emptying_a_real_trash_folder_still_works(self):
        write_file(self.path(".Duplicates_Trash", "x.bin"), payload("p2-07 a", 3000))
        write_file(self.path(".Duplicates_Trash", "x_1.bin"), payload("p2-07 b", 2000))

        files_deleted, freed, err = self.api().empty_duplicates_trash(self.root)

        self.assertEqual((files_deleted, err), (2, ""))
        self.assertEqual(freed, 5000)
        self.assertFalse(os.path.lexists(self.path(".Duplicates_Trash")))


if __name__ == "__main__":
    unittest.main()

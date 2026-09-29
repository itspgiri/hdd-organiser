"""P2-08: emptying .Duplicates_Trash reported success when files were left.

`empty_duplicates_trash` ignored every failed unlink (`except OSError: pass`)
and then removed the folder with `shutil.rmtree(..., ignore_errors=True)`, so
it returned no error however many files were left behind. The UI then said
the trash had been emptied, or, when nothing at all could be deleted, that
there was no .Duplicates_Trash folder.

Here a subfolder of the trash is made read-only (0o500), so the file inside
it cannot be unlinked, as happens with a drive that went read-only or a file
the user cannot delete.
"""

import os
import unittest

from pass2_helpers import Pass2Case, api_client, payload, write_file


@unittest.skipIf(os.geteuid() == 0, "root can delete files in a read-only folder")
class EmptyTrashLeftoversTest(Pass2Case):

    def make_trash(self):
        self.trash = self.path(".Duplicates_Trash")
        self.deletable = write_file(
            os.path.join(self.trash, "Photos", "a.jpg"), payload("p2-08 a", 50_000))
        self.stuck = write_file(
            os.path.join(self.trash, "Locked", "b.jpg"), payload("p2-08 b", 50_000))
        os.chmod(os.path.dirname(self.stuck), 0o500)

    def test_files_left_in_trash_are_reported_as_an_error(self):
        self.make_trash()
        files_deleted, _freed, err = self.api().empty_duplicates_trash(self.root)
        self.assertFalse(os.path.exists(self.deletable))
        self.assertTrue(os.path.exists(self.stuck))
        self.assertEqual(files_deleted, 1)
        self.assertTrue(err, "a file was left in .Duplicates_Trash but no error was returned")
        self.assertIn("1 file", err)
        self.assertIn(self.trash, err)

    def test_endpoint_does_not_report_success_when_files_are_left(self):
        self.make_trash()
        client, headers = api_client()
        res = client.post("/api/dup_empty_trash", headers=headers,
                          json={"root_folder": self.root})
        body = res.get_json()
        self.assertTrue(os.path.exists(self.stuck))
        self.assertFalse(body["success"], body)
        self.assertEqual(body["files_deleted"], 1)

    def test_trash_that_empties_completely_still_reports_success(self):
        # Control: passes before and after the fix.
        trash = self.path(".Duplicates_Trash")
        write_file(os.path.join(trash, "Photos", "c.jpg"), payload("p2-08 c", 50_000))
        files_deleted, _freed, err = self.api().empty_duplicates_trash(self.root)
        self.assertEqual((files_deleted, err), (1, ""))
        self.assertFalse(os.path.lexists(trash))


if __name__ == "__main__":
    unittest.main()

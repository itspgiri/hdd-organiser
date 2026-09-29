"""P2-02: delete and quarantine re-checked duplicates with a sampled hash.

Before removing a file, `trash_inplace_duplicates` re-checked it against the
copy being kept, but only compared the size and a hash of the first and last
1 MB. A file changed in the middle after the scan (same size) still passed,
so it was deleted even though it was no longer a duplicate. VeraCrypt
containers, VM disks and databases change exactly like that, and VeraCrypt
even keeps the container's modification time by default.
"""

import os
import unittest

from pass2_helpers import MB, Pass2Case, payload, read_file, write_file


def change_middle(path, tag):
    """Rewrites 4 KB in the middle of the file, keeping its size and mtime."""
    st = os.stat(path)
    data = bytearray(read_file(path))
    mid = len(data) // 2
    data[mid:mid + 4096] = payload(tag, 4096)
    write_file(path, bytes(data))
    os.utime(path, ns=(st.st_atime_ns, st.st_mtime_ns))
    return bytes(data)


class DeleteComparesEveryByteTest(Pass2Case):

    def setUp(self):
        super().setUp()
        self.original = payload("p2-02 container", 3 * MB)
        self.keep = write_file(self.path("a", "vault.hc"), self.original)
        self.dup = write_file(self.path("b", "vault.hc"), self.original)
        self.groups = self.scan()
        self.assertEqual([g["files"] for g in self.groups], [[self.keep, self.dup]])

    def remove(self, paths, permanent):
        return self.api().trash_inplace_duplicates(
            paths, self.root, permanent_delete=permanent, groups=self.groups)

    def test_permanent_delete_keeps_copy_changed_after_scan(self):
        changed = change_middle(self.dup, "p2-02 new data")

        count, refused, _ = self.remove([self.dup], permanent=True)

        self.assertEqual(count, 0)
        self.assertEqual(len(refused), 1)
        self.assertEqual(read_file(self.dup), changed)
        self.assertEqual(read_file(self.keep), self.original)

    def test_quarantine_keeps_copy_changed_after_scan(self):
        changed = change_middle(self.dup, "p2-02 new data")

        count, refused, _ = self.remove([self.dup], permanent=False)

        self.assertEqual(count, 0)
        self.assertEqual(len(refused), 1)
        self.assertEqual(read_file(self.dup), changed)
        trash = self.path(".Duplicates_Trash")
        self.assertEqual(os.listdir(trash) if os.path.isdir(trash) else [], [])

    def test_delete_refused_when_kept_copy_changed_after_scan(self):
        # Now the copy selected for deletion is the last copy of the original.
        change_middle(self.keep, "p2-02 edited original")

        count, refused, _ = self.remove([self.dup], permanent=True)

        self.assertEqual(count, 0)
        self.assertEqual(len(refused), 1)
        self.assertEqual(read_file(self.dup), self.original)

    def test_unchanged_duplicate_is_still_deleted(self):
        count, refused, reclaimed = self.remove([self.dup], permanent=True)

        self.assertEqual((count, refused), (1, []))
        self.assertFalse(os.path.exists(self.dup))
        self.assertEqual(read_file(self.keep), self.original)


if __name__ == "__main__":
    unittest.main()

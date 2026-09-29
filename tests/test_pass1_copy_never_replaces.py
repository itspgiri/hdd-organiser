"""Pass-1 audit, finding P1-10: copy_file() replaced whatever was at its target.

resolve_destination() picks and reserves a free name, but copy_file() only
renames the finished copy into place later. If a file appeared at that name
in between (another app, a second organizer window, the owner), copy_file()
permanently deleted it with _force_remove (clearing even a Finder lock) and
put its own copy there.

Run with:  .venv/bin/python -m unittest tests.test_pass1_copy_never_replaces -v
"""

import os
import sys
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from pass1_helpers import Pass1Case, read_file, write_file  # noqa: E402
from src import file_ops  # noqa: E402


class CopyNeverReplacesTests(Pass1Case):

    def test_file_that_appears_at_the_target_during_the_copy_is_kept(self):
        os.makedirs(self.dest)
        src = write_file(os.path.join(self.src, "report.txt"), b"the organizer's copy")
        target = os.path.join(self.dest, "report.txt")
        engine = file_ops.FileEngine(self.dest)
        self.addCleanup(engine.close)
        real_native_copy = file_ops.native_copy

        def someone_writes_the_target_meanwhile(source_path, tmp_path):
            ok = real_native_copy(source_path, tmp_path)
            write_file(target, b"written by another app meanwhile", mtime=None)
            return ok

        with mock.patch.object(file_ops, "native_copy", someone_writes_the_target_meanwhile):
            with self.assertRaises(OSError):
                engine.copy_file(src, target)

        self.assertEqual(read_file(target), b"written by another app meanwhile",
                         "copy_file deleted a file it did not create")
        self.assertEqual(sorted(n for n in os.listdir(self.dest) if not n.startswith(".")),
                         ["report.txt"], "the in-progress copy was left behind")

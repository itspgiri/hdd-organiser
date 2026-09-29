"""Pass-4 audit, finding P4-02: CLI whitespace destination resolved to os.getcwd().

In src/cli.py:
  1. Passing a whitespace-only destination string (e.g. `main.py /src "   " --copy`)
     bypassed `if not dest:`, stripped to `""`, and then `os.path.abspath("")`
     silently resolved to the current working directory (`os.getcwd()`).
     `OrganizerAPI.validate_paths` then saw a non-empty directory path and the
     CLI organized files directly into the current working directory.
  2. Passing an explicit empty string `""` for source or destination on the
     command line (`main.py /src "" --copy`) triggered `if not dest:` and
     launched the interactive AppleScript folder picker instead of rejecting
     the empty argument.
  3. When `--copy` extracted an archive containing only garbage/skipped files
     so `files_to_process` was empty, `run_cli` returned early at "No files to
     move!" without calling `scanner.cleanup_staging()`, leaving
     `.organizer_staging` behind on the destination.

Run with:  .venv/bin/python3 -m unittest tests/test_pass4_cli_dest_validation.py -v
"""

import io
import os
import sys
import unittest
import zipfile
from unittest import mock

from rich.console import Console

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from pass1_helpers import Pass1Case, snapshot_tree, write_file  # noqa: E402
from src import cli, utils  # noqa: E402


class CliDestValidationTests(Pass1Case):

    def _run_cli_argv(self, argv):
        buf = io.StringIO()
        test_console = Console(file=buf, force_terminal=False, width=120, theme=utils.custom_theme)
        with mock.patch.object(sys, "argv", argv), \
             mock.patch.object(utils, "console", test_console):
            cli.run_cli()
        return buf.getvalue()

    def test_whitespace_destination_does_not_organize_into_cwd(self):
        write_file(os.path.join(self.src, "notes.txt"), b"do not copy into cwd")
        cwd_dir = os.path.join(self.tmp, "fake_cwd")
        os.makedirs(cwd_dir)
        cwd_before = snapshot_tree(cwd_dir)

        old_cwd = os.getcwd()
        try:
            os.chdir(cwd_dir)
            out = self._run_cli_argv(["main.py", self.src, "   ", "--copy"])
        finally:
            os.chdir(old_cwd)

        self.assertEqual(
            snapshot_tree(cwd_dir),
            cwd_before,
            "whitespace destination caused CLI to organize files into os.getcwd()",
        )
        self.assertIn("Destination path cannot be empty", out)

    def test_explicit_empty_destination_does_not_open_applescript_dialog(self):
        write_file(os.path.join(self.src, "notes.txt"), b"hello")
        picker_mock = mock.MagicMock(return_value=self.dest)

        with mock.patch.object(cli, "_macos_choose_folder", picker_mock):
            out = self._run_cli_argv(["main.py", self.src, "", "--copy"])

        picker_mock.assert_not_called()
        self.assertFalse(os.path.exists(self.dest), "empty destination should have been rejected")
        self.assertIn("Destination path cannot be empty", out)

    def test_empty_archive_with_copy_flag_cleans_up_staging(self):
        zip_path = os.path.join(self.src, "takeout-20240101T000000Z-001.zip")
        with zipfile.ZipFile(zip_path, "w") as zf:
            zf.writestr("__MACOSX/._junk", b"appledouble")
            zf.writestr("Takeout/.DS_Store", b"ds_store")

        out = self._run_cli_argv(["main.py", self.src, self.dest, "--copy"])

        self.assertIn("No files to move", out)
        staging = os.path.join(self.dest, ".organizer_staging")
        self.assertFalse(
            os.path.exists(staging),
            "CLI returned early on empty scan without cleaning up .organizer_staging",
        )


if __name__ == "__main__":
    unittest.main()

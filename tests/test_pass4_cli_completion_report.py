"""Pass-4 audit, finding P4-01: CLI transfer loop reported "100% organized safely" on failures.

In src/cli.py, the transfer loop had the same flaw fixed in OrganizerAPI.run()
by P1-06:
  1. A file that disappeared or became unreadable between the scan and
     os.path.getsize/getmtime was skipped with a bare `continue` (no warning,
     no checkpoint DB record, so verify_transfer thought the destination was
     100% complete).
  2. Files or code projects that failed during copy were not counted.
  3. The CLI always ended with "All done! 100% of files organized safely." and
     recorded "status": "Completed" in run_history.json.

Run with:  .venv/bin/python3 -m unittest tests/test_pass4_cli_completion_report.py -v
"""

import io
import json
import os
import sys
import unittest
from unittest import mock

from rich.console import Console

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from pass1_helpers import Pass1Case, write_file  # noqa: E402
from src import cli, utils  # noqa: E402

SUCCESS_LINE = "100% of files organized safely"


class CliCompletionReportTests(Pass1Case):

    def _run_cli(self, extra_args=()):
        buf = io.StringIO()
        test_console = Console(file=buf, force_terminal=False, width=120, theme=utils.custom_theme)
        argv = ["main.py", self.src, self.dest, "--copy", *extra_args]
        with mock.patch.object(sys, "argv", argv), \
             mock.patch.object(utils, "console", test_console):
            cli.run_cli()
        return buf.getvalue()

    def _history(self):
        with open(os.path.join(self.tmp, "run_history.json"), encoding="utf-8") as fh:
            return json.load(fh)[0]

    def test_cli_failed_copy_is_not_reported_as_100_percent_success(self):
        write_file(os.path.join(self.src, "Docs", "fine.txt"), b"copies fine")
        locked = write_file(os.path.join(self.src, "Docs", "unreadable.txt"), b"cannot be read")
        os.chmod(locked, 0)

        out = self._run_cli()

        self.assertNotIn(SUCCESS_LINE, out, "CLI run with a failed file claimed 100% success")
        self.assertIn("Finished with problems: 1 file(s)", out)
        record = self._history()
        self.assertEqual(record["status"], "Completed with errors")
        self.assertEqual(record["failed_files"], 1)

    def test_cli_file_that_vanishes_after_scan_is_recorded_and_reported(self):
        write_file(os.path.join(self.src, "Docs", "stays.txt"), b"still here")
        write_file(os.path.join(self.src, "Docs", "gone.txt"), b"deleted mid-transfer")
        real_compute = cli.compute_relative_destination

        def delete_gone_txt_first(categorizer, dates, file_path, *rest):
            rel = real_compute(categorizer, dates, file_path, *rest)
            if os.path.basename(file_path) == "gone.txt":
                os.remove(file_path)
            return rel

        with mock.patch.object(cli, "compute_relative_destination", delete_gone_txt_first):
            out = self._run_cli()

        self.assertNotIn(SUCCESS_LINE, out, "CLI run that dropped a vanished file claimed 100% success")
        self.assertIn("gone.txt", out, "vanished file was dropped without any warning")
        verify = self.make_api().verify_transfer(self.dest)
        self.assertFalse(verify["is_perfect"], "verify_transfer was not informed of the vanished file")
        record = self._history()
        self.assertEqual(record["status"], "Completed with errors")
        self.assertEqual(record["failed_files"], 1)

    def test_cli_failed_project_is_counted_and_reported(self):
        write_file(os.path.join(self.src, "Docs", "ok.txt"), b"ok")
        write_file(os.path.join(self.src, "repo", ".git", "HEAD"), b"ref: refs/heads/main\n")
        write_file(os.path.join(self.src, "repo", "main.py"), b"print(1)\n")

        with mock.patch.object(cli, "copy_project_intact", return_value=(False, "simulated disk error")):
            out = self._run_cli()

        self.assertNotIn(SUCCESS_LINE, out, "CLI run with a failed project claimed 100% success")
        self.assertIn("1 code project(s) were not copied", out)
        record = self._history()
        self.assertEqual(record["status"], "Completed with errors")
        self.assertEqual(record["failed_projects"], 1)

    def test_cli_clean_run_still_reports_success(self):
        """Negative control: passes before and after."""
        write_file(os.path.join(self.src, "Docs", "a.txt"), b"a")
        out = self._run_cli()
        self.assertIn(SUCCESS_LINE, out)
        self.assertEqual(self._history()["status"], "Completed")


if __name__ == "__main__":
    unittest.main()

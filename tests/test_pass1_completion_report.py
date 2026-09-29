"""Pass-1 audit, finding P1-06: a transfer with failures reported "100% safe".

run() always ended with "All done! 100% of files organized safely." and
recorded the run as "Completed" in the history, even when files or whole
code projects had failed to copy (only a warning further up the log said
so), and a file that disappeared or became unreadable after the scan was
dropped without any log line, record, or count. The owner uses that final
line to decide whether the source drive can be erased.

Run with:  .venv/bin/python -m unittest tests.test_pass1_completion_report -v
"""

import json
import os
import sys
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from pass1_helpers import Pass1Case, write_file  # noqa: E402
from src import api_organizer  # noqa: E402

SUCCESS_LINE = "100% of files organized safely"


class CompletionReportTests(Pass1Case):

    def _history(self):
        with open(os.path.join(self.tmp, "run_history.json")) as fh:
            return json.load(fh)[0]

    def test_failed_copy_is_not_reported_as_success(self):
        write_file(os.path.join(self.src, "Docs", "fine.txt"), b"copies fine")
        locked = write_file(os.path.join(self.src, "Docs", "unreadable.txt"), b"cannot be read")
        os.chmod(locked, 0)

        self.organize()

        log = "\n".join(self.logs)
        self.assertNotIn(SUCCESS_LINE, log, "a run with a failed file claimed 100% success")
        self.assertIn("Finished with problems: 1 file(s)", log)
        record = self._history()
        self.assertEqual(record["status"], "Completed with errors")
        self.assertEqual(record["failed_files"], 1)

    def test_file_that_vanishes_after_the_scan_is_reported(self):
        write_file(os.path.join(self.src, "Docs", "stays.txt"), b"still here")
        write_file(os.path.join(self.src, "Docs", "gone.txt"), b"deleted mid-transfer")
        real = api_organizer.compute_relative_destination

        def delete_gone_txt_first(categorizer, dates, file_path, *rest):
            rel = real(categorizer, dates, file_path, *rest)
            if os.path.basename(file_path) == "gone.txt":
                os.remove(file_path)  # e.g. the owner tidied the source meanwhile
            return rel

        with mock.patch.object(api_organizer, "compute_relative_destination", delete_gone_txt_first):
            self.organize()

        log = "\n".join(self.logs)
        self.assertNotIn(SUCCESS_LINE, log, "a run that dropped a file claimed 100% success")
        self.assertIn("gone.txt", log, "the vanished file was dropped without a word")
        verify = self.make_api().verify_transfer(self.dest)
        self.assertFalse(verify["is_perfect"], "Verify does not know about the dropped file")

    def test_clean_run_still_reports_success(self):
        """Negative control: passes before and after."""
        write_file(os.path.join(self.src, "Docs", "a.txt"), b"a")
        self.organize()
        self.assertIn(SUCCESS_LINE, "\n".join(self.logs))
        self.assertEqual(self._history()["status"], "Completed")

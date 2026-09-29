"""P3-05: a transfer with failures was reported as "Organization Complete!".

Since P1-06 the organizer counts the files and code projects it could not
copy, logs "Finished with problems ... Do not erase the source" and saves
the run as "Completed with errors". But `run()` still returns True, so the
status was 'complete' and the progress screen's heading read "Organization
Complete!". `/api/status` carried no failure information, so the UI could not
know; the only sign was one log line among many.

The failing run here copies a code project with an unreadable file in it (the
copy fails, the project is counted as not copied); the other files copy fine.
"""

import os
import unittest

from pass3_helpers import UITestCase, make_source_drive, make_unreadable


class FailedTransferReportTest(UITestCase):

    def source_with_failing_project(self):
        source = make_source_drive(self.path("source"))
        make_unreadable(os.path.join(source, "Projects", "my-app", "index.js"))
        return source

    def test_transfer_with_a_failed_project_is_not_reported_as_complete(self):
        _, _, status = self.run_transfer(self.source_with_failing_project())
        self.assertEqual(status, "complete")  # the backend still says complete
        heading = self.page.inner_text("#status-heading")
        self.assertNotIn("Organization Complete", heading)
        self.assertIn("Finished with problems", heading)
        box = self.page.locator("#run-problems")
        self.assertTrue(box.is_visible(), "no visible summary of what was not copied")
        text = box.inner_text()
        self.assertIn("1 code project", text)
        self.assertIn("my-app", text)
        self.assertIn("Do not erase the source", text)
        self.assertNoJsErrors()

    def test_clean_transfer_is_still_reported_as_complete(self):
        """Negative control: passes before and after the fix."""
        self.run_transfer()
        self.assertIn("Organization Complete", self.page.inner_text("#status-heading"))
        self.assertFalse(self.page.locator("#run-problems").is_visible())
        self.assertNoJsErrors()


if __name__ == "__main__":
    unittest.main()

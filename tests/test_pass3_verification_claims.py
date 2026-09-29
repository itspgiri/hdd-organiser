"""P3-06: the verification checker claimed "100% safe to delete" the source.

"Run Integrity Verification Checker" said "Running 100% SHA-256 Hash & File
Size Integrity Verification Check…", then "🟢 100% Integrity Verified & Safe
to Delete!" and "It is now 100% safe to delete your original source folder!".
`verify_transfer` checks less than that:
- only the files recorded one by one in the checkpoint's `copies` table. Code
  projects are copied as whole folders and never checked, and a project that
  failed to copy is not recorded at all, so it cannot be reported missing;
- nothing about what the scan skipped on purpose (build and cache folders,
  system files) or could not read;
- a SHA-256 of the first and last 1 MB of a file, not of the whole file, and
  only for the first 10,000 files; after that, size only.

So after the transfer in P3-05, where a code project was not copied, the
checker still said it was 100% safe to delete the source.
"""

import os
import unittest

from pass3_helpers import UITestCase, make_source_drive, make_unreadable

VERIFY_BUTTON = "button:has-text('Run Integrity Verification Checker')"
LOADING = ("Running 100%", "Checking the copied files")  # before / after the fix


class VerificationClaimsTest(UITestCase):

    def verify(self, button=VERIFY_BUTTON):
        with self.page.expect_response(lambda r: "/api/verify_transfer" in r.url):
            self.page.locator(button).first.click()
        self.wait_for("!%s.some(t => document.getElementById('review-content-area')"
                      ".innerText.includes(t))" % list(LOADING))
        return self.page.inner_text("#review-content-area")

    def assertNoOverclaims(self, text):
        lowered = text.lower()
        self.assertNotIn("safe to delete", lowered)
        self.assertNotIn("100%", text)

    def failing_source(self):
        source = make_source_drive(self.path("source"))
        make_unreadable(os.path.join(source, "Projects", "my-app", "index.js"))
        return source

    def test_after_a_failed_project_it_does_not_say_safe_to_delete(self):
        self.run_transfer(self.failing_source())
        text = self.verify()
        self.assertNoOverclaims(text)
        self.assertIn("1 code project(s) were not copied", text)
        self.assertIn("Do not erase the source", text)
        self.assertNoJsErrors()

    def test_from_history_it_reports_that_runs_failures(self):
        self.run_transfer(self.failing_source())
        self.open_app()  # a new session: the page remembers nothing of the run
        self.click("#crumb-history")
        text = self.verify(".history-verify-btn")
        self.assertNoOverclaims(text)
        self.assertIn("1 code project(s) were not copied", text)
        self.assertNoJsErrors()

    def test_after_a_clean_run_it_says_what_was_and_was_not_checked(self):
        self.run_transfer()
        text = self.verify()
        self.assertNoOverclaims(text)
        self.assertIn("first and last 1 MB", text)
        self.assertIn("Not covered by this check", text)
        self.assertIn("code projects", text)
        self.assertNoJsErrors()


if __name__ == "__main__":
    unittest.main()

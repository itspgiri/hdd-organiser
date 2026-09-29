"""P3-07: the progress screen's controls outlived the run.

- After a run ended, "⛔ Cancel Operation" stayed live. Clicking it asked to
  "cancel the organization process midway", told the server to cancel, set
  the heading to "⛔ Cancelling — finishing the file in flight..." over a
  finished run, added "Cancellation requested" to that run's log, and left
  Start disabled, reading "Cancelling...": no poll was left to restore it.
- "Return to Setup" was disabled only in the page's first run. From the
  second run on it stayed enabled during the run.
- "Run Integrity Check" from History reused the progress screen as if a run
  were starting: heading "Organizing Files...", a 0% bar, a live Cancel, and
  "Return to Setup" disabled.

A stand-in organizer whose run() waits for the test lets the tests look at
the screen while a run is active.
"""

import threading
import unittest

from pass3_helpers import UITestCase, api_mod, app_mod, make_source_drive


class HeldRun(api_mod.OrganizerAPI):
    """OrganizerAPI whose run() returns only when `go` is set (or on cancel)."""

    go = threading.Event()

    def run(self, *args, **kwargs):
        self.log_cb("held run started")
        while not HeldRun.go.wait(0.05):
            if self.cancelled:
                return False
        return True


def controls(page):
    return page.evaluate("""() => {
        const b = id => { const e = document.getElementById(id);
            return {text: e.innerText.trim(), disabled: e.disabled,
                    shown: !!(e.offsetWidth || e.offsetHeight),
                    icon: !!e.querySelector('svg, i[data-lucide]')}; };
        return {heading: document.getElementById('status-heading').innerText.trim(),
                cancel: b('cancel-btn'), start: b('start-btn'), done: b('done-btn')};
    }""")


class RunControlsTest(UITestCase):

    def setUp(self):
        super().setUp()
        saved = app_mod.OrganizerAPI
        app_mod.OrganizerAPI = HeldRun
        self.addCleanup(setattr, app_mod, "OrganizerAPI", saved)
        HeldRun.go.set()
        self.addCleanup(HeldRun.go.set)

    def start_held_run(self):
        """From the finished first run: Return to Setup -> Start -> running."""
        HeldRun.go.clear()
        self.click("#done-btn")
        self.click("#start-btn")
        self.wait_for("isTransferActive === true")

    def test_no_cancel_after_the_run_has_ended(self):
        self.run_transfer()
        self.assertFalse(controls(self.page)["cancel"]["shown"])
        self.page.evaluate("cancelCurrentOperation()")  # as a stray click would
        self.page.wait_for_timeout(300)
        state = controls(self.page)
        self.assertEqual(state["heading"], "Organization Complete!")
        self.assertFalse(state["start"]["disabled"])
        with app_mod.state_lock:
            self.assertFalse(any("Cancellation" in line for line in app_mod.state.logs))
        self.assertEqual(self.dialogs, [])
        self.assertNoJsErrors()

    def test_controls_during_and_after_a_second_run(self):
        self.run_transfer()
        self.start_held_run()
        during = controls(self.page)
        self.assertTrue(during["cancel"]["shown"] and not during["cancel"]["disabled"])
        self.assertTrue(during["done"]["disabled"], "Return to Setup is live during the run")
        HeldRun.go.set()
        self.server.wait_run_ended()
        self.wait_for("pollInterval === null")
        after = controls(self.page)
        self.assertFalse(after["cancel"]["shown"])
        self.assertFalse(after["done"]["disabled"])
        self.assertFalse(after["start"]["disabled"])
        self.assertTrue(after["start"]["icon"], "Start lost its icon")
        self.assertNoJsErrors()

    def test_cancel_still_works_during_a_run(self):
        self.run_transfer()
        self.start_held_run()
        self.click("#cancel-btn")
        self.assertEqual(self.server.wait_run_ended(), "cancelled")
        self.wait_for("pollInterval === null")
        state = controls(self.page)
        self.assertEqual(state["heading"], "Operation Cancelled")
        self.assertFalse(state["cancel"]["shown"])
        self.assertFalse(state["start"]["disabled"])
        self.assertNoJsErrors()

    def test_history_check_does_not_look_like_a_run(self):
        app_mod.OrganizerAPI = api_mod.OrganizerAPI  # a real run writes history
        self.run_transfer(make_source_drive(self.path("source")))
        self.open_app()  # a new session
        self.click("#crumb-history")
        with self.page.expect_response(lambda r: "/api/verify_transfer" in r.url):
            self.page.locator(".history-verify-btn").first.click()
        self.page.wait_for_timeout(300)
        state = controls(self.page)
        self.assertNotIn("Organizing", state["heading"])
        self.assertFalse(state["cancel"]["shown"])
        self.assertFalse(state["done"]["disabled"], "Return to Setup is disabled")
        self.assertFalse(self.page.locator("#progress-box").is_visible())
        self.assertNoJsErrors()


if __name__ == "__main__":
    unittest.main()

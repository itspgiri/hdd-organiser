import unittest

import src.app as app_mod


class Pass4CancelWhenIdleTests(unittest.TestCase):
    """Regression tests for P4-05: /api/cancel and /api/dup_scan_cancel must not mutate state when idle or complete."""

    def setUp(self):
        self.client = app_mod.app.test_client()
        self.headers = {"X-Organizer-Token": app_mod.API_TOKEN}
        with app_mod.state_lock:
            self.saved_status = app_mod.state.status
            self.saved_message = app_mod.state.message
            self.saved_logs = list(app_mod.state.logs)
            self.saved_dup_status = app_mod.state.dup_status
            self.saved_dup_cancelled = app_mod.active_dup_scanner_cancelled

    def tearDown(self):
        with app_mod.state_lock:
            app_mod.state.status = self.saved_status
            app_mod.state.message = self.saved_message
            app_mod.state.logs = self.saved_logs
            app_mod.state.dup_status = self.saved_dup_status
            app_mod.active_dup_scanner_cancelled = self.saved_dup_cancelled

    def test_cancel_when_idle_or_complete_does_not_overwrite_message_or_logs(self):
        for terminal_status, msg in (("idle", "Waiting to start..."), ("complete", "All done! 100% of files organized safely.")):
            with app_mod.state_lock:
                app_mod.state.status = terminal_status
                app_mod.state.message = msg
                app_mod.state.logs = ["initial log line"]

            resp = self.client.post("/api/cancel", headers=self.headers)
            self.assertEqual(resp.status_code, 200)

            with app_mod.state_lock:
                self.assertEqual(app_mod.state.status, terminal_status)
                self.assertEqual(app_mod.state.message, msg)
                self.assertEqual(app_mod.state.logs, ["initial log line"])

    def test_cancel_when_running_transitions_to_cancelling_and_logs(self):
        with app_mod.state_lock:
            app_mod.state.status = "running"
            app_mod.state.message = "Copying file 1 of 10..."
            app_mod.state.logs = []

        resp = self.client.post("/api/cancel", headers=self.headers)
        self.assertEqual(resp.status_code, 200)

        with app_mod.state_lock:
            self.assertEqual(app_mod.state.status, "cancelling")
            self.assertIn("Cancellation requested", app_mod.state.message)
            self.assertTrue(any("Cancellation requested" in line for line in app_mod.state.logs))

    def test_dup_scan_cancel_when_idle_or_complete_does_not_set_cancel_flag(self):
        for terminal_status in ("idle", "complete", "cancelled", "error"):
            with app_mod.state_lock:
                app_mod.state.dup_status = terminal_status
                app_mod.active_dup_scanner_cancelled = False

            resp = self.client.post("/api/dup_scan_cancel", headers=self.headers)
            self.assertEqual(resp.status_code, 200)

            with app_mod.state_lock:
                self.assertEqual(app_mod.state.dup_status, terminal_status)
                self.assertFalse(app_mod.active_dup_scanner_cancelled)

    def test_dup_scan_cancel_when_running_sets_flag_and_cancelling_status(self):
        with app_mod.state_lock:
            app_mod.state.dup_status = "running"
            app_mod.active_dup_scanner_cancelled = False

        resp = self.client.post("/api/dup_scan_cancel", headers=self.headers)
        self.assertEqual(resp.status_code, 200)

        with app_mod.state_lock:
            self.assertEqual(app_mod.state.dup_status, "cancelling")
            self.assertTrue(app_mod.active_dup_scanner_cancelled)


if __name__ == "__main__":
    unittest.main()

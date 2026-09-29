import os
import tempfile
import unittest

import src.app as app_mod


class Pass4DupEndpointsStateAndPathTests(unittest.TestCase):
    """Regression tests for P4-06: /api/dup_scan_start and /api/dup_empty_trash validation and concurrency."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = self.tmp.name
        self.regular_file = os.path.join(self.root, "not_a_folder.txt")
        with open(self.regular_file, "wb") as f:
            f.write(b"hello")

        self.trash_dir = os.path.join(self.root, ".Duplicates_Trash")
        os.makedirs(self.trash_dir)
        self.trashed_file = os.path.join(self.trash_dir, "dup.txt")
        with open(self.trashed_file, "wb") as f:
            f.write(b"trashed duplicate")

        self.client = app_mod.app.test_client()
        self.headers = {"X-Organizer-Token": app_mod.API_TOKEN}

        with app_mod.state_lock:
            self.saved_status = app_mod.state.status
            self.saved_dup_status = app_mod.state.dup_status
            app_mod.state.status = "idle"
            app_mod.state.dup_status = "idle"

    def tearDown(self):
        with app_mod.state_lock:
            app_mod.state.status = self.saved_status
            app_mod.state.dup_status = self.saved_dup_status
        self.tmp.cleanup()

    def test_dup_scan_start_rejects_int_fd_without_wedging_dup_status(self):
        """Passing {"folder": 1} (integer file descriptor) must return 400 and leave dup_status == 'idle'."""
        resp = self.client.post(
            "/api/dup_scan_start",
            json={"folder": 1},
            headers=self.headers,
        )
        self.assertEqual(resp.status_code, 400)
        self.assertFalse(resp.get_json()["success"])
        with app_mod.state_lock:
            self.assertEqual(app_mod.state.dup_status, "idle")

    def test_dup_scan_start_rejects_regular_file_path(self):
        """Passing a regular file path to /api/dup_scan_start must return 400 and not start a scan."""
        resp = self.client.post(
            "/api/dup_scan_start",
            json={"folder": self.regular_file},
            headers=self.headers,
        )
        self.assertEqual(resp.status_code, 400)
        self.assertFalse(resp.get_json()["success"])
        with app_mod.state_lock:
            self.assertEqual(app_mod.state.dup_status, "idle")

    def test_dup_empty_trash_refuses_while_transfer_or_scan_active(self):
        """/api/dup_empty_trash must return 409 while a transfer or duplicate scan is running or cancelling."""
        for status, dup_status in (
            ("running", "idle"),
            ("cancelling", "idle"),
            ("idle", "running"),
            ("idle", "cancelling"),
        ):
            with app_mod.state_lock:
                app_mod.state.status = status
                app_mod.state.dup_status = dup_status

            resp = self.client.post(
                "/api/dup_empty_trash",
                json={"root_folder": self.root},
                headers=self.headers,
            )
            self.assertEqual(resp.status_code, 409)
            self.assertFalse(resp.get_json()["success"])
            self.assertTrue(os.path.exists(self.trashed_file))

    def test_dup_empty_trash_rejects_non_directory_paths(self):
        """/api/dup_empty_trash must return 400 for integer FDs or regular file paths."""
        for bad_root in (1, self.regular_file, "   "):
            resp = self.client.post(
                "/api/dup_empty_trash",
                json={"root_folder": bad_root},
                headers=self.headers,
            )
            self.assertEqual(resp.status_code, 400)
            self.assertFalse(resp.get_json()["success"])
            self.assertTrue(os.path.exists(self.trashed_file))


if __name__ == "__main__":
    unittest.main()

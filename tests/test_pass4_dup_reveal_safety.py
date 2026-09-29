import os
import tempfile
import unittest
from unittest.mock import patch

import src.app as app_mod


class Pass4DupRevealSafetyTests(unittest.TestCase):
    """Regression tests for P4-04: /api/dup_reveal path scoping and `--` option terminator."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = self.tmp.name
        self.scan_dir = os.path.join(self.root, "scanned_drive")
        self.outside_dir = os.path.join(self.root, "outside")
        os.makedirs(self.scan_dir)
        os.makedirs(self.outside_dir)

        self.inside_file = os.path.join(self.scan_dir, "-aCalculator.jpg")
        with open(self.inside_file, "wb") as f:
            f.write(b"jpegdata")

        self.outside_file = os.path.join(self.outside_dir, "secret.txt")
        with open(self.outside_file, "wb") as f:
            f.write(b"secret")

        self.client = app_mod.app.test_client()
        self.headers = {"X-Organizer-Token": app_mod.API_TOKEN}

        with app_mod.state_lock:
            self.saved_dup_root = app_mod.state.dup_root
            app_mod.state.dup_root = ""

    def tearDown(self):
        with app_mod.state_lock:
            app_mod.state.dup_root = self.saved_dup_root
        self.tmp.cleanup()

    def test_dup_reveal_refuses_when_no_scan_has_run(self):
        with patch("src.app.subprocess.Popen") as mock_popen:
            resp = self.client.post(
                "/api/dup_reveal",
                json={"path": self.inside_file},
                headers=self.headers,
            )
            self.assertEqual(resp.status_code, 403)
            self.assertFalse(resp.get_json()["success"])
            mock_popen.assert_not_called()

    def test_dup_reveal_refuses_path_outside_dup_root_or_symlink_escape(self):
        with app_mod.state_lock:
            app_mod.state.dup_root = os.path.realpath(self.scan_dir)

        symlink_escape = os.path.join(self.scan_dir, "escape.txt")
        os.symlink(self.outside_file, symlink_escape)

        with patch("src.app.subprocess.Popen") as mock_popen:
            resp1 = self.client.post(
                "/api/dup_reveal",
                json={"path": self.outside_file},
                headers=self.headers,
            )
            self.assertEqual(resp1.status_code, 403)
            self.assertFalse(resp1.get_json()["success"])

            resp2 = self.client.post(
                "/api/dup_reveal",
                json={"path": symlink_escape},
                headers=self.headers,
            )
            self.assertEqual(resp2.status_code, 403)
            self.assertFalse(resp2.get_json()["success"])
            mock_popen.assert_not_called()

    def test_dup_reveal_passes_double_dash_and_resolved_path(self):
        with app_mod.state_lock:
            app_mod.state.dup_root = os.path.realpath(self.scan_dir)

        with patch("src.app.subprocess.Popen") as mock_popen:
            resp = self.client.post(
                "/api/dup_reveal",
                json={"path": self.inside_file},
                headers=self.headers,
            )
            self.assertEqual(resp.status_code, 200)
            self.assertTrue(resp.get_json()["success"])
            mock_popen.assert_called_once_with(
                ["open", "-R", "--", os.path.realpath(self.inside_file)]
            )


if __name__ == "__main__":
    unittest.main()

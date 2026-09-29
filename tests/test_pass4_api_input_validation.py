import unittest
from unittest.mock import patch

import src.app as app_mod


class Pass4ApiInputValidationTests(unittest.TestCase):
    """Regression tests for P4-07: Flask API JSON/query/token input validation."""

    def setUp(self):
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

    def test_require_token_rejects_non_ascii_token_with_403_not_500(self):
        resp = self.client.get("/api/status?token=%C3%A9")
        self.assertEqual(resp.status_code, 403)
        self.assertFalse(resp.get_json()["success"])

    def test_post_endpoints_reject_non_dict_json_without_500(self):
        endpoints = (
            "/api/start",
            "/api/dissolve_project",
            "/api/trash_duplicates",
            "/api/restore_duplicates",
            "/api/repair_transfer",
            "/api/dup_scan_start",
            "/api/dup_reveal",
            "/api/dup_trash_inplace",
            "/api/dup_empty_trash",
        )
        for ep in endpoints:
            for payload in (["not", "a", "dict"], "string_payload", 42):
                resp = self.client.post(ep, json=payload, headers=self.headers)
                self.assertNotEqual(
                    resp.status_code,
                    500,
                    f"{ep} returned 500 for payload {payload!r}",
                )
                self.assertIn(resp.status_code, (200, 400, 403, 404))

    def test_post_endpoints_reject_non_string_paths_without_500(self):
        cases = (
            ("/api/start", {"source": [123], "dest": "/tmp/dest"}),
            ("/api/start", {"source": "/tmp/src", "dest": 123}),
            ("/api/dissolve_project", {"dest": 123, "project_path": 456}),
            ("/api/trash_duplicates", {"dest": 123, "source_paths": [456]}),
            ("/api/restore_duplicates", {"dest": 123, "source_paths": [456]}),
            ("/api/repair_transfer", {"dest": 1}),
            ("/api/dup_trash_inplace", {"root_folder": 123, "source_paths": [456]}),
        )
        for ep, payload in cases:
            resp = self.client.post(ep, json=payload, headers=self.headers)
            self.assertNotEqual(
                resp.status_code,
                500,
                f"{ep} returned 500 for payload {payload!r}",
            )
            self.assertIn(resp.status_code, (200, 400))
            self.assertFalse(resp.get_json()["success"])

    def test_open_finder_rejects_whitespace_only_path_without_opening_cwd(self):
        with patch("src.app.subprocess.run") as mock_run:
            resp = self.client.get("/api/open_finder?path=%20%20%20", headers=self.headers)
            self.assertEqual(resp.status_code, 400)
            self.assertFalse(resp.get_json()["success"])
            mock_run.assert_not_called()


if __name__ == "__main__":
    unittest.main()

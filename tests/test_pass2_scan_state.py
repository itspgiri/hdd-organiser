"""P2-06: duplicates results could belong to a different folder than the UI thinks.

1. `/api/dup_scan_start` only refused while the status was "running". After
   a cancel the status is "cancelling" until the old scan notices, which can
   take minutes inside a large file comparison. A new scan could start then,
   and starting it reset the shared cancel flag, so the old scan carried on
   and later published its results over the new scan's.
2. `/api/dup_trash_inplace` used whatever `root_folder` the UI sent (the
   editable path field) for `.Duplicates_Trash`, without checking that the
   results were for that folder.
"""

import os
import threading
import time
import unittest
from unittest import mock

from pass2_helpers import (
    OrganizerAPI, Pass2Case, api_client, payload, wait_for_dup_scan, write_file,
)


class ScanStateTest(Pass2Case):

    def test_cancelled_scan_cannot_replace_a_later_scans_results(self):
        folder_a = os.path.join(self.root, "A")
        folder_b = os.path.join(self.root, "B")
        os.makedirs(folder_a)
        os.makedirs(folder_b)
        groups_a = [{"size": 1, "files": [os.path.join(folder_a, "x"), os.path.join(folder_a, "y")]}]
        groups_b = [{"size": 1, "files": [os.path.join(folder_b, "x"), os.path.join(folder_b, "y")]}]
        a_started, release_a, a_returned = threading.Event(), threading.Event(), threading.Event()

        def fake_scan(api, folder):
            if folder != folder_a:
                return groups_b
            a_started.set()
            # Stuck in one long file comparison: no progress callbacks, so
            # no cancel check, until it finishes.
            release_a.wait(10)
            api._emit_progress(1, 1, "Phase 2/2: Verifying duplicates...")
            a_returned.set()
            return [] if api.cancelled else groups_a

        client, headers = api_client()
        with mock.patch.object(OrganizerAPI, "find_duplicates_inplace", fake_scan):
            self.assertEqual(client.post("/api/dup_scan_start", json={"folder": folder_a},
                                         headers=headers).status_code, 200)
            self.assertTrue(a_started.wait(5))
            client.post("/api/dup_scan_cancel", headers=headers)

            res = client.post("/api/dup_scan_start", json={"folder": folder_b}, headers=headers)
            b_accepted = bool(res.get_json().get("success"))
            if b_accepted:
                wait_for_dup_scan(client, headers)

            release_a.set()
            self.assertTrue(a_returned.wait(5))
            time.sleep(0.2)
            wait_for_dup_scan(client, headers)

            if not b_accepted:
                res = client.post("/api/dup_scan_start", json={"folder": folder_b},
                                  headers=headers)
                self.assertTrue(res.get_json().get("success"), res.get_json())
                wait_for_dup_scan(client, headers)

        final = client.get("/api/dup_scan_status?include_results=1", headers=headers).get_json()
        self.assertEqual(final["status"], "complete")
        self.assertEqual(final.get("results"), groups_b,
                         "the cancelled scan of A published its results over B's")

    def test_quarantine_refuses_a_folder_other_than_the_scanned_one(self):
        data = payload("p2-06 root", 4096)
        keep = write_file(self.path("x.bin"), data)
        dup = write_file(self.path("x copy.bin"), data)
        elsewhere = os.path.join(self.sandbox, "other-folder")
        os.makedirs(elsewhere)
        client, headers = api_client()
        client.post("/api/dup_scan_start", json={"folder": self.root}, headers=headers)
        self.assertEqual(wait_for_dup_scan(client, headers)["group_count"], 1)

        res = client.post("/api/dup_trash_inplace", headers=headers, json={
            "source_paths": [dup], "root_folder": elsewhere, "permanent_delete": False})

        self.assertTrue(os.path.exists(dup), f"moved away: {res.get_json()}")
        self.assertTrue(os.path.exists(keep))
        self.assertFalse(os.path.exists(os.path.join(elsewhere, ".Duplicates_Trash")))
        self.assertNotEqual(res.get_json().get("count"), 1)

    def test_quarantine_into_the_scanned_folder_still_works(self):
        data = payload("p2-06 control", 4096)
        keep = write_file(self.path("x.bin"), data)
        dup = write_file(self.path("x copy.bin"), data)
        client, headers = api_client()
        client.post("/api/dup_scan_start", json={"folder": self.root}, headers=headers)
        wait_for_dup_scan(client, headers)

        res = client.post("/api/dup_trash_inplace", headers=headers, json={
            "source_paths": [dup], "root_folder": self.root + os.sep,
            "permanent_delete": False}).get_json()

        self.assertEqual((res["count"], res["refused"]), (1, []))
        self.assertTrue(os.path.exists(keep))
        self.assertTrue(os.path.exists(self.path(".Duplicates_Trash", "x copy.bin")))


if __name__ == "__main__":
    unittest.main()

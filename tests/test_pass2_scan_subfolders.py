"""P2-01: the in-place duplicate scan crashed on any folder with a subfolder.

`find_duplicates_inplace` asked `self.categorizer` whether each subfolder is
a code project, but `OrganizerAPI` never sets that attribute. The first
subfolder raised AttributeError, so the scan never completed on a real drive.

These tests deliberately build the API the way the app does (no
`make_api()` workaround).
"""

import os
import unittest

from pass2_helpers import (
    CONFIG_PATH, OrganizerAPI, Pass2Case, api_client, payload,
    wait_for_dup_scan, write_file,
)


class ScanWithSubfoldersTest(Pass2Case):

    def plain_api(self):
        return OrganizerAPI(CONFIG_PATH, self.logs.append, lambda *a, **k: None)

    def test_scan_finds_duplicates_in_subfolders(self):
        data = payload("p2-01 photo", 4096)
        a = write_file(self.path("Photos", "2023", "IMG_0001.JPG"), data)
        b = write_file(self.path("Backup", "IMG_0001.JPG"), data)

        groups = self.plain_api().find_duplicates_inplace(self.root)

        self.assertEqual(len(groups), 1)
        self.assertEqual(sorted(groups[0]["files"]), sorted([a, b]))

    def test_scan_still_skips_code_projects(self):
        data = payload("p2-01 module", 2048)
        write_file(self.path("MyApp", "package.json"), b'{"name": "myapp"}\n')
        write_file(self.path("MyApp", "src", "util.js"), data)
        write_file(self.path("Notes", "util.js"), data)
        a = write_file(self.path("Docs", "a.txt"), payload("p2-01 doc", 100))
        b = write_file(self.path("Docs", "old", "a.txt"), payload("p2-01 doc", 100))

        groups = self.plain_api().find_duplicates_inplace(self.root)

        # The project's copy is never offered, so util.js has no group.
        self.assertEqual([sorted(g["files"]) for g in groups], [sorted([a, b])])

    def test_scan_endpoint_completes(self):
        data = payload("p2-01 endpoint", 4096)
        write_file(self.path("A", "clip.mov"), data)
        write_file(self.path("B", "clip.mov"), data)
        client, headers = api_client()

        res = client.post("/api/dup_scan_start", json={"folder": self.root},
                          headers=headers)
        self.assertEqual(res.status_code, 200)
        status = wait_for_dup_scan(client, headers)

        self.assertEqual(status["status"], "complete", status["message"])
        self.assertEqual(status["group_count"], 1)


if __name__ == "__main__":
    unittest.main()

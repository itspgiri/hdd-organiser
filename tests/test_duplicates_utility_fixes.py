"""Regression tests for the Duplicates Utility full audit fixes."""

import os
import shutil
import stat
import tempfile
import threading
import unittest
from unittest import mock

import src.app as webapp
from src.api_organizer import OrganizerAPI
from src.file_ops import _clear_immutable, files_are_identical

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CONFIG_PATH = os.path.join(REPO_ROOT, "src", "config.json")


class DuplicatesUtilityFixesTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="dup_fixes_")
        self.addCleanup(self._cleanup_tmp)
        self.api = OrganizerAPI(CONFIG_PATH)
        self.client = webapp.app.test_client()
        self.headers = {"X-Organizer-Token": webapp.API_TOKEN}

        with webapp.state_lock:
            self._saved_state = (
                webapp.state.status,
                webapp.state.dup_status,
                list(webapp.state.dup_results or []),
                webapp.state.dup_root,
                webapp.state.dup_scan_id,
            )
            webapp.state.status = "idle"
            webapp.state.dup_status = "idle"
            webapp.state.dup_results = []
            webapp.state.dup_root = ""

    def _cleanup_tmp(self):
        for root, dirs, files in os.walk(self.tmp):
            for d in dirs:
                dp = os.path.join(root, d)
                if not os.path.islink(dp):
                    try:
                        os.chmod(dp, 0o700)
                    except OSError:
                        pass
            for f in files:
                fp = os.path.join(root, f)
                if not os.path.islink(fp):
                    _clear_immutable(fp)
                    try:
                        os.chmod(fp, 0o600)
                    except OSError:
                        pass
        shutil.rmtree(self.tmp, ignore_errors=True)

    def tearDown(self):
        with webapp.state_lock:
            (
                webapp.state.status,
                webapp.state.dup_status,
                webapp.state.dup_results,
                webapp.state.dup_root,
                webapp.state.dup_scan_id,
            ) = self._saved_state

    def test_tilde_expansion_in_dup_trash_inplace_and_empty_trash(self):
        """~ paths in root_folder and source_paths expand to HOME instead of creating literal ./~ directories."""
        fake_home = os.path.join(self.tmp, "home_user")
        scan_dir = os.path.join(fake_home, "dups")
        os.makedirs(scan_dir)
        f1 = os.path.join(scan_dir, "keep.txt")
        f2 = os.path.join(scan_dir, "keep copy.txt")
        for fp in (f1, f2):
            with open(fp, "wb") as fh:
                fh.write(b"tilde duplicate payload " * 100)

        with mock.patch.dict(os.environ, {"HOME": fake_home}):
            r_start = self.client.post(
                "/api/dup_scan_start",
                headers=self.headers,
                json={"folder": "~/dups"},
            )
            self.assertEqual(r_start.status_code, 200)
            for _ in range(100):
                st = self.client.get("/api/dup_scan_status", headers=self.headers).get_json()
                if st["status"] == "complete":
                    break
                threading.Event().wait(0.02)
            self.assertEqual(st["status"], "complete")

            r_trash = self.client.post(
                "/api/dup_trash_inplace",
                headers=self.headers,
                json={
                    "source_paths": ["~/dups/keep copy.txt"],
                    "root_folder": "~/dups",
                    "permanent_delete": False,
                },
            )
            self.assertEqual(r_trash.status_code, 200)
            trash_json = r_trash.get_json()
            self.assertEqual(trash_json["count"], 1)
            self.assertEqual(trash_json["refused"], [])
            self.assertTrue(os.path.exists(os.path.join(scan_dir, ".Duplicates_Trash", "keep copy.txt")))
            self.assertFalse(os.path.exists(os.path.join(os.getcwd(), "~")))

            r_empty = self.client.post(
                "/api/dup_empty_trash",
                headers=self.headers,
                json={"root_folder": "~/dups"},
            )
            self.assertEqual(r_empty.status_code, 200)
            self.assertEqual(r_empty.get_json()["files_deleted"], 1)
            self.assertFalse(os.path.exists(os.path.join(scan_dir, ".Duplicates_Trash")))

    def test_trash_inplace_duplicates_refuses_when_groups_is_none_or_empty(self):
        """Calling trash_inplace_duplicates with groups=None or groups=[] must never delete files without twin verification."""
        scan_dir = os.path.join(self.tmp, "no_groups")
        os.makedirs(scan_dir)
        f1 = os.path.join(scan_dir, "a.txt")
        f2 = os.path.join(scan_dir, "b.txt")
        with open(f1, "wb") as fh:
            fh.write(b"unique content 1")
        with open(f2, "wb") as fh:
            fh.write(b"unique content 2")

        for grp_arg in (None, []):
            removed, refused, reclaimed = self.api.trash_inplace_duplicates(
                [f1, f2], scan_dir, permanent_delete=True, groups=grp_arg
            )
            self.assertEqual(removed, 0)
            self.assertEqual(reclaimed, 0)
            self.assertEqual(len(refused), 2)
            self.assertTrue(os.path.exists(f1))
            self.assertTrue(os.path.exists(f2))

    def test_trash_inplace_duplicates_refuses_files_outside_root_folder(self):
        """Files outside root_folder must be refused even if passed in a forged groups structure."""
        scan_dir = os.path.join(self.tmp, "inside")
        outside_dir = os.path.join(self.tmp, "outside")
        os.makedirs(scan_dir)
        os.makedirs(outside_dir)
        inside_keep = os.path.join(scan_dir, "doc.txt")
        outside_dup = os.path.join(outside_dir, "doc_copy.txt")
        for fp in (inside_keep, outside_dup):
            with open(fp, "wb") as fh:
                fh.write(b"same bytes across folders")

        forged_groups = [{"size": 25, "files": [inside_keep, outside_dup]}]
        removed, refused, _ = self.api.trash_inplace_duplicates(
            [outside_dup], scan_dir, permanent_delete=True, groups=forged_groups
        )
        self.assertEqual(removed, 0)
        self.assertEqual(len(refused), 1)
        self.assertIn("outside the scanned folder", refused[0])
        self.assertTrue(os.path.exists(outside_dup))

    def test_realpath_and_symlink_spelling_matches_in_trash_inplace_duplicates(self):
        """Paths differing by symlinked parent directory spelling still match between source_paths and groups."""
        real_dir = os.path.join(self.tmp, "real_scan")
        os.makedirs(real_dir)
        alias_dir = os.path.join(self.tmp, "alias_scan")
        os.symlink(real_dir, alias_dir)

        f_keep = os.path.join(real_dir, "item.bin")
        f_dup = os.path.join(real_dir, "item copy.bin")
        for fp in (f_keep, f_dup):
            with open(fp, "wb") as fh:
                fh.write(b"symlink spelling test" * 50)

        groups = [{"size": os.path.getsize(f_keep), "files": [os.path.realpath(f_keep), os.path.realpath(f_dup)]}]
        alias_dup = os.path.join(alias_dir, "item copy.bin")

        removed, refused, _ = self.api.trash_inplace_duplicates(
            [alias_dup], real_dir, permanent_delete=True, groups=groups
        )
        self.assertEqual(removed, 1)
        self.assertEqual(refused, [])
        self.assertTrue(os.path.exists(f_keep))
        self.assertFalse(os.path.exists(f_dup))

    def test_camera_filename_not_penalized_as_copy_and_numbered_copy_detected(self):
        """IMG_1234.JPG is kept as original over IMG_1234 copy.JPG even when deeper, and report.pdf beats report_1.pdf."""
        scan_dir = os.path.join(self.tmp, "sort_test")
        deep_cam = os.path.join(scan_dir, "2023", "Vacation", "IMG_1234.JPG")
        shallow_copy = os.path.join(scan_dir, "IMG_1234 copy.JPG")
        os.makedirs(os.path.dirname(deep_cam))
        for fp in (deep_cam, shallow_copy):
            with open(fp, "wb") as fh:
                fh.write(b"camera bytes " * 200)

        rep_orig = os.path.join(scan_dir, "sub", "report.pdf")
        rep_num = os.path.join(scan_dir, "report_1.pdf")
        os.makedirs(os.path.dirname(rep_orig))
        for fp in (rep_orig, rep_num):
            with open(fp, "wb") as fh:
                fh.write(b"pdf report bytes " * 100)

        groups = self.api.find_duplicates_inplace(scan_dir)
        self.assertEqual(len(groups), 2)
        by_first = {os.path.basename(g["files"][0]): g["files"] for g in groups}
        self.assertIn("IMG_1234.JPG", by_first)
        self.assertIn("report.pdf", by_first)

    def test_post_transfer_trash_duplicates_symlink_and_locked_file_safety(self):
        """Post-transfer trash_duplicates refuses symlinked .Duplicates_Trash and locked files without copy or Finder delete."""
        src = os.path.join(self.tmp, "post_src")
        dest = os.path.join(self.tmp, "post_dest")
        elsewhere = os.path.join(self.tmp, "elsewhere")
        os.makedirs(src)
        os.makedirs(elsewhere)

        f1 = os.path.join(src, "doc.txt")
        f2 = os.path.join(src, "doc copy.txt")
        for fp in (f1, f2):
            with open(fp, "wb") as fh:
                fh.write(b"post-transfer duplicate content " * 50)

        self.api.run(src, dest, is_preview=False, dest_mode="new")
        dups = self.api.get_duplicate_records(dest)
        self.assertEqual(len(dups), 1)
        dup_src = dups[0]["source_path"]

        # 1. Symlinked .Duplicates_Trash must be refused
        trash_link = os.path.join(src, ".Duplicates_Trash")
        os.symlink(elsewhere, trash_link)
        count_sym, refused_sym = self.api.trash_duplicates([dup_src], dest_abs=dest)
        self.assertEqual(count_sym, 0)
        self.assertEqual(len(refused_sym), 1)
        self.assertIn("symbolic link", refused_sym[0])
        self.assertTrue(os.path.exists(dup_src))
        self.assertEqual(os.listdir(elsewhere), [])
        os.unlink(trash_link)

        # 2. Locked (UF_IMMUTABLE) file must fail atomically via os.rename without leaving a copy in .Duplicates_Trash
        os.chflags(dup_src, stat.UF_IMMUTABLE)
        try:
            with mock.patch("subprocess.run") as mock_subproc:
                count_lock, refused_lock = self.api.trash_duplicates([dup_src], dest_abs=dest)
                mock_subproc.assert_not_called()
            self.assertEqual(count_lock, 0)
            self.assertEqual(len(refused_lock), 1)
            self.assertTrue(os.path.exists(dup_src))
            trash_contents = os.listdir(trash_link) if os.path.isdir(trash_link) else []
            self.assertEqual(trash_contents, [])
        finally:
            _clear_immutable(dup_src)

    def test_boolean_string_parsing_and_running_transfer_guard_in_dup_trash_inplace(self):
        """String 'false' is not coerced to True, and dup_trash_inplace returns 409 while a transfer is running."""
        scan_dir = os.path.join(self.tmp, "bool_test")
        os.makedirs(scan_dir)
        f1 = os.path.join(scan_dir, "a.txt")
        f2 = os.path.join(scan_dir, "a copy.txt")
        f3 = os.path.join(scan_dir, "a copy 2.txt")
        for fp in (f1, f2, f3):
            with open(fp, "wb") as fh:
                fh.write(b"boolean test bytes " * 50)

        self.client.post("/api/dup_scan_start", headers=self.headers, json={"folder": scan_dir})
        for _ in range(100):
            if self.client.get("/api/dup_scan_status", headers=self.headers).get_json()["status"] == "complete":
                break
            threading.Event().wait(0.02)

        # 1. Blocked with 409 while main organizer transfer is running
        with webapp.state_lock:
            webapp.state.status = "running"
        try:
            r_busy = self.client.post(
                "/api/dup_trash_inplace",
                headers=self.headers,
                json={"source_paths": [f2], "root_folder": scan_dir},
            )
            self.assertEqual(r_busy.status_code, 409)
        finally:
            with webapp.state_lock:
                webapp.state.status = "idle"

        # 2. String "false" for permanent_delete and delete_all_redundant is parsed as False
        r_ok = self.client.post(
            "/api/dup_trash_inplace",
            headers=self.headers,
            json={
                "source_paths": [f2],
                "root_folder": scan_dir,
                "permanent_delete": "false",
                "delete_all_redundant": "false",
            },
        )
        self.assertEqual(r_ok.status_code, 200)
        body = r_ok.get_json()
        self.assertFalse(body["permanent_delete"])
        self.assertEqual(body["count"], 1)
        # f2 was quarantined (not permanently deleted), and f3 was NOT deleted because delete_all_redundant was "false"
        self.assertTrue(os.path.exists(os.path.join(scan_dir, ".Duplicates_Trash", "a copy.txt")))
        self.assertTrue(os.path.exists(f3))
        self.assertTrue(os.path.exists(f1))

    def test_files_are_identical_and_phase2_respect_immediate_cancel(self):
        """files_are_identical aborts when cancel_check returns True, and dup_scan_cancel immediately cancels active_dup_api_instance."""
        f1 = os.path.join(self.tmp, "c1.bin")
        f2 = os.path.join(self.tmp, "c2.bin")
        for fp in (f1, f2):
            with open(fp, "wb") as fh:
                fh.write(b"x" * (256 * 1024))

        self.assertTrue(files_are_identical(f1, f2))
        self.assertFalse(files_are_identical(f1, f2, cancel_check=lambda: True))

    def test_post_transfer_tilde_dest_and_dup_scan_concurrency_guard(self):
        """Post-transfer /api/duplicates, /api/trash_duplicates, /api/restore_duplicates, /api/verify_transfer, and /api/repair_transfer expand ~ in dest and block during active dup_scan."""
        fake_home = os.path.join(self.tmp, "tilde_home")
        src = os.path.join(fake_home, "src")
        dest = os.path.join(fake_home, "dest")
        os.makedirs(src)

        f1 = os.path.join(src, "note.txt")
        f2 = os.path.join(src, "note copy.txt")
        for fp in (f1, f2):
            with open(fp, "wb") as fh:
                fh.write(b"post-transfer tilde test " * 40)

        with mock.patch.dict(os.environ, {"HOME": fake_home}):
            self.api.run("~/src", "~/dest", is_preview=False, dest_mode="new")

            # 1. /api/duplicates?dest=~/dest finds the duplicate instead of returning empty list
            r_dups = self.client.get("/api/duplicates?dest=~/dest", headers=self.headers)
            self.assertEqual(r_dups.status_code, 200)
            dups_data = r_dups.get_json()
            self.assertEqual(len(dups_data["duplicates"]), 1)
            dup_src = dups_data["duplicates"][0]["source_path"]

            # 2. Active standalone dup scan blocks /api/trash_duplicates and /api/restore_duplicates with 409
            with webapp.state_lock:
                webapp.state.dup_status = "running"
            try:
                r_busy_trash = self.client.post(
                    "/api/trash_duplicates",
                    headers=self.headers,
                    json={"dest": "~/dest", "source_paths": [dup_src]},
                )
                self.assertEqual(r_busy_trash.status_code, 409)
                r_busy_restore = self.client.post(
                    "/api/restore_duplicates",
                    headers=self.headers,
                    json={"dest": "~/dest", "source_paths": [dup_src]},
                )
                self.assertEqual(r_busy_restore.status_code, 409)
            finally:
                with webapp.state_lock:
                    webapp.state.dup_status = "idle"

            # 3. /api/trash_duplicates and /api/restore_duplicates work with ~/dest
            r_trash = self.client.post(
                "/api/trash_duplicates",
                headers=self.headers,
                json={"dest": "~/dest", "source_paths": [dup_src]},
            )
            self.assertEqual(r_trash.status_code, 200)
            self.assertEqual(r_trash.get_json()["count"], 1)

            r_restore = self.client.post(
                "/api/restore_duplicates",
                headers=self.headers,
                json={"dest": "~/dest", "source_paths": [dup_src]},
            )
            self.assertEqual(r_restore.status_code, 200)
            self.assertEqual(r_restore.get_json()["count"], 1)

            # 4. /api/verify_transfer and /api/repair_transfer work with ~/dest
            r_ver = self.client.get("/api/verify_transfer?dest=~/dest", headers=self.headers)
            self.assertEqual(r_ver.status_code, 200)
            self.assertTrue(r_ver.get_json()["success"])

            r_rep = self.client.post("/api/repair_transfer", headers=self.headers, json={"dest": "~/dest"})
            self.assertEqual(r_rep.status_code, 200)
            self.assertTrue(r_rep.get_json()["success"])

    def test_broken_symlinks_in_trash_and_restore_and_symlink_dir_in_empty_trash(self):
        """Broken symlinks are not overwritten during quarantine or restore, and symlinked dirs inside .Duplicates_Trash are unlinked cleanly."""
        scan_dir = os.path.join(self.tmp, "sym_edge")
        os.makedirs(scan_dir)
        f1 = os.path.join(scan_dir, "keep.txt")
        f2 = os.path.join(scan_dir, "dup.txt")
        for fp in (f1, f2):
            with open(fp, "wb") as fh:
                fh.write(b"symlink collision edge case " * 30)

        # Pre-populate .Duplicates_Trash/dup.txt as a broken symlink
        trash_dir = os.path.join(scan_dir, ".Duplicates_Trash")
        os.makedirs(trash_dir)
        broken_in_trash = os.path.join(trash_dir, "dup.txt")
        os.symlink("/nonexistent/target/path", broken_in_trash)

        groups = [{"size": os.path.getsize(f1), "files": [f1, f2]}]
        removed, refused, _ = self.api.trash_inplace_duplicates(
            [f2], scan_dir, permanent_delete=False, groups=groups
        )
        self.assertEqual(removed, 1)
        self.assertEqual(refused, [])
        # Both the broken symlink and the renamed dup_1.txt exist
        self.assertTrue(os.path.islink(broken_in_trash))
        self.assertTrue(os.path.exists(os.path.join(trash_dir, "dup_1.txt")))

        # Put a symlink to an external directory inside .Duplicates_Trash and verify empty_duplicates_trash unlinks it cleanly
        ext_dir = os.path.join(self.tmp, "external_dir")
        os.makedirs(ext_dir)
        ext_file = os.path.join(ext_dir, "precious.txt")
        with open(ext_file, "wb") as fh:
            fh.write(b"do not delete me")
        os.symlink(ext_dir, os.path.join(trash_dir, "linked_subdir"))

        files_del, _, err = self.api.empty_duplicates_trash(scan_dir)
        self.assertEqual(err, "")
        self.assertGreaterEqual(files_del, 2)
        self.assertFalse(os.path.lexists(trash_dir))
        self.assertTrue(os.path.exists(ext_file))

    def test_overlapping_groups_keep_survivor_and_repair_restores_trashed_duplicate(self):
        """Overlapping groups cannot trick trash_inplace_duplicates into deleting all copies, and repair_transfer restores a trashed duplicate if its destination twin was lost."""
        scan_dir = os.path.join(self.tmp, "overlap")
        os.makedirs(scan_dir)
        f1 = os.path.join(scan_dir, "a.txt")
        f2 = os.path.join(scan_dir, "b.txt")
        for fp in (f1, f2):
            with open(fp, "wb") as fh:
                fh.write(b"overlapping groups payload " * 40)

        malformed_groups = [
            {"size": os.path.getsize(f1), "files": [f1, f2]},
            {"size": os.path.getsize(f1), "files": [f2, f1]},
        ]
        removed, refused, _ = self.api.trash_inplace_duplicates(
            [f1, f2], scan_dir, permanent_delete=True, groups=malformed_groups
        )
        self.assertEqual(removed, 1)
        self.assertEqual(len(refused), 1)
        self.assertTrue(os.path.exists(f1) or os.path.exists(f2))

        # Now test repair_transfer recovering a trashed duplicate when the destination copy is lost
        src = os.path.join(self.tmp, "rep_src")
        dest = os.path.join(self.tmp, "rep_dest")
        os.makedirs(src)
        s1 = os.path.join(src, "doc.txt")
        s2 = os.path.join(src, "doc copy.txt")
        for fp in (s1, s2):
            with open(fp, "wb") as fh:
                fh.write(b"recoverable trashed duplicate " * 40)

        self.api.run(src, dest, is_preview=False, dest_mode="new")
        dups = self.api.get_duplicate_records(dest)
        self.assertEqual(len(dups), 1)
        dup_src = dups[0]["source_path"]
        dest_copy = dups[0]["duplicate_of"]
        primary_src = s1 if dup_src == s2 else s2

        # Quarantine dup_src into .Duplicates_Trash
        t_count, t_ref = self.api.trash_duplicates([dup_src], dest_abs=dest)
        self.assertEqual(t_count, 1)
        self.assertEqual(t_ref, [])
        self.assertFalse(os.path.exists(dup_src))

        # Simulate disaster: both the destination copy AND primary_src are lost!
        os.remove(dest_copy)
        os.remove(primary_src)

        # repair_transfer should automatically restore dup_src from .Duplicates_Trash and purge its DB record
        rep = self.api.repair_transfer(dest)
        self.assertTrue(rep["success"])
        self.assertTrue(os.path.exists(dup_src))

        # Re-running the transfer now re-copies dup_src to the destination!
        self.api.run(src, dest, is_preview=False, dest_mode="merge")
        ver = self.api.verify_transfer(dest)
        self.assertTrue(ver["is_perfect"])
        self.assertEqual(ver["verified_count"], 1)

    def test_multi_tier_parallel_scanner_and_macos_library_skip(self):
        """3-tier parallel scanner distinguishes >2MB files differing outside sample windows and skips macOS ~/Library unless targeted directly."""
        scan_dir = os.path.join(self.tmp, "tier_scan")
        os.makedirs(scan_dir)

        # 1. Small duplicate pair (<= 8 KB) handled directly via Tier 1 + Tier 3
        s_a = os.path.join(scan_dir, "small.txt")
        s_b = os.path.join(scan_dir, "small copy.txt")
        for fp in (s_a, s_b):
            with open(fp, "wb") as fh:
                fh.write(b"small duplicate " * 100)

        # 2. Three > 2 MB files that share first 1 MB, middle 64 KB, and last 1 MB,
        # where two are identical and the third differs at offset 1.2 MB
        big_base = bytearray(b"A" * (3 * 1024 * 1024))
        b1 = os.path.join(scan_dir, "movie.bin")
        b2 = os.path.join(scan_dir, "movie copy.bin")
        b3_diff = os.path.join(scan_dir, "movie_edited.bin")
        for fp in (b1, b2):
            with open(fp, "wb") as fh:
                fh.write(big_base)
        big_diff = bytearray(big_base)
        big_diff[1200 * 1024] = ord("Z")
        with open(b3_diff, "wb") as fh:
            fh.write(big_diff)

        # 3. Simulated macOS user Library folder (with Application Support & Caches)
        lib_dir = os.path.join(scan_dir, "Library")
        os.makedirs(os.path.join(lib_dir, "Application Support"))
        os.makedirs(os.path.join(lib_dir, "Caches"))
        lib_dup = os.path.join(lib_dir, "small_in_lib.txt")
        with open(lib_dup, "wb") as fh:
            fh.write(b"small duplicate " * 100)

        groups = self.api.find_duplicates_inplace(scan_dir)
        self.assertEqual(len(groups), 2)
        all_offered = [p for g in groups for p in g["files"]]
        self.assertNotIn(b3_diff, all_offered)
        self.assertNotIn(lib_dup, all_offered)
        self.assertIn(b1, all_offered)
        self.assertIn(b2, all_offered)


if __name__ == "__main__":
    unittest.main()



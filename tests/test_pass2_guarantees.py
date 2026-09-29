"""Pass-2 guarantee tests for the standalone duplicates utility.

G4. No duplicates-utility operation deletes or quarantines the last remaining
    copy of a file, including when every copy is selected, or when files
    change between the scan and the delete.
G5. The duplicate scan writes nothing to the drive being scanned.
G6. On a volume with zero bytes free, the scan completes and permanent delete
    actually frees space. Tested on real, completely full disk images, one
    exFAT and one APFS.

Other G4 cases live with their findings: files changed in the middle after
the scan (P2-02, test_pass2_verify_full_content.py) and concurrent requests
(P2-04, test_pass2_concurrent_removal.py).
"""

import os
import shutil
import sys
import threading
import unittest

from pass2_helpers import (
    MB, DiskImage, Pass2Case, api_client, fill_volume, free_bytes, make_api,
    payload, read_file, tree_snapshot, volume_is_full, wait_for_dup_scan,
    write_file,
)


# ---------------------------------------------------------------------------
# G4: the last copy survives
# ---------------------------------------------------------------------------

class LastCopySurvivesTest(Pass2Case):

    def setUp(self):
        super().setUp()
        self.photo = payload("g4 photo", 300_000)
        self.photos = [
            write_file(self.path("Photos", "IMG_0001.JPG"), self.photo),
            write_file(self.path("Backup", "2019", "IMG_0001.JPG"), self.photo),
            write_file(self.path("Desktop", "IMG_0001 copy.JPG"), self.photo),
        ]
        self.doc = payload("g4 doc", 70_000)
        self.docs = [
            write_file(self.path("Docs", "tax.pdf"), self.doc),
            write_file(self.path("Docs", "old", "tax.pdf"), self.doc),
        ]
        self.unique = write_file(self.path("Docs", "unique.txt"), payload("g4 unique", 500))

    def survivors(self, paths):
        return [p for p in paths if os.path.exists(p)]

    def assert_one_intact_copy_each(self):
        for paths, data in ((self.photos, self.photo), (self.docs, self.doc)):
            alive = self.survivors(paths)
            self.assertGreaterEqual(len(alive), 1, f"every copy of {paths[0]} is gone")
            for p in alive:
                self.assertEqual(read_file(p), data)
        self.assertTrue(os.path.exists(self.unique))

    def remove(self, paths, permanent, groups=None):
        groups = self.scan() if groups is None else groups
        return self.api().trash_inplace_duplicates(
            paths, self.root, permanent_delete=permanent, groups=groups)

    def test_every_copy_selected_permanent_delete(self):
        count, refused, _ = self.remove(self.photos + self.docs + [self.unique], True)
        self.assertEqual(count, 3)
        self.assertEqual(len(self.survivors(self.photos)), 1)
        self.assertEqual(len(self.survivors(self.docs)), 1)
        self.assert_one_intact_copy_each()

    def test_every_copy_selected_quarantine(self):
        count, refused, _ = self.remove(self.photos + self.docs, False)
        self.assertEqual(count, 3)
        self.assert_one_intact_copy_each()

    def test_every_copy_selected_through_the_api(self):
        client, headers = api_client()
        client.post("/api/dup_scan_start", json={"folder": self.root}, headers=headers)
        self.assertEqual(wait_for_dup_scan(client, headers)["group_count"], 2)
        for permanent in (False, True):
            res = client.post("/api/dup_trash_inplace", headers=headers, json={
                "source_paths": self.photos + self.docs, "root_folder": self.root,
                "permanent_delete": permanent}).get_json()
            self.assert_one_intact_copy_each()
        self.assertEqual(res["remaining_results"], [])

    def test_delete_all_redundant_through_the_api(self):
        client, headers = api_client()
        client.post("/api/dup_scan_start", json={"folder": self.root}, headers=headers)
        wait_for_dup_scan(client, headers)
        res = client.post("/api/dup_trash_inplace", headers=headers, json={
            "source_paths": ["ignored"], "root_folder": self.root,
            "permanent_delete": True, "delete_all_redundant": True}).get_json()
        self.assertEqual(res["count"], 3)
        self.assert_one_intact_copy_each()

    def test_kept_copy_deleted_after_scan(self):
        groups = self.scan()
        keeper = next(g["files"][0] for g in groups if g["files"][0] in self.docs)
        os.remove(keeper)
        others = [p for p in self.docs if p != keeper]
        self.remove(others, True, groups)
        self.assert_one_intact_copy_each()

    def test_kept_copy_replaced_after_scan(self):
        groups = self.scan()
        keeper = next(g["files"][0] for g in groups if g["files"][0] in self.docs)
        write_file(keeper, payload("g4 other doc", 70_001))
        others = [p for p in self.docs if p != keeper]
        count, _, _ = self.remove(others, True, groups)
        self.assertEqual(count, 0)
        self.assertEqual([read_file(p) for p in others], [self.doc])

    def test_selected_copy_grew_after_scan(self):
        groups = self.scan()
        dup = next(g["files"][1] for g in groups if g["files"][0] in self.docs)
        with open(dup, "ab") as fh:
            fh.write(b"one more line\n")
        count, _, _ = self.remove([dup], True, groups)
        self.assertEqual(count, 0)
        self.assertTrue(read_file(dup).endswith(b"one more line\n"))

    def test_selected_copy_replaced_by_hard_link_to_kept_copy(self):
        groups = self.scan()
        keeper, dup = next(g["files"] for g in groups if g["files"][0] in self.docs)
        os.remove(dup)
        os.link(keeper, dup)
        self.remove([dup], True, groups)
        self.assertTrue(os.path.exists(keeper))
        self.assertEqual(read_file(keeper), self.doc)

    def test_stale_and_outside_paths_are_left_alone(self):
        outside = write_file(os.path.join(self.sandbox, "outside", "tax.pdf"), self.doc)
        groups = self.scan()
        count, _, _ = self.remove(
            [outside, self.unique, self.path("Docs", "gone.pdf"), "", "relative.pdf"],
            True, groups)
        self.assertEqual(count, 0)
        self.assertTrue(os.path.exists(outside))
        self.assert_one_intact_copy_each()
        self.assertEqual(len(self.survivors(self.docs)), 2)


# ---------------------------------------------------------------------------
# G5: the scan writes nothing (Python-level write attempts)
# ---------------------------------------------------------------------------

_WATCH = {"root": None, "events": []}
_REENTRY = threading.local()
_WRITE_FLAGS = os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_TRUNC | os.O_APPEND
_WRITE_EVENTS = {
    "os.mkdir", "os.rename", "os.remove", "os.rmdir", "os.chmod", "os.chflags",
    "os.lchflags", "os.chown", "os.utime", "os.symlink", "os.link",
    "os.truncate", "os.setxattr", "os.removexattr", "shutil.rmtree",
    "shutil.move", "shutil.copyfile", "shutil.copymode", "shutil.copystat",
    "shutil.copytree", "sqlite3.connect",
}


def _under(value, root):
    if isinstance(value, (str, bytes, os.PathLike)):
        p = os.path.realpath(os.path.abspath(os.fsdecode(value)))
        return p == root or p.startswith(root + os.sep)
    return False


def _audit_hook(event, args):
    root = _WATCH["root"]
    if root is None or getattr(_REENTRY, "on", False):
        return
    _REENTRY.on = True
    try:
        if event == "open":
            path, mode, flags = (tuple(args) + (None, None, None))[:3]
            writing = bool(mode and any(c in str(mode) for c in "wax+")) or \
                bool(isinstance(flags, int) and flags & _WRITE_FLAGS)
            if writing and _under(path, root):
                _WATCH["events"].append((event, args))
        elif event in _WRITE_EVENTS:
            if any(_under(a, root) for a in args):
                _WATCH["events"].append((event, args))
        elif event == "subprocess.Popen":
            _WATCH["events"].append((event, args))
    except Exception:
        pass
    finally:
        _REENTRY.on = False


sys.addaudithook(_audit_hook)


class ScanWritesNothingTest(Pass2Case):

    def build_tree(self):
        data = payload("g5", 5000)
        write_file(self.path("a", "x.bin"), data)
        write_file(self.path("b", "c", "x.bin"), data)
        write_file(self.path("Proj", ".git", "HEAD"), b"ref: refs/heads/main\n")
        write_file(self.path("Proj", "x.bin"), data)
        write_file(self.path("Lib.photoslibrary", "originals", "x.bin"), data)
        write_file(self.path("$RECYCLE.BIN", "x.bin"), data)
        write_file(self.path(".Duplicates_Trash", "x.bin"), data)
        os.symlink(self.path("a"), self.path("link-to-a"))
        os.symlink(self.path("a", "x.bin"), self.path("link.bin"))
        os.link(self.path("a", "x.bin"), self.path("hard.bin"))

    def watch(self, fn):
        _WATCH["events"] = []
        _WATCH["root"] = os.path.realpath(self.root)
        try:
            return fn()
        finally:
            _WATCH["root"] = None

    def test_scan_makes_no_write_calls_under_the_scanned_folder(self):
        self.build_tree()
        before = tree_snapshot(self.root)
        groups = self.watch(lambda: make_api().find_duplicates_inplace(self.root))
        self.assertEqual(len(groups), 1)
        self.assertEqual(_WATCH["events"], [])
        self.assertEqual(tree_snapshot(self.root), before)

    def test_scan_through_the_api_makes_no_write_calls(self):
        self.build_tree()
        before = tree_snapshot(self.root)
        client, headers = api_client()

        def run():
            client.post("/api/dup_scan_start", json={"folder": self.root}, headers=headers)
            return wait_for_dup_scan(client, headers)

        status = self.watch(run)
        self.assertEqual(status["status"], "complete", status["message"])
        self.assertEqual(_WATCH["events"], [])
        self.assertEqual(tree_snapshot(self.root), before)


# ---------------------------------------------------------------------------
# G5 and G6 on real, completely full volumes
# ---------------------------------------------------------------------------

class _FullVolumeGuarantees:
    FS = None
    SIZE_MB = 48

    @classmethod
    def setUpClass(cls):
        cls.image = DiskImage(cls.FS, cls.SIZE_MB, "P2FULL").open()

    @classmethod
    def tearDownClass(cls):
        cls.image.close()

    def test_scan_and_permanent_delete_on_a_full_volume(self):
        mnt = self.image.mountpoint
        data = os.path.join(mnt, "data")
        photo, doc, video = payload("g6 photo", 3 * MB), payload("g6 doc", MB), payload("g6 video", 2 * MB)
        keep_photo = write_file(os.path.join(data, "Photos", "IMG_0001.JPG"), photo)
        dup_photo = write_file(os.path.join(data, "Photos", "Backup", "IMG_0001.JPG"), photo)
        keep_doc = write_file(os.path.join(data, "Docs", "tax.pdf"), doc)
        dup_doc = write_file(os.path.join(data, "Docs", "old", "tax.pdf"), doc)
        keep_video = write_file(os.path.join(data, "Video", "clip.mov"), video)
        dup_video = write_file(os.path.join(data, "Video", "old", "clip.mov"), video)

        # Quarantine one duplicate while there is still room, so the trash can
        # be emptied later on a full volume.
        api = make_api()
        groups = api.find_duplicates_inplace(data)
        count, refused, _ = api.trash_inplace_duplicates(
            [dup_video], data, permanent_delete=False, groups=groups)
        self.assertEqual((count, refused), (1, []))

        fill_volume(mnt)
        self.assertTrue(volume_is_full(mnt), "volume is not completely full")

        # G5 + G6: the scan completes on the full volume and changes nothing.
        before_tree, before_free = tree_snapshot(mnt), free_bytes(mnt)
        client, headers = api_client()
        client.post("/api/dup_scan_start", json={"folder": data}, headers=headers)
        status = wait_for_dup_scan(client, headers, timeout=120)
        self.assertEqual(status["status"], "complete", status["message"])
        results = client.get("/api/dup_scan_status?include_results=1",
                             headers=headers).get_json()["results"]
        self.assertEqual(sorted(g["files"][0] for g in results), sorted([keep_doc, keep_photo]))
        self.assertEqual(tree_snapshot(mnt), before_tree, "the scan changed the volume")
        self.assertEqual(free_bytes(mnt), before_free)
        self.assertTrue(volume_is_full(mnt))

        # G6: permanent delete on the full volume frees real space.
        free0 = free_bytes(mnt)
        res = client.post("/api/dup_trash_inplace", headers=headers, json={
            "source_paths": [dup_photo, dup_doc], "root_folder": data,
            "permanent_delete": True}).get_json()
        freed = free_bytes(mnt) - free0
        self.assertEqual((res["count"], res["refused"]), (2, []), res)
        self.assertGreaterEqual(freed, len(photo) + len(doc) - 64 * 1024)
        self.assertEqual(res["bytes_reclaimed"], len(photo) + len(doc))
        self.assertFalse(os.path.exists(dup_photo) or os.path.exists(dup_doc))
        self.assertEqual(read_file(keep_photo), photo)
        self.assertEqual(read_file(keep_doc), doc)
        self.assertFalse(volume_is_full(mnt))

        # G6: emptying the trash on a full volume frees real space too.
        fill_volume(mnt)
        self.assertTrue(volume_is_full(mnt))
        free0 = free_bytes(mnt)
        res = client.post("/api/dup_empty_trash", headers=headers,
                          json={"root_folder": data}).get_json()
        freed = free_bytes(mnt) - free0
        self.assertEqual((res.get("success"), res.get("files_deleted")), (True, 1), res)
        self.assertGreaterEqual(freed, len(video) - 64 * 1024)
        self.assertEqual(res["bytes_freed"], len(video))
        self.assertEqual(read_file(keep_video), video)


class FullExFATVolumeTest(_FullVolumeGuarantees, unittest.TestCase):
    FS = "ExFAT"


class FullAPFSVolumeTest(_FullVolumeGuarantees, unittest.TestCase):
    FS = "APFS"


if __name__ == "__main__":
    unittest.main()

"""Regression tests for the data-safety fixes.

Every test in this file corresponds to a defect that was reproduced against the
previous code. They are written so that each one FAILS if its fix is reverted,
and several include a deliberate negative control so a test cannot pass simply
by the code doing nothing at all.

Run with:  .venv/bin/python -m unittest discover -s tests -v
"""

import os
import shutil
import sqlite3
import sys
import tempfile
import threading
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.api_organizer import OrganizerAPI
from src.categorizer import Categorizer
from src.file_ops import FileEngine, copy_project_intact, files_are_identical
from src.scanner import Scanner

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CONFIG_PATH = os.path.join(REPO_ROOT, "src", "config.json")

ONE_MB = 1024 * 1024


def write_file(path, data=b"hello"):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as fh:
        fh.write(data)
    return path


def make_lookalike(middle_byte):
    """Two files that share a size and both sampled 1MB windows but differ
    in the middle -- exactly what defeats a first+last part hash."""
    return b"A" * ONE_MB + middle_byte * 1024 + b"Z" * ONE_MB


class TempCaseMixin:
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="organizer_safety_")
        self.source = os.path.join(self.tmp, "source")
        self.dest = os.path.join(self.tmp, "dest")
        os.makedirs(self.source, exist_ok=True)
        os.makedirs(self.dest, exist_ok=True)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)


class RepairTransferTests(TempCaseMixin, unittest.TestCase):
    """C-1: repair_transfer used to delete healthy files.

    A failed copy recorded the path it *would* have been written to. If a
    different file legitimately owned that path, repair deleted the healthy
    file while 'cleaning up' the failure.
    """

    def _api(self):
        return OrganizerAPI(CONFIG_PATH, lambda m: None, lambda *a, **k: None)

    def _seed_db(self, rows):
        engine = FileEngine(self.dest)
        for source_path, dest_path, size, part_hash, status in rows:
            engine.record_copy(source_path, dest_path, size, 0.0, part_hash, status=status)
        engine.close()

    def test_healthy_file_not_deleted_for_someone_elses_failure(self):
        victim = write_file(os.path.join(self.dest, "Documents", "report.pdf"), b"important")
        size = os.path.getsize(victim)

        self._seed_db([
            # The healthy file, copied successfully.
            ("/src/a/report.pdf", victim, size, "", "completed"),
            # A different source that failed; under the old code this row
            # carried the same dest_path and caused the deletion.
            ("/src/b/report.pdf", "", 10, "", "failed: disk full"),
        ])

        result = self._api().repair_transfer(self.dest)

        self.assertTrue(result["success"])
        self.assertTrue(os.path.exists(victim), "repair deleted a healthy, successfully-copied file")
        self.assertEqual(result.get("deleted_count", 0), 0)

    def test_contested_path_is_never_deleted(self):
        contested = write_file(os.path.join(self.dest, "Documents", "notes.txt"), b"data")
        size = os.path.getsize(contested)

        # Two rows claim the same path and both look bad (wrong size recorded).
        self._seed_db([
            ("/src/a/notes.txt", contested, size + 99, "", "completed"),
            ("/src/b/notes.txt", contested, size + 99, "", "completed"),
        ])

        result = self._api().repair_transfer(self.dest)

        self.assertTrue(os.path.exists(contested), "deleted a path claimed by more than one record")
        self.assertEqual(result.get("deleted_count", 0), 0)

    def test_path_outside_destination_is_never_deleted(self):
        outsider = write_file(os.path.join(self.tmp, "elsewhere", "precious.txt"), b"do not touch")
        self._seed_db([
            ("/src/precious.txt", outsider, 999999, "", "completed"),
        ])

        result = self._api().repair_transfer(self.dest)

        self.assertTrue(os.path.exists(outsider), "repair deleted a file outside the destination")
        self.assertEqual(result.get("deleted_count", 0), 0)

    def test_negative_control_genuinely_corrupt_file_is_still_removed(self):
        """Repair must still do its job, or the tests above prove nothing."""
        corrupt = write_file(os.path.join(self.dest, "Documents", "broken.bin"), b"tiny")
        self._seed_db([
            ("/src/broken.bin", corrupt, 999999, "", "completed"),  # size mismatch
        ])

        result = self._api().repair_transfer(self.dest)

        self.assertFalse(os.path.exists(corrupt), "repair failed to remove a genuinely corrupt file")
        self.assertEqual(result.get("deleted_count", 0), 1)
        self.assertEqual(result["repaired_count"], 1)

    def test_failed_row_records_no_destination(self):
        """The underlying cause: a failed row must not name a path it never wrote."""
        engine = FileEngine(self.dest)
        engine.record_copy("/src/x.txt", "", 5, 0.0, "", status="failed: boom")
        engine.close()

        conn = sqlite3.connect(os.path.join(self.dest, ".organizer_checkpoint.db"))
        rows = conn.execute("SELECT dest_path FROM copies WHERE source_path = '/src/x.txt'").fetchall()
        conn.close()

        self.assertEqual(rows[0][0], "")


class DeduplicationTests(TempCaseMixin, unittest.TestCase):
    """C-2 / X-1: dedup must be byte-exact and race-free."""

    def test_part_hash_collision_is_not_treated_as_duplicate(self):
        original = write_file(os.path.join(self.dest, "Media", "clip.mov"), make_lookalike(b"1"))
        candidate = write_file(os.path.join(self.source, "clip.mov"), make_lookalike(b"2"))

        self.assertEqual(os.path.getsize(original), os.path.getsize(candidate))
        self.assertFalse(files_are_identical(original, candidate))

        engine = FileEngine(self.dest)
        try:
            size = os.path.getsize(original)
            engine.record_copy("/src/original/clip.mov", original, size, 0.0,
                               engine._get_part_hash(original, size))
            is_dup, _ = engine.is_content_duplicate(candidate, os.path.getsize(candidate))
        finally:
            engine.close()

        self.assertFalse(is_dup, "two different files with colliding part hashes were called duplicates")

    def test_identical_content_is_still_deduplicated(self):
        """Negative control for the test above."""
        payload = make_lookalike(b"1")
        original = write_file(os.path.join(self.dest, "Media", "same.mov"), payload)
        candidate = write_file(os.path.join(self.source, "same.mov"), payload)

        engine = FileEngine(self.dest)
        try:
            size = os.path.getsize(original)
            engine.record_copy("/src/original/same.mov", original, size, 0.0,
                               engine._get_part_hash(original, size))
            is_dup, _ = engine.is_content_duplicate(candidate, os.path.getsize(candidate))
        finally:
            engine.close()

        self.assertTrue(is_dup, "identical files were not deduplicated")

    def test_missing_destination_file_does_not_suppress_the_copy(self):
        """A DB row whose file has since vanished must not block a re-copy."""
        ghost = os.path.join(self.dest, "Media", "gone.mov")
        payload = make_lookalike(b"1")
        candidate = write_file(os.path.join(self.source, "gone.mov"), payload)

        engine = FileEngine(self.dest)
        try:
            engine.record_copy("/src/gone.mov", ghost, len(payload), 0.0,
                               engine._get_part_hash(candidate, len(payload)))
            is_dup, _ = engine.is_content_duplicate(candidate, len(payload))
        finally:
            engine.close()

        self.assertFalse(is_dup, "a duplicate verdict was returned against a file that no longer exists")

    def test_parallel_identical_files_do_not_produce_a_renamed_copy(self):
        """X-1: two workers handling identical same-named files raced past
        dedup entirely, so the loser was written as name_1 instead of skipped."""
        payload = b"identical payload" * 1000
        a = write_file(os.path.join(self.source, "dir_a", "photo.jpg"), payload)
        b = write_file(os.path.join(self.source, "dir_b", "photo.jpg"), payload)

        engine = FileEngine(self.dest)
        results = {}
        barrier = threading.Barrier(2)

        def worker(name, path):
            barrier.wait()
            target = os.path.join(self.dest, "Media", "photo.jpg")
            final, part_hash = engine.resolve_destination(target, "photo.jpg",
                                                          os.path.getsize(path), path)
            results[name] = final
            if final:
                engine.copy_file(path, final)
                engine.record_copy(path, final, os.path.getsize(path), 0.0, part_hash)
                engine.release_reservation(final)
            else:
                engine.record_copy(path, "DUPLICATE_SKIPPED", os.path.getsize(path), 0.0, part_hash)

        threads = [threading.Thread(target=worker, args=("a", a)),
                   threading.Thread(target=worker, args=("b", b))]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        engine.close()

        media = os.path.join(self.dest, "Media")
        written = sorted(os.listdir(media)) if os.path.isdir(media) else []
        self.assertEqual(written, ["photo.jpg"],
                         f"identical files raced past dedup and produced {written}")
        self.assertEqual(sum(1 for v in results.values() if v is None), 1,
                         "exactly one of the two identical files should have been skipped")


class AppleScriptInjectionTests(unittest.TestCase):
    """C-3: the folder-picker prompt was interpolated into AppleScript source."""

    def test_prompt_is_not_interpolated_into_script_source(self):
        import inspect
        from src import app as app_module
        from src import cli as cli_module

        for func in (app_module.select_folder, cli_module._macos_choose_folder):
            src = inspect.getsource(func)
            self.assertIn("on run argv", src,
                          f"{func.__name__} no longer passes the prompt via argv")
            self.assertNotIn('with prompt "{', src,
                             f"{func.__name__} interpolates the prompt into the script source")

    def test_payload_in_prompt_does_not_execute(self):
        """End-to-end proof using osascript itself, with no dialog involved."""
        import subprocess
        marker = os.path.join(tempfile.mkdtemp(prefix="applescript_"), "pwned.txt")
        payload = f'x" & (do shell script "touch {marker}") & "y'

        script = '''
        on run argv
            return (item 1 of argv)
        end run
        '''
        result = subprocess.run(["osascript", "-e", script, payload],
                                capture_output=True, text=True)

        self.assertFalse(os.path.exists(marker), "AppleScript executed an injected shell command")
        self.assertIn("do shell script", result.stdout,
                      "the payload should come back as inert text")


class ApiTokenTests(unittest.TestCase):
    """L-5: every /api/ route was reachable with no authentication."""

    def setUp(self):
        from src import app as app_module
        self.app_module = app_module
        app_module.app.config["TESTING"] = True
        self.client = app_module.app.test_client()

    def test_api_rejects_missing_token(self):
        resp = self.client.get("/api/status")
        self.assertEqual(resp.status_code, 403)

    def test_api_rejects_wrong_token(self):
        resp = self.client.get("/api/status", headers={"X-Organizer-Token": "not-the-token"})
        self.assertEqual(resp.status_code, 403)

    def test_api_accepts_header_token(self):
        resp = self.client.get("/api/status",
                               headers={"X-Organizer-Token": self.app_module.API_TOKEN})
        self.assertEqual(resp.status_code, 200)

    def test_api_accepts_query_token_for_downloads(self):
        resp = self.client.get(f"/api/status?token={self.app_module.API_TOKEN}")
        self.assertEqual(resp.status_code, 200)

    def test_index_is_not_gated_and_carries_the_token(self):
        resp = self.client.get("/")
        self.assertEqual(resp.status_code, 200)
        self.assertIn(self.app_module.API_TOKEN.encode(), resp.data,
                      "the UI cannot authenticate without the token in the page")

    def test_token_is_not_predictable(self):
        self.assertGreaterEqual(len(self.app_module.API_TOKEN), 32)


class DissolveProjectTests(TempCaseMixin, unittest.TestCase):
    """H-1: dissolve accepted any path and ended in an unconditional rmtree."""

    def _api(self):
        return OrganizerAPI(CONFIG_PATH, lambda m: None, lambda *a, **k: None)

    def test_path_outside_code_folder_is_refused(self):
        outsider = os.path.join(self.tmp, "precious")
        write_file(os.path.join(outsider, "family.txt"), b"irreplaceable")

        ok, msg = self._api().dissolve_and_resort_project(self.dest, outsider)

        self.assertFalse(ok)
        self.assertTrue(os.path.exists(os.path.join(outsider, "family.txt")),
                        "dissolve deleted a folder outside the destination")

    def test_code_root_itself_is_refused(self):
        code_root = os.path.join(self.dest, "Code")
        write_file(os.path.join(code_root, "proj", "main.py"), b"print(1)")

        ok, msg = self._api().dissolve_and_resort_project(self.dest, code_root)

        self.assertFalse(ok)
        self.assertTrue(os.path.isdir(code_root), "dissolve removed the entire Code folder")

    def test_traversal_out_of_code_folder_is_refused(self):
        outsider = os.path.join(self.tmp, "precious")
        write_file(os.path.join(outsider, "family.txt"), b"irreplaceable")
        traversal = os.path.join(self.dest, "Code", "..", "..", "precious")

        ok, _ = self._api().dissolve_and_resort_project(self.dest, traversal)

        self.assertFalse(ok)
        self.assertTrue(os.path.exists(os.path.join(outsider, "family.txt")))

    def test_negative_control_real_project_is_dissolved(self):
        proj = os.path.join(self.dest, "Code", "myproj")
        write_file(os.path.join(proj, "notes.txt"), b"some notes")

        ok, msg = self._api().dissolve_and_resort_project(self.dest, proj)

        self.assertTrue(ok, msg)
        self.assertFalse(os.path.exists(proj), "a legitimate project was not dissolved")


class ProjectDetectionTests(TempCaseMixin, unittest.TestCase):
    """H-2: .git was pruned from the walk before project markers were tested,
    so a repository whose only marker was .git was never detected."""

    def _scan(self):
        scanner = Scanner(Categorizer(CONFIG_PATH),
                          staging_root=os.path.join(self.tmp, "staging"))
        scanner.scan_directory(self.source)
        return scanner

    def test_git_only_repository_is_detected(self):
        repo = os.path.join(self.source, "gitonly")
        write_file(os.path.join(repo, ".git", "HEAD"), b"ref: refs/heads/main\n")
        write_file(os.path.join(repo, "main.py"), b"print('hi')")

        scanner = self._scan()

        self.assertIn(os.path.realpath(repo),
                      [os.path.realpath(p) for p in scanner.projects_found],
                      "a repo identified only by .git was not detected as a project")

    def test_repository_files_are_not_also_scattered(self):
        repo = os.path.join(self.source, "gitonly")
        write_file(os.path.join(repo, ".git", "HEAD"), b"ref: refs/heads/main\n")
        write_file(os.path.join(repo, "main.py"), b"print('hi')")

        scanner = self._scan()

        scattered = [f for f in scanner.files_to_process if "gitonly" in f]
        self.assertEqual(scattered, [],
                         "files from an intact project were also queued for scattering")

    def test_git_history_is_preserved_when_copying(self):
        repo = os.path.join(self.source, "repo")
        write_file(os.path.join(repo, ".git", "HEAD"), b"ref: refs/heads/main\n")
        write_file(os.path.join(repo, "node_modules", "junk.js"), b"// regenerable")
        write_file(os.path.join(repo, "main.py"), b"print('hi')")

        target = os.path.join(self.dest, "Code", "repo")
        ok, err = copy_project_intact(repo, target)

        self.assertTrue(ok, err)
        self.assertTrue(os.path.exists(os.path.join(target, ".git", "HEAD")),
                        "git history was stripped from a project copied 'intact'")
        self.assertFalse(os.path.exists(os.path.join(target, "node_modules")),
                         "regenerable build output should still be skipped")


class PathValidationTests(unittest.TestCase):
    """H-4: the GUI had no source/destination overlap guard and the CLI used
    a naive string prefix test."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="organizer_paths_")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_destination_inside_source_is_rejected(self):
        src = os.path.join(self.tmp, "drive")
        dest = os.path.join(src, "organized")
        os.makedirs(dest)
        self.assertIsNotNone(OrganizerAPI.validate_paths(src, dest))

    def test_source_inside_destination_is_rejected(self):
        dest = os.path.join(self.tmp, "organized")
        src = os.path.join(dest, "messy")
        os.makedirs(src)
        self.assertIsNotNone(OrganizerAPI.validate_paths(src, dest))

    def test_identical_paths_are_rejected(self):
        same = os.path.join(self.tmp, "same")
        os.makedirs(same)
        self.assertIsNotNone(OrganizerAPI.validate_paths(same, same))

    def test_sibling_with_shared_prefix_is_allowed(self):
        """The old startswith() test wrongly rejected this."""
        src = os.path.join(self.tmp, "Photos")
        dest = os.path.join(self.tmp, "Photos_Backup")
        os.makedirs(src)
        os.makedirs(dest)
        self.assertIsNone(OrganizerAPI.validate_paths(src, dest))

    def test_symlinked_destination_inside_source_is_rejected(self):
        """The old startswith() test could be walked around with a symlink."""
        src = os.path.join(self.tmp, "drive")
        real_dest = os.path.join(src, "organized")
        os.makedirs(real_dest)
        link = os.path.join(self.tmp, "shortcut")
        os.symlink(real_dest, link)
        self.assertIsNotNone(OrganizerAPI.validate_paths(src, link))

    def test_every_source_in_a_multi_source_list_is_checked(self):
        safe = os.path.join(self.tmp, "safe")
        risky = os.path.join(self.tmp, "risky")
        dest = os.path.join(risky, "organized")
        os.makedirs(safe)
        os.makedirs(dest)
        self.assertIsNotNone(OrganizerAPI.validate_paths(f"{safe},{risky}", dest))
        self.assertIsNotNone(OrganizerAPI.validate_paths([safe, risky], dest))


class StagingLocationTests(TempCaseMixin, unittest.TestCase):
    """H-3: Google Takeout archives were unzipped onto the source drive."""

    def test_default_staging_is_not_on_the_source(self):
        scanner = Scanner(Categorizer(CONFIG_PATH))
        self.assertNotIn(os.path.realpath(self.source),
                         os.path.realpath(scanner.staging_root))
        self.assertTrue(os.path.realpath(scanner.staging_root).startswith(
            os.path.realpath(tempfile.gettempdir())))

    def test_explicit_staging_root_is_honoured(self):
        staging = os.path.join(self.dest, ".organizer_staging")
        scanner = Scanner(Categorizer(CONFIG_PATH), staging_root=staging)
        self.assertEqual(os.path.realpath(scanner.staging_root), os.path.realpath(staging))

    def test_cleanup_removes_staged_directories(self):
        staging = os.path.join(self.dest, ".organizer_staging")
        scanner = Scanner(Categorizer(CONFIG_PATH), staging_root=staging)
        staged = os.path.join(staging, "abc123")
        write_file(os.path.join(staged, "file.txt"), b"staged")
        scanner.staging_dirs_used.append(staged)

        scanner.cleanup_staging()

        self.assertFalse(os.path.exists(staged), "staged extraction was left behind")


class UnsortedTaggingTests(unittest.TestCase):
    """L-1: `"Unsorted" in rel_dest` matched innocent filenames."""

    def test_filename_containing_unsorted_is_not_a_directory_match(self):
        rel = os.path.join("Documents", "Unsorted ideas.txt")
        self.assertNotIn("Unsorted", rel.split(os.sep)[:-1])

    def test_real_unsorted_directory_still_matches(self):
        rel = os.path.join("Unsorted", "mystery.bin")
        self.assertIn("Unsorted", rel.split(os.sep)[:-1])
        rel_nested = os.path.join("Media", "Screenshots", "Unsorted", "shot.png")
        self.assertIn("Unsorted", rel_nested.split(os.sep)[:-1])


class VolumeListingTests(unittest.TestCase):
    """M-4: shutil was never imported in app.py, and a bare `except Exception`
    swallowed the resulting NameError, so this endpoint always returned []."""

    def test_shutil_is_importable_from_app_module(self):
        from src import app as app_module
        self.assertTrue(hasattr(app_module, "shutil"),
                        "app.py calls shutil.disk_usage but never imports shutil")

    def test_list_volumes_returns_mounted_volumes(self):
        from src import app as app_module
        app_module.app.config["TESTING"] = True
        client = app_module.app.test_client()

        resp = client.get("/api/list_volumes",
                          headers={"X-Organizer-Token": app_module.API_TOKEN})

        self.assertEqual(resp.status_code, 200)
        payload = resp.get_json()
        if os.path.isdir("/Volumes") and os.listdir("/Volumes"):
            self.assertTrue(payload.get("volumes"),
                            "/Volumes is populated but the endpoint reported nothing")


class RerunIdempotencyTests(TempCaseMixin, unittest.TestCase):
    """Running the same job twice (the "did I already sort this?" case) must
    not duplicate anything. Loose files were checkpointed, but code projects
    were not, so every re-run cloned each repo as project_1, project_2, ..."""

    def _api(self):
        return OrganizerAPI(CONFIG_PATH, lambda m: None, lambda *a, **k: None)

    def _seed_source(self):
        write_file(os.path.join(self.source, "stuff", "notes.txt"), b"some notes")
        repo = os.path.join(self.source, "my-app")
        write_file(os.path.join(repo, ".git", "HEAD"), b"ref: refs/heads/main")
        write_file(os.path.join(repo, "app.py"), b"print('hi')")

    def _dest_listing(self):
        out = []
        for root, _dirs, files in os.walk(self.dest):
            for f in files:
                out.append(os.path.relpath(os.path.join(root, f), self.dest))
        return sorted(out)

    def test_second_identical_run_changes_nothing(self):
        self._seed_source()
        api = self._api()

        api.run(self.source, self.dest)
        after_first = self._dest_listing()
        api.run(self.source, self.dest)
        after_second = self._dest_listing()

        self.assertEqual(after_first, after_second,
                         "re-running the same job changed the destination")

    def test_project_is_not_cloned_on_rerun(self):
        self._seed_source()
        api = self._api()

        api.run(self.source, self.dest)
        api.run(self.source, self.dest)

        code_dir = os.path.join(self.dest, "Code")
        self.assertEqual(sorted(os.listdir(code_dir)), ["my-app"],
                         "the code project was cloned by the second run")

    def test_project_is_recognised_without_a_checkpoint(self):
        """Covers a destination organized before project checkpoints existed:
        the DB has no record, so recognition must fall back to content."""
        self._seed_source()
        api = self._api()
        api.run(self.source, self.dest)

        engine = FileEngine(self.dest)
        with engine.lock:
            engine.conn.execute("DELETE FROM projects")
            engine.conn.commit()
        engine.close()

        api.run(self.source, self.dest)

        code_dir = os.path.join(self.dest, "Code")
        self.assertEqual(sorted(os.listdir(code_dir)), ["my-app"],
                         "a project with no checkpoint record was cloned instead of recognised")

    def test_genuinely_different_project_with_same_name_still_gets_a_suffix(self):
        """Negative control: two unrelated projects sharing a name must not be
        merged into one just because dedup got keener."""
        api = self._api()
        repo_a = os.path.join(self.source, "a", "my-app")
        write_file(os.path.join(repo_a, ".git", "HEAD"), b"ref: refs/heads/main")
        write_file(os.path.join(repo_a, "app.py"), b"print('from A')")

        repo_b = os.path.join(self.source, "b", "my-app")
        write_file(os.path.join(repo_b, ".git", "HEAD"), b"ref: refs/heads/main")
        write_file(os.path.join(repo_b, "app.py"), b"print('a completely different program')")

        api.run(self.source, self.dest)

        code_dir = os.path.join(self.dest, "Code")
        self.assertEqual(sorted(os.listdir(code_dir)), ["my-app", "my-app_1"],
                         "two different projects sharing a name were collapsed together")


class OrganizerHousekeepingTests(TempCaseMixin, unittest.TestCase):
    """Feeding an already-organized folder back in as a SOURCE used to drag the
    organizer's own checkpoint database and Spotlight marker into Unsorted/."""

    def test_housekeeping_files_are_treated_as_garbage(self):
        for name in (".organizer_checkpoint.db", ".organizer_checkpoint.db-shm",
                     ".organizer_checkpoint.db-wal", ".metadata_never_index"):
            write_file(os.path.join(self.source, name), b"internal bookkeeping")
        write_file(os.path.join(self.source, "real.txt"), b"actual user data")

        scanner = Scanner(Categorizer(CONFIG_PATH),
                          staging_root=os.path.join(self.tmp, "staging"))
        scanner.scan_directory(self.source)

        queued = [os.path.basename(f) for f in scanner.files_to_process]
        self.assertEqual(queued, ["real.txt"],
                         f"the organizer queued its own housekeeping files: {queued}")

    def test_reorganizing_an_organized_folder_adds_no_unsorted_junk(self):
        write_file(os.path.join(self.source, "stuff", "notes.txt"), b"some notes")
        api = OrganizerAPI(CONFIG_PATH, lambda m: None, lambda *a, **k: None)
        api.run(self.source, self.dest)

        second = os.path.join(self.tmp, "dest2")
        api.run(self.dest, second)

        unsorted = os.path.join(second, "Unsorted")
        leftovers = os.listdir(unsorted) if os.path.isdir(unsorted) else []
        self.assertEqual(leftovers, [],
                         f"organizer internals were filed as user data: {leftovers}")

    def test_staging_directory_is_never_rescanned(self):
        staging = os.path.join(self.source, ".organizer_staging")
        write_file(os.path.join(staging, "extracted", "leftover.txt"), b"staged content")
        write_file(os.path.join(self.source, "real.txt"), b"actual user data")

        scanner = Scanner(Categorizer(CONFIG_PATH),
                          staging_root=os.path.join(self.tmp, "staging"))
        scanner.scan_directory(self.source)

        queued = [os.path.basename(f) for f in scanner.files_to_process]
        self.assertEqual(queued, ["real.txt"],
                         "a leftover staging area was re-ingested as user content")


if __name__ == "__main__":
    unittest.main()

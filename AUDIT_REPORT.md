# Drive Organizer: Full App Audit (2026-09)

Branch: `audit/full-app-audit-2026-09`, created from `main` at `92ed5c1`.

The audit runs in four sequential passes. Each pass adds its own section below.
A separate reviewer checks all passes at the end.

| Pass | Scope | Status |
|------|-------|--------|
| 1 | Data safety in the organize flow's backend | In progress |
| 2 | Duplicates utility | Not started |
| 3 | Web UI | Not started |
| 4 | CLI and local API | Not started |

---

## Pass 1: Organize-flow data safety

### Scope

In scope: scanning, preview (dry run), transfer/copy, verify, repair/re-sync
(including the automatic pre-transfer repair), history, cancellation, and the
de-duplication that happens while organizing. That includes the checkpoint
database's `DUPLICATE_SKIPPED` records and the organize-flow actions that act on
them (`get_duplicate_records`, `trash_duplicates`, `restore_duplicates`), plus
code-project copying and dissolving (`copy_project_intact`,
`dissolve_and_resort_project`).

Out of scope (left to later passes): the standalone duplicates utility
(in-place scan, quarantine, permanent delete, empty trash), the web UI, the CLI,
and the local API's auth. Anything spotted there is listed under
"For later passes" below.

### Guarantees tested

- **G1.** No organize-flow operation deletes or overwrites the last remaining copy of a file.
- **G2.** Preview (dry run) writes nothing to the source drive.
- **G3.** Cancelling or crashing mid-transfer never leaves a file lost, or half-copied at its final destination.

### Method

- Read only the modules this scope needs: `src/api_organizer.py` (organize,
  verify, repair, dissolve, organize-flow duplicate actions),
  `src/file_ops.py`, `src/scanner.py`, `src/categorizer.py`, `src/utils.py`.
- Reproduced each finding with an automated test on synthetic data in a
  private temp folder that the test deletes afterwards. Crashes are simulated
  in a child process that calls `os._exit()` halfway through writing a file,
  so no `finally` block, `atexit` handler, or database commit runs. That is as
  close to a power cut or `kill -9` as a unit test can get.
- Each fix is its own commit, together with a test that fails on `92ed5c1`
  and passes with the fix. `make test` passes at every commit.

### Findings

Severity scale: **Critical** means data loss is likely in normal use.
**High** means data loss, or a broken guarantee, in a plausible scenario.
**Medium** means misleading results that could lead the owner to delete data.
**Low** means a narrow edge case or defence-in-depth.

Findings are numbered in the order they were identified, not by severity, and
are added here as each one is confirmed. Commit hashes are listed in the
summary table at the end of this section.

#### P1-01 (Critical): repair and auto-repair permanently delete the last copy of a file

- **Location:** `src/api_organizer.py`, `OrganizerAPI.repair_transfer` (the
  deletion step), reached from the Verify & Repair button and from
  `auto_repair_if_needed`, which `run()` calls before every real transfer into
  a destination that already has a checkpoint database.
- **What happens:** any destination file whose size (or, in deep mode, its
  sampled hash) no longer matched its checkpoint record was deleted with
  `_force_remove`, which bypasses the Trash and even clears a Finder lock.
  Repair never checked whether the source still existed, or whether the file
  was really a broken copy rather than one the owner changed after
  organizing. So it deleted:
  - organized files the owner later edited (a document, a photo retouched in
    place, a spreadsheet), at the start of the next merge or resume, and
  - any mismatching file whose source had since been wiped, which is exactly
    the owner's plan for the source drive. Then the deleted file was the last
    copy.
- **Steps to reproduce:** organize a folder; append a line to an organized
  `.txt` file; run another real transfer (merge) from any source into the same
  destination. The edited file is gone, not in the Trash. Or: organize, delete
  the source file, change the organized file, and click Verify & Repair.
- **Fix:** a new fourth condition for deleting: the file must be a cut-short
  (or complete) copy of a source file that still exists, meaning every byte of
  the file matches the start of the source (`OrganizerAPI._is_cut_short_copy`).
  Deleting that loses nothing, and the next run copies it again. Any other
  mismatching file is kept, and only its checkpoint record is cleared, as
  before. `repair_transfer` now also returns `kept_count`, and auto-repair logs
  it.
- **Behaviour change to note:** after a kept file's record is cleared, a
  later run whose source still has the original copies the original again,
  next to the kept file (for example `letter_1.txt`). Nothing is lost, but the
  owner may see both versions. A copy that was damaged in some other way (for
  example zero-filled after a power cut, rather than cut short) is also kept
  now instead of deleted, next to the fresh copy. Deleting it is left to the
  owner.
- **Residual risk:** an edit that only truncates a file (keeping its first
  bytes unchanged) still looks like a cut-short copy and is deleted. Every byte
  of it still exists in the source, so no data is lost.
- **Existing test changed:** `test_data_safety.py`,
  `test_negative_control_genuinely_corrupt_file_is_still_removed`, seeded a
  record whose source (`/src/broken.bin`) did not exist and expected the
  mismatching file to be deleted. Under the new rule that file could be the
  last copy, so it is kept. The fixture now creates a real source whose first
  bytes match the cut-short copy; the test still checks that repair removes a
  genuinely broken copy.
- **Status:** Fixed. Tests in `tests/test_pass1_repair_keeps_last_copy.py`
  (the first three fail on `92ed5c1`):
  - `test_repair_keeps_changed_copy_when_source_is_gone`
  - `test_auto_repair_keeps_file_edited_after_organizing`
  - `test_deep_repair_keeps_same_size_edit`
  - `test_repair_still_removes_truncated_copy_and_resync_restores_it`
    (negative control, passes before and after)

#### P1-02 (High): organizing deletes the owner's own `*.tmp` files

- **Location:** `src/file_ops.py`, `FileEngine.resolve_destination`
  (`_cleanup_orphan_tmp`, `is_path_busy`) and `FileEngine.copy_file`.
- **What happens:** `copy_file` writes each file to `<final name>.tmp` and
  renames it into place when complete. To clean up after an interrupted run,
  `resolve_destination` permanently deleted (`_force_remove`) any existing
  `<name>.tmp` whenever it was about to place `<name>` and `<name>` did not
  exist yet. But `.tmp` is an ordinary extension. Files such as
  `settings.json.tmp` or `export.csv.tmp` are organized into the flat
  `Unsorted/` folder (every unknown extension goes there), so the next time a
  `settings.json` or `export.csv` is placed in `Unsorted/`, in the same run or
  a later merge, the organizer deletes the earlier file. If its source was
  wiped, that was the last copy.
- **Steps to reproduce:** organize a drive that has `exports/data.json.tmp`;
  then organize (merge) another drive that has `exports/data.json` into the
  same destination. `Unsorted/data.json.tmp` is gone, not in the Trash.
- **Fix:** in-progress copies now use the app-specific suffix
  `.organizer-partial` (`file_ops.PARTIAL_SUFFIX`, `file_ops.partial_path_for`,
  which also keeps the name within the 255-byte limit), and only files with
  that suffix are ever cleaned up. Verify's fallback index now skips
  `.organizer-partial` files instead of every `*.tmp` file, so an organized
  `*.tmp` file can be found again after the drive is remounted elsewhere.
  Final file names are unchanged.
- **Note:** leftovers named `*.tmp` from an interrupted run of an older
  version are no longer removed automatically. They cannot be told apart from
  the owner's files, so they are left for the owner to delete.
- **Status:** Fixed. Tests in `tests/test_pass1_partial_file_naming.py`:
  - `test_existing_dot_tmp_file_in_destination_is_not_deleted` (fails on
    `92ed5c1`)
  - `test_tmp_file_organized_earlier_survives_a_later_run` (fails on
    `92ed5c1`)
  - `test_leftover_partial_copy_is_still_cleaned_up` and
    `test_partial_name_fits_macos_name_limit` cover the new suffix. They error
    on `92ed5c1` because `partial_path_for` does not exist there.

#### P1-03 (High): a crash during a code-project copy leaves a half-copied project

- **Location:** `src/file_ops.py`, `copy_project_intact` (used by `run()` and
  by the CLI); `src/api_organizer.py`, `list_code_projects`.
- **What happens:** loose files are written to a temporary name and renamed
  into place, but code projects were copied with `shutil.copytree` straight
  into their final folder `Code/<name>`, file by file. A crash, power cut, or
  unplugged drive in the middle left `Code/<name>` with some files missing and
  one truncated. The next run did not recognise it as a copy (correctly), so it
  copied the project again as `Code/<name>_1` and left the broken folder in
  place. Both look like real projects; the owner may keep the broken one, or
  dissolve it into the library, and wipe the source. This breaks G3.
- **Steps to reproduce:** organize a source containing a project folder (with
  a `package.json`) and kill the app while the project is being copied (the
  test does this in a child process with `os._exit()` halfway through a file).
  Run again: `Code/` holds `webapp` (broken) and `webapp_1`.
- **Fix:** the project is copied into a hidden staging folder next to its final
  place (`Code/.<name>.organizer-partial`, see `project_staging_path`) and
  renamed to `Code/<name>` only once every file is in place. A staging folder
  left by an interrupted run is discarded and the copy starts again. The
  project list offered for dissolving (`list_code_projects`) never shows a
  staging folder.
- **Behaviour change to note:** a project that cannot be copied completely
  (for example, one file is unreadable) is no longer left partly copied under
  its final name. Nothing is placed, and the existing warning is logged; the
  source is untouched. Previously the readable files were left in
  `Code/<name>`.
- **Status:** Fixed. Tests in `tests/test_pass1_project_copy_atomic.py`, both
  fail on `92ed5c1`:
  - `test_crash_during_project_copy_leaves_no_half_copied_project`
  - `test_leftover_staging_folder_is_not_offered_as_a_project`

#### P1-04 (High): dissolving a code project permanently deletes `.git` history and some folders

- **Location:** `src/api_organizer.py`, `dissolve_and_resort_project` (the
  "dissolve" action offered for folders under `<dest>/Code`).
- **What happens:** dissolving moves the project's files into the library
  (Media, Documents, ...), then deletes the project folder with
  `shutil.rmtree`. Both the move loop and the "is anything left?" check skip
  the folders in `SKIP_SYSTEM_DIRS` and the "garbage" file names, so whatever
  is inside them is never moved and never counted as left over, and `rmtree`
  deletes it without a warning. The project copy (`copy_project_intact`)
  does bring some of these along, so on a real destination this deletes:
  - the whole `.git` folder (all commit history, stashes, unpushed branches);
  - folders the owner happened to name `Caches` or `.tmp`, with everything in
    them;
  - `._*` (AppleDouble: resource forks and Finder metadata on exFAT drives)
    and `~$*` files;
  - any other skip-listed folder that is in the destination copy (for
    example one copied by an older version or put there by the owner).

  If the owner has wiped the source, which is the point of organizing, these
  were the last copies.
- **Steps to reproduce (confirmed on synthetic data):** source
  `my_app/{.git/HEAD, .git/objects/ab/cdef0123, Caches/meeting-notes.txt,
  main.py, ._main.py, ~$report.docx}`; organize into a new destination;
  `Code/my_app` holds all six. Call
  `dissolve_and_resort_project(dest, dest/Code/my_app)`: it returns
  "Project dissolved and files re-sorted successfully!", `main.py` is now
  `Code/Snippets/main.py`, and the other five files exist nowhere in the
  destination.
- **Not fixed, owner decision:** the existing test
  `tests/test_full_audit_fixes.py::test_dissolve_project_skips_git_deletes_duplicates_and_persists_across_runs`
  asserts that the dissolved folder, `.git` included, is gone, so deleting it
  is the current intended design. Changing it changes behaviour.
- **Recommendation:** never `rmtree` in this flow. Either move what is left
  (the `.git` folder and the skipped folders) to the Trash or to an organizer
  quarantine folder the owner can review, or keep it in `Code/<name>` and say
  so in the result message. At minimum, treat `Caches` and `.tmp` as ordinary
  folders here, and list what will be deleted before asking to confirm.
- **Related, also not changed:**
  - When `shutil.move` fails, the fallback is `safe_copy` then
    `_force_remove(original)`. The copy is not compared with the original
    before the original is deleted.
  - The move onto the reserved name has the same check-then-rename window as
    P1-10: `os.rename` silently replaces a file that appears at that name in
    between.
  - Files inside the project that are identical to a file already in the
    library are deleted instead of moved. This is safe: `resolve_destination`
    compares the two byte for byte first.

#### P1-05 (Low): preview deletes an archive staging folder

- **Location:** `src/scanner.py`, `Scanner.extract_gdrive_zip`, the `except`
  block.
- **What happens:** a real transfer unpacks Google Drive / Takeout archives into
  `<destination>/.organizer_staging/<archive>_<key>/` and keeps that folder
  when the run is cancelled, so the next run can resume without unzipping
  again. If preview then met the same archive and could not open it (for
  example a truncated download, or an archive that changed since), the error
  handler removed the staging folder with `rmtree`, even in preview. That
  breaks "preview writes nothing". If the destination is on the source drive,
  it is a write to the source drive (G2). The deleted data is only an
  extracted copy of the archive, so the practical impact is small.
- **Steps to reproduce:** start a real transfer of a source that contains
  `takeout-20230101-001.zip` and cancel it after extraction; replace the zip
  with a damaged one; run a preview into the same destination. The staging
  folder is gone.
- **Fix:** the error handler no longer removes the staging folder in preview.
- **Status:** Fixed. Tests in `tests/test_pass1_preview_staging.py`:
  - `test_preview_keeps_staging_folder_of_unreadable_archive` (fails on
    `92ed5c1`)
  - `test_preview_keeps_staging_folder_of_readable_archive` (guard; passes
    before and after)

#### P1-06 (Medium): a transfer with failures ends with "100% of files organized safely"

- **Location:** `src/api_organizer.py`, `OrganizerAPI.run()`: the final log
  line, the history record, and `process_single_file`.
- **What happens:** the run always ended with "All done! 100% of files
  organized safely." and was saved to the history as "Completed", even when
  files failed to copy (permission denied, FAT32 4 GB limit, iCloud
  placeholder) or a whole code project failed. The only sign was a warning
  somewhere above in a long log. Worse, a file that disappeared or became
  unreadable between the scan and the copy was dropped without any log line,
  record, or count, so Verify later reported a perfect transfer. The owner
  decides from that last line whether the source can be erased.
- **Steps to reproduce:** organize a folder containing a file you cannot read
  (`chmod 000`). The log shows a warning, then "All done! 100% of files
  organized safely.", and the history says "Completed".
- **Fix:** failed files and failed projects are counted. A run with failures
  now ends with "⚠️ Finished with problems: N file(s) and M code project(s)
  were not copied (see the warnings above). Do not erase the source until they
  have been copied." The history record gets `failed_files` and
  `failed_projects`, and the status "Completed with errors". A file that
  vanished after the scan is logged, counted, and recorded as failed, so Verify
  lists it. `run()` still returns `True` in this case, so the UI's
  success/error handling is unchanged (see "For later passes").
- **Status:** Fixed. Tests in `tests/test_pass1_completion_report.py`:
  - `test_failed_copy_is_not_reported_as_success` (fails on `92ed5c1`)
  - `test_file_that_vanishes_after_the_scan_is_reported` (fails on `92ed5c1`)
  - `test_clean_run_still_reports_success` (negative control)

#### P1-07 (High): parallel transfer workers crash the app with a segmentation fault

- **Location:** `src/file_ops.py`, `FileEngine.is_already_copied`,
  `is_content_duplicate` (the size probe), `get_project_copy`,
  `is_project_dissolved`.
- **What happens:** the four transfer worker threads share one SQLite
  connection, guarded by `FileEngine.lock`. macOS's system SQLite (which the
  Python 3.9 in `.venv` links against) is built with `THREADSAFE=2`, so a
  connection must never be used by two threads at the same time. These
  lookups kept their cursor in a local variable after `with self.lock:` ended.
  When the method returned, Python freed the cursor, and freeing a cursor that
  still holds an unfinished statement calls `sqlite3_reset()`. That ran outside
  the lock, at the same time as another worker's query on the same connection.
  The race is hit whenever a lookup finds a row, which is almost every lookup
  in a resumed or merge run. On the owner's drive (hundreds of thousands of
  files, four workers) a resumed transfer is very likely to crash. The copy
  itself is atomic, so a crash does not half-write a file, but the app dies
  mid-transfer and memory corruption inside SQLite could also damage the
  checkpoint database, which the repair logic relies on to decide what to
  delete. The race is how this finding surfaced: before the fix, the pass-1
  guarantee tests crashed `make test` about 1 run in 40 with
  `Segmentation fault: 11`, inside a resumed transfer.
- **Steps to reproduce:** record a few hundred files in a `FileEngine`, then
  call `is_already_copied()` on them from four threads in a loop. On
  `92ed5c1` the process segfaults in well under a second, every time.
- **Fix:** every cursor on the shared connection is now closed while the lock
  is still held (new helper `FileEngine._select_one`; `has_completed_owner` and
  `mark_project_dissolved` close theirs explicitly).
- **Status:** Fixed. Tests in `tests/test_pass1_checkpoint_thread_safety.py`:
  - `test_parallel_checkpoint_lookups_do_not_crash` runs the four-thread
    lookup loop for 2 seconds in a child process. On `92ed5c1` the child exits
    with -11 (SIGSEGV).
  - `test_every_cursor_is_closed_before_the_lock_is_released` is a
    deterministic check: it flags any cursor that is freed without being closed
    while the lock is not held. On `92ed5c1` it flags the lookups listed under
    Location.

#### P1-10 (Low): `copy_file` deletes a file that appears at its target during the copy

- **Location:** `src/file_ops.py`, `FileEngine.copy_file`, the final rename.
- **What happens:** `resolve_destination` reserves a free name, and
  `copy_file` renames the finished copy into place at the end. If a file had
  appeared at that name in the meantime (another app, a second organizer
  window, the owner saving a file), `copy_file` permanently deleted it with
  `_force_remove`, even clearing a Finder lock, and put its own copy there.
  Reaching this needs a race with another writer, so it is unlikely, but the
  lost file could be anything.
- **Steps to reproduce:** in a test, make the copy step write a different file
  to the target path before `copy_file` renames (see the test). The other file
  is replaced.
- **Fix:** `copy_file` now refuses: it raises `FileExistsError`, removes its
  own partial file, and the transfer records the file as failed (and, after
  P1-06, counts it). A tiny window remains between the check and the rename;
  closing it would need an exclusive rename (`renamex_np` with `RENAME_EXCL`),
  which is left as a recommendation. The same pattern (check, then `rename`)
  exists in the dissolve flow (P1-04) and is not changed.
- **Status:** Fixed. Test in `tests/test_pass1_copy_never_replaces.py`:
  `test_file_that_appears_at_the_target_during_the_copy_is_kept` (fails on
  `92ed5c1`).

### For later passes

- **Pass 3 (web UI):** after P1-06, a transfer with failures still returns
  success to the UI (`run()` returns `True`), so the UI shows its normal
  "complete" state. Only the log and history say "Finished with problems" /
  "Completed with errors". Consider showing a distinct warning state, and the
  new `failed_files` / `failed_projects` history fields.

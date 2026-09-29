# Drive Organizer: Full App Audit (2026-09)

Branch: `audit/full-app-audit-2026-09`, created from `main` at `92ed5c1`.

The audit runs in four sequential passes. Each pass adds its own section below.
A separate reviewer checks all passes at the end.

| Pass | Scope | Status |
|------|-------|--------|
| 1 | Data safety in the organize flow's backend | Complete |
| 2 | Duplicates utility | Complete |
| 3 | Web UI | In progress |
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
  From `src/cli.py`, only the transfer loop was read, because it also calls
  `copy_file`, which P1-10 changes.
- Reproduced each fixed finding with an automated test on synthetic data in a
  private temp folder that the test deletes afterwards. Crashes are simulated
  in a child process that calls `os._exit()` halfway through writing a file,
  so no `finally` block, `atexit` handler, or database commit runs. That is as
  close to a power cut or `kill -9` as a unit test can get. The findings that
  are not fixed (P1-04, P1-08, P1-09) were reproduced the same way with
  scratch scripts, which are not committed. Their entries give the exact
  steps.
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

#### P1-08 (Medium): auto-unzipped archives are not copied, and members the unzip skips are silently left behind

- **Location:** `src/scanner.py`: `Scanner.is_gdrive_zip` (the
  `_GDRIVE_TOKEN_RE` name match), `extract_gdrive_zip` with
  `_should_skip_zip_member_path`, and `scan_directory` (after unzipping, the
  archive itself is not added to the transfer list).
- **What happens:** any `.zip` whose name contains the word `takeout`,
  `gdrive`, `drive`, `icloud`, `onedrive`, `cloud` or `download` is unpacked,
  and only the unpacked files are organized; the archive itself is never
  copied. That matches personal archives too (`Hard Drive Backup.zip`,
  `download.zip`). Members the unzip skips are neither unpacked nor copied, and
  nothing reports them. The preview's "Skipped N build/cache folder(s)" line
  counts folders on disk only. The skipped members are:
  - anything inside a folder named in `SKIP_SYSTEM_DIRS` (`.git`,
    `node_modules`, `Caches`, `.tmp`, `venv`, `.cache`, ...) or `__MACOSX`;
  - "garbage" names (`._*`, `~$*`, `.DS_Store`, `Thumbs.db`, `desktop.ini`);
  - unsafe paths (zip-slip) and symbolic links.

  A code project inside an archive is not recognised as a project either: its
  files are scattered into categories and its `.git` is dropped. The run ends
  with "All done! 100% of files organized safely." Once the owner wipes the
  source, the skipped members are gone.
- **Steps to reproduce (confirmed on synthetic data):** in the source, create
  `takeout-20230101T000000Z-001.zip` with `Takeout/Drive/report.pdf`,
  `Takeout/Drive/Caches/important-notes.txt`, `Takeout/Drive/.tmp/draft.docx`,
  `Takeout/Drive/myproject/.git/config` and
  `Takeout/Drive/node_modules/patched/index.js`. Also create
  `Hard Drive Backup.zip` with `Documents/thesis.docx` and
  `Documents/venv/README-setup.md`. Preview, then organize. Only `report.pdf`
  and `thesis.docx` reach the destination. Neither archive is copied, and
  neither the preview nor the final summary mentions the rest.
- **Not fixed, owner decision:** replacing an archive by its contents is the
  intended design (the guarantee test
  `test_every_transferable_source_file_reaches_the_destination` lists the
  archive as not transferred by design). Keeping archives costs space on a
  nearly full drive, so the owner should choose.
- **Recommendation:** also copy the original archive (for example into an
  `Archives/` folder), at least whenever any member was skipped. Count skipped
  members in the preview and the final summary. Match only real export names
  (for example `takeout-*.zip`, `drive-download-*.zip`) or ask per archive.

#### P1-09 (Low): after a crash and resume, files are recorded as duplicates of their own copies

- **Location:** `src/file_ops.py`, `FileEngine.record_copy` (commits every 50
  rows) and `resolve_destination` (the identical-file check at the target
  name).
- **What happens:** copies are committed to the checkpoint database in batches
  of 50. After a crash, files that already reached their final names may have
  no committed record. On resume, `resolve_destination` finds an identical
  file at the target name, which is the file's own earlier copy, and records
  the source as `DUPLICATE_SKIPPED` with `duplicate_of` pointing at that copy.
  The duplicates list (`get_duplicate_records`) then offers those originals
  as duplicates, and Verify counts them as skipped duplicates. No data is lost:
  `trash_duplicates` re-checks that the twin exists, is a different file and
  is byte-identical before moving anything, and the organized copy is
  complete. But the numbers are wrong and the owner is invited to "clean up"
  files that are not duplicates.
- **Steps to reproduce (confirmed on synthetic data):** 12 source files; crash
  the transfer in a child process after 5 copies (`run_until_crash` in
  `tests/pass1_helpers.py`); the database has 0 rows. Resume with a merge
  organize: 5 sources are recorded as `DUPLICATE_SKIPPED`, each with
  `duplicate_of` set to its own copy (for example `batch1/file04.txt` ->
  `Documents/Text/file04.txt`), and `get_duplicate_records` returns 5.
- **Not fixed:** it is bookkeeping, not data loss, and the fix changes how
  resumes are recorded.
- **Recommendation:** when the identical file at the target name has no
  database record pointing to it, record it as this source's completed copy
  instead of a duplicate; or commit each record as it is written.

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

#### P1-11 (Medium): the preview does not report what a code-project copy leaves out

- **Location:** `src/scanner.py`, `Scanner.scan_directory` (the project-root
  branch that fills `skipped_dir_breakdown`); `src/file_ops.py`,
  `PROJECT_IGNORE_PATTERNS` (used by `copy_project_intact`).
- **What happens:** a code project is copied without anything whose name
  matches `PROJECT_IGNORE_PATTERNS`: `node_modules`, `venv`, `.venv`,
  `__pycache__`, `.cache`, `.next`, `.turbo`, `.firebase`, `.gradle`,
  `.cargo`, and also `build`, `dist` and `target`. The pattern applies to files
  as well as folders, and at every depth. The preview's "Skipped N build/cache
  folder(s), which will NOT be copied" line is meant to list exactly these
  (see the comment on `SKIP_BUILD_DIRS`), so that wiping the source after
  organizing loses nothing unexpectedly. But it listed a different set,
  `SKIP_BUILD_DIRS`. So `build/`, `dist/` and `target/` (for example a
  compiled thesis PDF, or the release that was shipped) and a build script
  named `build` were left out silently. Meanwhile `Caches` and `.tmp` were
  listed as not copied although they are. The run then ends with "All done!
  100% of files organized safely."
- **Steps to reproduce:** project `my_app` with `.git/HEAD`, `main.py`, a file
  `build`, `dist/MyApp-1.0.dmg`, `target/design-targets.xlsx`,
  `node_modules/pkg/index.js` and `Caches/notes.txt`. The preview reports only
  `node_modules x1` and `Caches x1`. Organizing leaves out `build`, `dist`,
  `target` and `node_modules`, and copies `Caches`.
- **Fix:** for the project's top level, the preview now reports exactly the
  names that match `PROJECT_IGNORE_PATTERNS`, files included, taken from the
  same constant the copy uses.
- **Not fixed (needs a decision):** matching names deeper inside a project
  (for example `frontend/node_modules` or `docs/build`) are still left out
  without being reported. Reporting them means walking every project during
  the preview, which is slow on a large drive. Options: log them during the
  real copy (the `ignore` callback sees every one) and in the final summary,
  or walk projects in the preview. Separately, the owner should decide whether
  `build`, `dist` and `target` should be left out at all: they are ordinary
  folder names.
- **Status:** Fixed for the top level. Test in
  `tests/test_pass1_project_skips_reported.py`:
  `test_preview_reports_exactly_what_the_project_copy_leaves_out` (fails on
  `92ed5c1`: `build`, `dist` and `target` are left out without being
  reported).

### Summary

| ID | Severity | Finding | Status | Commit | Tests in `tests/` |
|----|----------|---------|--------|--------|-------------------|
| P1-01 | Critical | Repair and auto-repair deleted the last copy of a file | Fixed | `79686ca` | `test_pass1_repair_keeps_last_copy.py` (3, plus 1 control) |
| P1-02 | High | Organizing deleted the owner's `*.tmp` files | Fixed | `d116d4f` | `test_pass1_partial_file_naming.py` (4) |
| P1-03 | High | A crash left a half-copied code project | Fixed | `7dc7e85` | `test_pass1_project_copy_atomic.py` (2) |
| P1-04 | High | Dissolve permanently deletes `.git` and skipped folders | Not fixed: owner decision | `bd36613` (report only) | None |
| P1-05 | Low | Preview deleted an archive staging folder | Fixed | `47da1c6` | `test_pass1_preview_staging.py` (1, plus 1 guard) |
| P1-06 | Medium | Failed transfers reported "100% organized safely" | Fixed | `fe2c9f1` | `test_pass1_completion_report.py` (2, plus 1 control) |
| P1-07 | High | SQLite race segfaulted parallel transfers | Fixed | `7ff333e` | `test_pass1_checkpoint_thread_safety.py` (2) |
| P1-08 | Medium | Auto-unzipped archives are not copied; skipped members are silently left behind | Not fixed: owner decision | `6ef6fc4` (report only) | None |
| P1-09 | Low | A resume records files as duplicates of their own copies | Not fixed: recommendation | `6ef6fc4` (report only) | None |
| P1-10 | Low | `copy_file` replaced a file that appeared at its target | Fixed | `9019385` | `test_pass1_copy_never_replaces.py` (1) |
| P1-11 | Medium | Preview did not report what a project copy leaves out | Fixed at the project's top level; deeper levels not fixed | `26b1822` | `test_pass1_project_skips_reported.py` (1) |

Other pass-1 commits:
- `ec70f48` started this report.
- `bfc744c` added the guarantee tests for G1 to G3 (`test_pass1_guarantees.py`,
  7 tests) and the shared fixtures in `tests/pass1_helpers.py`.
- The commit that adds this summary is the last pass-1 commit.

`make test` went from 122 tests at `92ed5c1` to 148: 26 new tests in 9 new
test files, plus the shared `tests/pass1_helpers.py`.
`tests/test_data_safety.py` also got one fixture change for P1-01.

#### Running the new tests against `92ed5c1`

Run from the repository root with this branch checked out. Any empty scratch
folder works for `W`.

```sh
W=$(mktemp -d)/base-92ed5c1
git worktree add --detach "$W" 92ed5c1
git archive audit/full-app-audit-2026-09 tests/pass1_helpers.py \
  $(git ls-tree --name-only audit/full-app-audit-2026-09 tests/ | grep 'tests/test_pass1_') \
  | tar -x -C "$W"
.venv/bin/python3 -c "import os,sys,unittest; os.chdir(sys.argv[1]); sys.path.insert(0, sys.argv[1]); unittest.main(module=None, argv=['unittest','discover','-s','tests','-p','test_pass1_*.py','-v'])" "$W"
git worktree remove --force "$W"
```

Result on 2026-09-29: 26 tests, 14 failures and 2 errors.
- Every fix test fails, each for the reason its finding describes.
- The 2 errors are P1-02's helper tests: `partial_path_for` does not exist on
  `92ed5c1`.
- The 10 tests that pass are expected to pass on both versions:
  - the 7 guarantee tests: G1 to G3 hold on `92ed5c1` in the scenarios they
    cover, and the violations are covered by the fix tests;
  - 3 controls: P1-01's truncated-copy control, P1-05's readable-archive
    guard, and P1-06's clean-run control.

On `92ed5c1` the P1-07 race can also crash the whole test process. That
happened in about 1 of 40 full-suite runs. If it happens, rerun.

#### Not verified, or only partly verified

- **P1-07 stress test:** it is probabilistic.
  - On `92ed5c1` it crashed on every attempt.
  - With the fix: no crash in 40 full-suite runs, or in about 380,000 lookups
    from the repro script.
  - A passing run cannot prove the race is gone. The deterministic cursor test
    is the real guard.
- **P1-04, P1-08 and P1-09:** these are not fixed, so there are no committed
  tests for them. Each was reproduced with a scratch script on synthetic data;
  the exact steps are in each entry.
- **Crashes:** simulated with `os._exit()` in a child process. Real power
  loss (disk write caches, file-system journaling) was not tested.
- **File systems:** every test ran in a temp folder on the Mac's internal APFS
  disk. No disk images were used, so behaviour on exFAT or FAT32, which is
  likely for the owner's external drive, was not exercised. That covers the
  `copy2` fallback, rename atomicity and `._*` files.
- **Scale:** nothing was run at the owner's scale (2 TB, about 1 TB of
  duplicates). The fixes add no extra scanning. P1-01 reads the source only
  for files whose copy does not match.
- **Remaining race windows:** P1-10 and the dissolve move still check, then
  rename. An exclusive rename would close them.

### For later passes

- **Pass 4 (CLI):** the CLI's transfer loop in `src/cli.py` has the pattern
  P1-06 fixed in `run()`: a file that disappears or cannot be read after the
  scan is skipped with `continue`, and is neither recorded nor counted
  (around the `os.path.getsize` call before `resolve_destination`). Check what
  its final summary says when files were not copied.

- **Pass 3 (web UI):** after P1-06, a transfer with failures still returns
  success to the UI (`run()` returns `True`), so the UI shows its normal
  "complete" state. Only the log and history say "Finished with problems" /
  "Completed with errors". Consider showing a distinct warning state, and the
  new `failed_files` / `failed_projects` history fields.

---

## Pass 2: Duplicates utility

### Scope

In scope: the standalone duplicates utility's backend and its local API
endpoints. That is `OrganizerAPI.find_duplicates_inplace` (the in-place scan,
grouping and hashing, which copy is kept, and what the scan skips),
`OrganizerAPI.trash_inplace_duplicates` (quarantine to `.Duplicates_Trash`
and permanent delete), `OrganizerAPI.empty_duplicates_trash`, and the
`/api/dup_scan_start`, `/api/dup_scan_status`, `/api/dup_scan_cancel`,
`/api/dup_trash_inplace`, `/api/dup_empty_trash` and `/api/dup_reveal`
routes in `src/app.py`. Also whether what the utility reports (space
reclaimed, counts) matches what happened on disk.

Out of scope (left to other passes): the organize flow (pass 1), the
duplicates screen's HTML/JS and the rest of the UI (pass 3), the CLI and API
auth (pass 4). The backend was still tested against requests the UI would
not normally send (every copy selected, stale paths, paths outside the
scanned folder, concurrent requests).

### Guarantees tested

- **G4.** No duplicates-utility operation deletes or quarantines the last
  remaining copy of a file, including when every copy is selected, or when
  files change between the scan and the delete.
- **G5.** The duplicate scan writes nothing to the drive being scanned.
- **G6.** On a volume with zero bytes free, the scan completes and permanent
  delete actually frees space. Tested on real, completely full disk images,
  one exFAT and one APFS.

### Method

- Read only what this scope needs: the three `OrganizerAPI` methods above,
  the helpers they call in `src/file_ops.py` (`get_part_hash`,
  `files_are_identical`, `_force_remove`, `_clear_immutable`), the skip lists
  in `src/scanner.py`, `Categorizer.is_project_root`, and the duplicates
  routes and scanner state in `src/app.py`. From `src/static/script.js`, only
  the request each duplicates button sends was read, to know what the backend
  receives.
- Each fixed finding has an automated test on synthetic data in a private
  temp folder, or on a small disk image that the test creates, attaches at a
  mount point inside its own temp folder (never under `/Volumes`), and
  detaches and deletes afterwards. Shared fixtures are in
  `tests/pass2_helpers.py`.
- Each fix is its own commit, together with a test that fails on `92ed5c1`
  and passes with the fix. `make test` passes at every commit.
- On `92ed5c1`, P2-01 makes every scan of a folder with a subfolder crash.
  So that the tests for the other findings exercise the baseline's real
  logic instead of stopping at P2-01, their helper (`make_api()`) sets the
  missing attribute on the API object. The P2-01 tests do not.

### Findings

Same severity scale as pass 1: **Critical** means data loss is likely in
normal use. **High** means data loss, or a broken guarantee, in a plausible
scenario. **Medium** means misleading results that could lead the owner to
delete data. **Low** means a narrow edge case or defence-in-depth.

Findings are numbered in the order they were identified, not by severity.
Commit hashes are listed in the summary table at the end of this section.

#### P2-01 (High): the duplicate scan crashes on any folder that has a subfolder

- **Location:** `src/api_organizer.py`, `OrganizerAPI.find_duplicates_inplace`.
- **What happens:** to skip code projects, the scan calls
  `self.categorizer.is_project_root(...)` for every folder below the scanned
  one. `OrganizerAPI` has no `categorizer` attribute (the other methods build
  a local `Categorizer`), so the first subfolder raises `AttributeError`. The
  scan thread catches it and the status becomes `error` with the message
  `'OrganizerAPI' object has no attribute 'categorizer'`. Only a folder with
  no subfolders at all could be scanned, so the utility never worked on a
  real drive, full or not (G6).
- **Steps to reproduce:** create `A/x.jpg` and `B/x.jpg` with the same
  content in a folder and scan it with the duplicates utility. The scan ends
  in an error.
- **Fix:** build a local `Categorizer(self.config_path)`, as the other
  methods do.
- **Status:** Fixed. Tests in `tests/test_pass2_scan_subfolders.py` (all
  three fail on `92ed5c1`):
  - `test_scan_finds_duplicates_in_subfolders`
  - `test_scan_still_skips_code_projects` (the project check now runs)
  - `test_scan_endpoint_completes` (through `/api/dup_scan_start`)

#### P2-02 (High): delete and quarantine remove a file that changed after the scan

- **Location:** `src/api_organizer.py`, `OrganizerAPI.trash_inplace_duplicates`
  (the check against the kept copy before each removal).
- **What happens:** the scan compares every byte, but a 2 TB scan takes
  hours and the owner acts on the results later. Before removing a file, the
  utility re-checked it against the copy being kept, but only compared the
  size and `get_part_hash`, which hashes the first and last 1 MB. A file
  changed in the middle after the scan, with the same size, still matched and
  was deleted or quarantined. That was the last copy of its new content. The
  same happened the other way round: if the kept copy changed in the middle,
  the selected copy (now the last copy of the original) was deleted.
  VeraCrypt containers, VM disks, disk images and databases change in exactly
  this way, and VeraCrypt keeps the container's modification time by default,
  so not even the date shows it.
- **Steps to reproduce:** create two identical 3 MB files in different
  folders and scan. Overwrite 4 KB in the middle of the second one, then
  permanently delete it from the results. It is deleted.
- **Fix:** compare every byte (`files_are_identical`) against the kept copy
  right before each removal. Anything that differs is kept and listed as
  "no longer matches original copy - kept safe."
- **Cost:** each removal now reads the selected file and the kept copy in
  full, as the scan already did. At the owner's scale (about 1 TB of
  duplicates) that adds hours to a delete on a USB hard drive. That is the
  price of not deleting on a guess; faster options are under
  "Recommendations" below.
- **Status:** Fixed. Tests in `tests/test_pass2_verify_full_content.py` (the
  first three fail on `92ed5c1`):
  - `test_permanent_delete_keeps_copy_changed_after_scan`
  - `test_quarantine_keeps_copy_changed_after_scan`
  - `test_delete_refused_when_kept_copy_changed_after_scan`
  - `test_unchanged_duplicate_is_still_deleted` (control, passes before and
    after)

#### P2-03 (High): the copy kept can be the one in the Windows or Linux trash, and the live file is deleted

- **Location:** `src/api_organizer.py`, `OrganizerAPI.find_duplicates_inplace`
  (which folders the scan skips, and `_original_sort_key`, which picks the
  copy to keep).
- **What happens:** the scan skips macOS's `.Trash` and `.Trashes`, but not
  the Windows Recycle Bin (`$RECYCLE.BIN`, `RECYCLER`) or the Linux desktop
  trash (`.Trash-1000`). An exFAT drive is exactly the kind that gets plugged
  into Windows and Linux machines, so these folders are common on it. Copies
  in them are grouped with the live files, and the keep heuristic often
  prefers the trashed one:
  - Windows renames a recycled file to something like `$R3XK2P1.JPG`. The
    heuristic treats a name ending in `_<digits>` as "looks like a copy", and
    camera names such as `IMG_1234.JPG` or `DSC_0001.JPG` all end that way.
    So the recycled copy always outranks the live photo.
  - A Linux trash copy keeps its name, so the shallower path wins:
    `.Trash-1000/files/IMG_2001.JPG` beats `Photos/2023/06/IMG_2001.JPG`.

  "Delete all redundant", and the default selection, which is every file
  but the kept one, then delete the live photo. The only copy left is in a
  trash folder that Windows or the Linux desktop empties on its own, or that
  the owner empties as junk. The same applies to other folders whose contents
  another program deletes on its own schedule: Syncthing's `.stversions`,
  Dropbox's `.dropbox.cache`, macOS's `.TemporaryItems`.
- **Steps to reproduce:** in the scanned folder, create
  `Photos/IMG_1234.JPG` and an identical
  `$RECYCLE.BIN/S-1-5-21-…-1001/$R3XK2P1.JPG`. Scan and delete all
  redundant copies. `Photos/IMG_1234.JPG` is deleted; the Recycle Bin copy
  is kept.
- **Fix:** the duplicates scan now also skips (case-insensitively) the
  Windows Recycle Bin (`$RECYCLE.BIN`, `RECYCLER`, `RECYCLED`), `System Volume
  Information`, Linux `.Trash-*`, macOS `.TemporaryItems`,
  `.DocumentRevisions-V100`, `.MobileBackups` and `Backups.backupdb`, and
  `.dropbox.cache` and `.stversions`. Nothing inside them is offered, and no
  copy in them can be the one kept. The shared skip lists in `scanner.py`
  are unchanged, so the organize flow is not affected (see the note under
  "For later passes").
- **Not fixed:** the keep heuristic itself still ranks `IMG_1234.JPG` as a
  copy. That is harmless once trash folders are skipped, but it is a poor
  signal; see the recommendations.
- **Status:** Fixed. Tests in `tests/test_pass2_skip_os_trash.py` (the first
  three fail on `92ed5c1`):
  - `test_windows_recycle_bin_copy_is_not_kept_instead_of_live_photo`
  - `test_linux_trash_copy_is_not_kept_instead_of_live_photo`
  - `test_other_self_emptying_folders_are_skipped`
  - `test_duplicates_in_ordinary_folders_are_still_found` (control, passes
    before and after)

#### P2-04 (Low): two removal requests at the same time can delete every copy

- **Location:** `src/api_organizer.py`, `OrganizerAPI.trash_inplace_duplicates`
  and `empty_duplicates_trash`, reached from `/api/dup_trash_inplace` and
  `/api/dup_empty_trash`.
- **What happens:** the app builds a new `OrganizerAPI` for every request,
  and Flask serves requests in parallel. Each removal checks that the copy it
  keeps still exists and matches, then removes its own selection. Nothing
  stopped two requests from interleaving: one request removing `b` (keeping
  `a`) and another removing `a` (keeping `b`) could both pass their checks
  before either removed anything, and together they removed both copies. The
  UI disables its buttons while a request runs, so normal use does not send
  two such requests. It takes a second request, sent while the first is
  still running, that keeps the very copy the first one deletes: for
  example after reloading the window during a long delete, which shows the
  old groups again. That is a narrow case (rated Low, like pass 1's race in
  P1-10), but it breaks G4, and the brief requires the backend to be safe
  whatever the UI sends.
- **Steps to reproduce:** scan a folder with one pair of duplicates. Send two
  `/api/dup_trash_inplace` requests at the same time, one selecting each
  copy. The test holds each request just before it deletes, until both have
  passed their checks, which makes the interleaving deterministic. Both
  copies are deleted.
- **Fix:** a process-wide lock (`OrganizerAPI._dup_removal_lock`) makes
  quarantine, permanent delete and emptying the trash run one at a time. The
  second request then sees that the copy it meant to keep is gone and
  refuses.
- **Status:** Fixed. Test in `tests/test_pass2_concurrent_removal.py` (fails
  on `92ed5c1`): `test_concurrent_requests_cannot_remove_every_copy`.

#### P2-05 (Medium): "freed X of disk space" counts file sizes, not the space actually freed

- **Location:** `src/api_organizer.py`, `OrganizerAPI.trash_inplace_duplicates`
  (permanent delete) and `empty_duplicates_trash`. The UI shows the number
  they return as "Successfully deleted N duplicate files and freed X of disk
  space!" and "Emptied .Duplicates_Trash: … freed X!".
- **What happens:** both added up the sizes of the files they deleted. On
  APFS, a copy made with Finder's Duplicate, or by copying within the same
  volume, is a clone that shares its blocks with the original. Deleting it
  frees almost nothing. The same is true of a file with another hard link
  outside the scanned folder, or a file that a snapshot still holds (Time
  Machine keeps local snapshots of APFS drives it backs up). On a real APFS
  image, deleting a 4 MB clone freed 0 bytes and the utility reported 4 MB
  freed. On a nearly full drive the owner would believe the space was
  recovered and act on that, for example by deleting more. exFAT has no
  clones, so this only affects an APFS drive.
- **Steps to reproduce:** on an APFS volume, make `Album/clip.mov` and
  `cp -c Album/clip.mov Backup/clip.mov`. Scan and permanently delete the
  copy: the utility reports the file's size as freed, and `df` shows no
  change. Emptying a `.Duplicates_Trash` that holds a clone does the same.
- **Fix:** both operations read the volume's free space before and after,
  and report what the drive actually gained, capped at the deleted files'
  total size. A gain within 1% (at least 1 MiB) of that size counts as all
  of it, so metadata blocks APFS allocates meanwhile are not reported as a
  shortfall. When the drive gained less, the log says so and gives the
  likely reasons. Anything else writing to the drive during the delete can
  only make the number lower, never higher. Quarantine still reports the
  size moved; the UI already says that no space is freed until the trash is
  emptied.
- **Behaviour change to note:** the number the UI shows after a permanent
  delete or emptying the trash is now the measured gain. On exFAT, or on
  APFS without clones, it is the same as before.
- **Status:** Fixed. Tests in `tests/test_pass2_reported_space.py`, run on a
  real 64 MB APFS disk image (the first two fail on `92ed5c1`):
  - `test_permanent_delete_of_clone_does_not_claim_space`
  - `test_emptying_trash_of_clone_does_not_claim_space`
  - `test_permanent_delete_of_real_copy_reports_its_size` (control, passes
    before and after)

#### P2-06 (Medium): results can belong to a different folder than the one the UI shows

- **Location:** `src/app.py`, `dup_scan_start` / `run_dup_scan` and
  `dup_trash_inplace`.
- **What happens:** two ways to act on one folder's results while believing
  they are another's:
  1. **Cancel, then scan another folder.** `dup_scan_start` only refused
     while the status was `running`. After a cancel the status is
     `cancelling` until the old scan reaches its next progress callback. Inside
     one comparison of two large videos that can take minutes. A new scan could
     start meanwhile, and starting it reset the shared cancel flag, so the old
     scan was no longer cancelled. It ran to the end and published its results
     over the new scan's: the UI showed folder A's duplicates as the results
     for folder B. `dup_trash_inplace` likewise only refused while `running`.
  2. **The folder named in the request was not checked.** `dup_trash_inplace`
     used the `root_folder` the UI sent, which is the value of the editable
     path field at the time of the click, for `.Duplicates_Trash`, without
     checking that the results were for that folder. Scan A, change the field
     to B, click Quarantine: A's duplicates were moved into
     `B/.Duplicates_Trash`. If B was on another drive, `shutil.move` copied
     each file across and deleted the original (since P2-11, quarantine
     only renames, and refuses a file on another drive).

  Each group still keeps one copy, so G4 holds, but the owner acts on files
  they did not mean to touch.
- **Steps to reproduce:** (1) start a scan of a large folder A, cancel it
  during a big file comparison, and start a scan of B; A's results appear
  when A's scan finishes. (2) scan A, change the path field to B, quarantine
  a duplicate; it lands in `B/.Duplicates_Trash`.
- **Fix:** the scanner state now records which folder the results belong to
  (`dup_root`) and which scan produced them (`dup_scan_id`).
  - A new scan is refused while the previous one is still `cancelling`.
  - A superseded scan stops at its next check and never publishes.
  - A removal request whose `root_folder` is not the scanned folder is
    refused with HTTP 409 and an error saying which folder the results are
    for. Paths are compared after resolving `~`, `..`, trailing slashes and
    symlinks.
  - A removal that finishes after a newer scan has started no longer writes
    its pruned groups over the new results.

  Emptying the trash still accepts any folder, because it does not use scan
  results.
- **Status:** Fixed. Tests in `tests/test_pass2_scan_state.py` (the first two
  fail on `92ed5c1`):
  - `test_cancelled_scan_cannot_replace_a_later_scans_results`
  - `test_quarantine_refuses_a_folder_other_than_the_scanned_one`
  - `test_quarantine_into_the_scanned_folder_still_works` (control, passes
    before and after)

#### P2-07 (Low): emptying a symlinked `.Duplicates_Trash` deletes whatever it points to

- **Location:** `src/api_organizer.py`, `OrganizerAPI.empty_duplicates_trash`
  (and quarantine in `trash_inplace_duplicates`).
- **What happens:** emptying the trash walks `<folder>/.Duplicates_Trash`
  with `os.walk`, which follows the top folder when it is a symbolic link,
  and deletes every file it finds. The final `shutil.rmtree` refuses the link
  but its error is ignored, so the call reports success. A link is plausible
  here: when the drive is full, the quarantine cannot create the folder, and
  pointing `.Duplicates_Trash` at a folder on another drive is an obvious
  workaround. If that folder also holds anything else, emptying the trash
  permanently deletes it. Quarantine moved files through the link as well,
  so they left the scanned folder, and with it the drive.
- **Steps to reproduce:** `ln -s ~/Documents/SomeFolder
  <scanned>/.Duplicates_Trash`, then click "Empty .Duplicates_Trash". The
  files in `SomeFolder` are deleted.
- **Fix:** a `.Duplicates_Trash` that is a symbolic link is neither emptied
  nor used for quarantine. Both return an error that says why. Links inside
  a real trash folder were already safe: they are removed, not followed.
- **Status:** Fixed. Tests in `tests/test_pass2_symlinked_trash.py` (the
  first two fail on `92ed5c1`):
  - `test_emptying_a_symlinked_trash_deletes_nothing_behind_it`
  - `test_quarantine_does_not_move_files_through_a_symlinked_trash`
  - `test_emptying_a_real_trash_folder_still_works` (control, passes before
    and after)

#### P2-08 (Low): emptying the trash reports success when files are left in it

- **Location:** `src/api_organizer.py`, `OrganizerAPI.empty_duplicates_trash`.
- **What happens:** every failed delete is ignored (`except OSError: pass`),
  and the final `shutil.rmtree(..., ignore_errors=True)` never raises, so the
  call returns no error however many files are left. The UI then says
  "Emptied .Duplicates_Trash: permanently deleted N files and freed X!", or,
  when nothing at all could be deleted, "No .Duplicates_Trash folder or
  quarantined files found in this directory". The quarantined copies are
  still on the drive, and the owner has no reason to look. Files cannot be
  deleted when the drive is mounted read-only (macOS does this after some
  file-system errors), when a failing drive returns I/O errors, or when the
  user is not allowed to delete them.
- **Steps to reproduce:** put two files in `<scanned>/.Duplicates_Trash`,
  one of them in a subfolder made read-only with `chmod 500`. Empty the
  trash. The result is success with one file deleted; the other file and the
  trash folder are still there.
- **Fix:** failures are still skipped, so one bad file does not stop the
  rest. Afterwards, if `.Duplicates_Trash` still exists, the call returns an
  error that gives the number of files left, the folder, and the first error,
  for example "1 file could not be deleted and is still in
  …/.Duplicates_Trash (first error: [Errno 13] Permission denied: …).
  Deleted 1 file." The endpoint then returns `success: false` with that
  message, and the UI shows it. The message includes the number deleted
  because the UI's error alert shows only the message.
- **Status:** Fixed. Tests in `tests/test_pass2_empty_trash_leftovers.py`
  (the first two fail on `92ed5c1`; all three are skipped when run as root,
  who can delete files in a read-only folder):
  - `test_files_left_in_trash_are_reported_as_an_error`
  - `test_endpoint_does_not_report_success_when_files_are_left`
  - `test_trash_that_empties_completely_still_reports_success` (control,
    passes before and after)

#### P2-09 (Medium): files inside many kinds of package are offered for deletion

- **Location:** `src/api_organizer.py`, `OrganizerAPI.find_duplicates_inplace`
  (`PACKAGE_BUNDLE_EXTS` and the folder pruning).
- **What happens:** the scan means to skip macOS packages, the folders the
  Finder shows as a single document, because deleting one file inside breaks
  the whole thing. It recognised them only by a short list of extensions:
  apps, Photos, iPhoto and Aperture libraries, Final Cut, Logic, GarageBand,
  Xcode projects, RTFD, frameworks and a few more. Not in the list:
  - VMware Fusion, Parallels and UTM virtual machines (`.vmwarevm`, `.pvm`,
    `.utm`) and sparse bundles (`.sparsebundle`, `.backupbundle`);
  - Scrivener projects (`.scriv`), and Pages, Numbers and Keynote documents
    saved as packages;
  - Lightroom and Capture One libraries (`.lrlibrary`, `.lrdata`,
    `.cocatalog`) and migrated iPhoto libraries (`.migratedphotolibrary`);
  - audio plug-ins, installer packages and other code bundles.

  Their internal files were grouped with other copies like any file, and
  "Delete all redundant" deleted whichever copy the keep heuristic ranked
  lower. That is usually the one inside the package, because it is deeper.
  Two examples:
  - A Pages document or a Lightroom library whose image also exists as a
    loose file loses its copy of the image.
  - The extents of a split VMware disk (`Virtual Disk-s001.vmdk`, `-s002`,
    …) that cover never-written parts of the disk can be byte-identical. All
    but one are deleted, and the virtual machine no longer opens.

  The kept copy is byte-identical, so the data could be put back, but only
  by someone who knows which names were deleted.
- **Steps to reproduce:** in the scanned folder, create
  `VMs/Windows 11.vmwarevm/` containing four identical files, `Virtual
  Disk-s002.vmdk` to `Virtual Disk-s005.vmdk`. Scan and delete all redundant
  copies. Three of the four are deleted.
- **Fix:**
  - The extension list now includes the package types above (the full list
    is in the code).
  - A folder laid out as a macOS bundle (`Contents/Info.plist`) is skipped
    whatever its extension, which covers plug-ins and apps with unusual
    extensions. The check costs one extra lookup, and only for folders that
    have a `Contents` subfolder.
  - As before, the scanned folder itself is never skipped: pointing the
    utility at a package is a deliberate choice.
  - `.hdd` (a Parallels disk) was left out on purpose. Parallels disks live
    inside `.pvm`, which is listed, and "Old Drive.hdd" is a plausible name
    for an ordinary backup folder, which would then be skipped without
    notice.
- **Behaviour change:** files inside these packages are no longer offered,
  so a duplicate copy of a whole virtual machine or library is not found
  either. Finding duplicate packages as whole units is left as a
  recommendation.
- **Not fixed:** folders that are not packages but whose files still belong
  together. Virtual machines made on Windows or Linux are plain folders
  (with a `.vmx` or `.vbox` file) and have the same split-extent risk. Code
  projects with none of the configured markers, and media that a catalog
  refers to by path, are also exposed. These need the owner's decision; see
  the recommendations.
- **Status:** Fixed for packages; folders that are not packages are not
  fixed (owner decision). Tests in `tests/test_pass2_package_skips.py` (the
  first three fail on `92ed5c1`):
  - `test_files_inside_packages_are_not_offered` (one subtest per package
    type added)
  - `test_split_vmware_disk_keeps_all_its_extents`
  - `test_unlisted_bundle_is_recognised_by_its_layout`
  - `test_ordinary_folders_are_still_scanned` (control, passes before and
    after)
  - `test_scanning_a_package_directly_still_looks_inside` (control, passes
    before and after)

#### P2-10 (Low): quarantine records nothing, and emptying the trash does not check that the kept copies still exist

- **Location:** `src/api_organizer.py`, quarantine in
  `OrganizerAPI.trash_inplace_duplicates`, and
  `OrganizerAPI.empty_duplicates_trash`.
- **What happens:** quarantine moves each file to the top level of
  `<folder>/.Duplicates_Trash` under its bare name (`a.jpg`, then `a_1.jpg`,
  and so on). Nothing records the folder it came from, or which copy was
  kept in its place. So:
  - there is no restore, in the app or by hand, short of searching the drive
    for each file's twin. The organize flow's duplicates have one
    (`restore_duplicates`); the standalone utility does not;
  - emptying the trash deletes everything in it without checking that each
    file's kept copy still exists. G4 is checked when a file is quarantined,
    but if the kept copy is deleted afterwards, by the owner or by another
    program such as a sync tool, emptying the trash deletes the last copy.
- **Steps to reproduce (scratch run):** create identical `Album/a.jpg` and
  `Backup/old/a.jpg`, scan, and quarantine `Backup/old/a.jpg`. Delete
  `Album/a.jpg` outside the app, then empty the trash. The result is success
  with 1 file deleted, and no copy of the file is left.
- **Why not fixed:** a fix needs a record written at quarantine time (for
  example a manifest inside `.Duplicates_Trash` with each file's original
  path, its kept copy and its size), a restore action, and a decision on
  what emptying should do with a file whose kept copy is gone: keep it and
  report it, or restore it. That is a new on-disk format and a change in
  behaviour, so it is the owner's decision. The record must also be written
  on an almost full drive, which constrains the design.
- **Severity:** Low. It takes a deletion outside the utility between the
  quarantine and the emptying. But the loss is permanent, and the flat trash
  gives the owner no way to notice beforehand.
- **Status:** Not fixed: owner decision. No committed test.

#### P2-11 (Low): a refused quarantine leaves a full copy in the trash, and permanent delete removes locked files without saying so

- **Location:** `src/api_organizer.py`, `OrganizerAPI.trash_inplace_duplicates`,
  and `_force_remove` in `src/file_ops.py`.
- **What happens:**
  - Quarantine moved files with `shutil.move`. When the rename fails,
    `shutil.move` copies the file into the trash and then deletes the
    original. If the rename failed because the file is locked in the Finder
    or its folder is read-only, the delete fails for the same reason. The
    file was reported as "could not be moved", but a full copy of it was left
    in the trash, still locked. The utility used more space instead of less.
    On a nearly full drive the copy fails with "No space left on device", so
    the message told the owner the drive was full and to use permanent
    delete, instead of naming the lock.
  - Permanent delete goes through `_force_remove`, which clears the Finder
    "Locked" flag and makes the file writable before deleting it. A locked
    file is the owner's explicit mark not to delete it, but locked
    duplicates are deleted like any other, and the result does not mention
    them. So quarantine refuses a locked file while permanent delete removes
    it silently. If the delete fails, the file is left unlocked with changed
    permissions.
- **Steps to reproduce:** create identical `Album/a.jpg` and
  `Backup/old/a.jpg`, lock the second (`chflags uchg`), and scan.
  Quarantining it reports "could not be moved ([Errno 1] Operation not
  permitted)" and leaves a locked copy in `.Duplicates_Trash`. Permanently
  deleting it instead reports 1 file deleted, with nothing refused.
- **Fix (stray copy):** quarantine now only renames (`os.rename`) within the
  drive, and never copies. A refused file leaves nothing in the trash, and
  the message gives the real error.
- **Behaviour change:** a file on a different volume from the trash (only
  possible when the scanned folder contains another volume's mount point)
  is now refused, with a message saying so. Before, it was copied into the
  trash on the scanned folder's volume, then deleted from its own.
- **Not fixed (locked files and permanent delete):** `_force_remove` is
  shared with the organize flow, and whether locked duplicates should be
  kept, listed or deleted is the owner's decision. Recommendation: have
  both actions of the duplicates utility refuse locked files with a message
  that names the lock, or at least list them in the result; and restore the
  flag and mode if a delete fails.
- **Status:** Stray copy fixed; deleting locked files not fixed (owner
  decision). Tests in `tests/test_pass2_refused_quarantine.py` (the first
  two fail on `92ed5c1`; the read-only-folder test is skipped when run as
  root):
  - `test_locked_file_is_refused_without_a_copy_in_the_trash`
  - `test_file_in_read_only_folder_is_refused_without_a_copy_in_the_trash`
  - `test_ordinary_duplicate_is_still_quarantined` (control, passes before
    and after)

### Recommendations

These need the owner's decision, or go beyond fixing a defect. P2-02, P2-03
and P2-09 point here; P2-10 and P2-11 carry their own recommendations.

1. **Faster checks before removal (P2-02).** Each removal now reads the
   selected file and the kept copy in full. Read the kept copy once per group
   instead of once per removed file: hash it in full once, then compare each
   selected copy's full hash with it. For groups with many copies that nearly
   halves the reading. Anything that avoids reading the selected file, such
   as trusting size and modification time, would reopen P2-02: VeraCrypt, for
   one, keeps a changed container's modification time, and exFAT records no
   separate change time to fall back on.
2. **Keep heuristic (P2-03).** `_original_sort_key` counts a name ending in
   `_<digits>`, `-<digits>` or ` <digits>` as a copy, and every camera name
   (`IMG_1234`, `DSC_0001`) ends that way. Count such an ending as a copy
   marker only when another file in the same group has the same name without
   it (`IMG_1234 2.JPG` next to `IMG_1234.JPG`). Keep treating ` copy` and
   `(1)` as copy markers.
3. **Folders whose files belong together but are not packages (P2-09).**
   - Virtual machines made on Windows or Linux are plain folders. Skip a
     folder that holds a `.vmx` or `.vbox` file, as packages are skipped.
   - Code projects are recognised only by the markers in `config.json`:
     `.git`, `package.json`, `requirements.txt`, `Cargo.toml`, `go.mod`,
     `pom.xml` and `build.gradle`. Projects with only `pyproject.toml`,
     `setup.py`, `CMakeLists.txt`, `Package.swift`, a `.sln` or `.csproj`, or
     `.hg` or `.svn`, and Xcode project folders without git, are scanned like
     any folder. The list is shared with the organize flow, so extending it
     changes that flow too.
   - Media that a catalog refers to by path: Lightroom Classic catalogs
     (`.lrcat`), Premiere Pro and DaVinci Resolve projects, Pro Tools and
     Ableton Live sessions, the Music (iTunes) library. Deleting the copy a
     catalog points to breaks the link, even though an identical copy
     survives elsewhere. At least warn when a group has a copy in a folder
     that also holds a catalog, or prefer keeping that copy.
4. **Duplicate packages as whole units (P2-09).** Packages are skipped, so a
   second copy of a whole virtual machine or photo library is not found,
   although it may be the biggest saving on the drive. A separate check could
   compare whole packages (or folders) file by file and offer the duplicate
   as one item.
5. **Metadata is not compared.** Files with the same bytes are duplicates
   even if only one of them has Finder tags, a Finder comment, "Where from"
   information, other extended attributes or a resource fork. Deleting that
   copy loses them. Prefer keeping the copy with more metadata, or at least
   show the difference.
6. **Files already gone are not reported.** A selected file that no longer
   exists when the removal runs is skipped silently: it is neither counted
   nor listed. Nothing is lost, but the result does not add up to the
   selection. List it as "already gone".
7. **Cancelling can be slow.** The scan checks for cancellation between
   files, not inside `files_are_identical`, which compares two files in
   full. On a USB hard drive, comparing two large videos can take minutes,
   and the cancel waits for it. P2-06 made that wait safe, but it is still
   slow. Pass a cancel check into the comparison loop.

### Summary

| ID | Severity | Finding | Status | Commit | Tests in `tests/` |
|----|----------|---------|--------|--------|-------------------|
| P2-01 | High | The duplicate scan crashed on any folder with a subfolder | Fixed | `89c021a` | `test_pass2_scan_subfolders.py` (3) |
| P2-02 | High | Delete and quarantine removed files that changed after the scan | Fixed | `6408d17` | `test_pass2_verify_full_content.py` (3, plus 1 control) |
| P2-03 | High | The Windows or Linux trash copy was kept and the live file deleted | Fixed | `012e9d3` | `test_pass2_skip_os_trash.py` (3, plus 1 control) |
| P2-04 | Low | Two removal requests at once could delete every copy | Fixed | `6307622` (rated Low in `459a306`) | `test_pass2_concurrent_removal.py` (1) |
| P2-05 | Medium | "Freed X" counted file sizes, not the space freed | Fixed | `cce1c2e` | `test_pass2_reported_space.py` (2, plus 1 control) |
| P2-06 | Medium | Results could belong to a different folder than the UI shows | Fixed | `755f5ff` | `test_pass2_scan_state.py` (2, plus 1 control) |
| P2-07 | Low | Emptying a symlinked `.Duplicates_Trash` deleted what it pointed to | Fixed | `176ab24` | `test_pass2_symlinked_trash.py` (2, plus 1 control) |
| P2-08 | Low | Emptying the trash reported success when files were left | Fixed | `2365371` | `test_pass2_empty_trash_leftovers.py` (2, plus 1 control) |
| P2-09 | Medium | Files inside many kinds of package were offered for deletion | Fixed for packages; other folders not fixed: owner decision | `dcacdf5` | `test_pass2_package_skips.py` (3, plus 2 controls) |
| P2-10 | Low | Quarantine records nothing; emptying the trash does not check the kept copies | Not fixed: owner decision | `ac7d803` (report only) | None |
| P2-11 | Low | A refused quarantine left a copy in the trash; permanent delete removes locked files silently | Stray copy fixed; locked files not fixed: owner decision | `ac7d803` | `test_pass2_refused_quarantine.py` (2, plus 1 control) |

Other pass-2 commits:
- `89c021a` (P2-01) also started this section and added the shared fixtures
  in `tests/pass2_helpers.py`.
- `459a306` re-rated P2-04 from Medium to Low.
- `e935431` added the guarantee tests for G4 to G6
  (`test_pass2_guarantees.py`, 13 tests) and two fixture fixes in
  `tests/pass2_helpers.py`.
- The commit that adds this summary is the last pass-2 commit.

`make test` went from 148 tests at `c8840b9` to 193: 45 new tests in 11 new
test files, plus the shared `tests/pass2_helpers.py`. No existing test was
changed. The source changes are all in `src/api_organizer.py` and
`src/app.py`.

Guarantees, with the fixes:
- **G4** holds in every scenario tested: every copy selected (directly and
  through the API), "delete all redundant", the kept copy deleted or
  replaced after the scan, a selected copy that grew or was replaced by a
  hard link to the kept copy, and stale paths or paths outside the scanned
  folder. On `92ed5c1` it was broken by P2-02 (changed files) and P2-04
  (concurrent requests), and in effect by P2-03. P2-10 is an open gap:
  emptying the trash does not re-check the kept copies.
- **G5** holds. An audit hook recorded no write calls under the scanned
  folder during a scan, called directly or through the API, and on the full
  volumes the scan changed neither the file tree nor the free space.
- **G6** holds on real, completely full 48 MB exFAT and APFS images: the scan
  completes, and both permanent delete and emptying the trash free the space
  they report (to within 64 KB of file-system overhead). "Completely full"
  means that a new 1-byte file cannot be created. exFAT then reports 0 bytes
  free; a 32 MB APFS image still reported about 1.25 MB, a reserve APFS
  keeps. Quarantine on a full drive is refused cleanly, because
  `.Duplicates_Trash` cannot be created, and the message points to permanent
  delete.

#### Running the new tests against `92ed5c1`

Run from the repository root with this branch checked out. Any empty scratch
folder works for `W`.

```sh
W=$(mktemp -d)/base-92ed5c1
git worktree add --detach "$W" 92ed5c1
git archive audit/full-app-audit-2026-09 tests/pass2_helpers.py \
  $(git ls-tree --name-only audit/full-app-audit-2026-09 tests/ | grep 'tests/test_pass2_') \
  | tar -x -C "$W"
.venv/bin/python3 -c "import os,sys,unittest; os.chdir(sys.argv[1]); sys.path.insert(0, sys.argv[1]); unittest.main(module=None, argv=['unittest','discover','-s','tests','-p','test_pass2_*.py','-v'])" "$W"
git worktree remove --force "$W"
```

Result on 2026-09-29: 45 tests, of which 26 fail, 2 error and 17 pass
(unittest reports "failures=47" because it counts each failed subtest).
- All 23 fix tests fail, each for the reason its finding describes. The 2
  errors are P2-01's direct tests (`AttributeError`).
- 5 of the 13 guarantee tests fail: the ones that go through the app's
  endpoints (G4 through the API twice, G5 through the API, and the two
  full-volume G6 tests). On `92ed5c1` the app's scan ends in "error" on any
  folder with a subfolder (P2-01), so there are no results to act on.
- The 17 that pass are expected to pass on both versions:
  - the other 8 guarantee tests: G4 and G5 hold on `92ed5c1` in the
    scenarios they cover when the scan is called directly, and the
    violations are covered by the fix tests;
  - 9 controls: one for each fixed finding except P2-01 and P2-04, and two
    for P2-09.

The run leaves no disk image attached (checked with `hdiutil info`).

#### Not verified, or only partly verified

- **Scale:** nothing was run at the owner's scale (2 TB, about 1 TB of
  duplicates). P2-02's full comparison reads each removed file and its kept
  copy again; on a USB hard drive that adds hours to a large delete. Not
  measured.
- **File systems:** exFAT was tested only on small images (16 to 48 MB),
  with the exFAT driver of macOS 26.6.2, which runs in FSKit and mounts with
  `noatime`. Earlier macOS versions use a different driver, and a 2 TB exFAT
  volume has much larger clusters than these images. FAT32 and NTFS were not
  tested. On exFAT, extended attributes live in `._` files; whether removing
  or quarantining a duplicate also removes or moves its `._` file was not
  checked.
- **G5's audit hook** sees writes made through Python. Writes the OS makes on
  its own, such as access times on a volume mounted without `noatime`, are
  not seen. On the full volumes, the scan left the free space unchanged.
- **P2-09:** that the unused extents of a split VMware disk are
  byte-identical follows from the VMDK sparse-extent format; it was not
  checked against a disk made by VMware. The package list covers common
  types, not all, and the `Contents/Info.plist` check only catches app-style
  bundles.
- **P2-10, and the locked-file half of P2-11:** not fixed, so there are no
  committed tests. Each was reproduced with a scratch script on synthetic
  data; the steps are in each entry.
- **Across processes:** the removal lock (P2-04) and the scan state (P2-06)
  are per process. Two copies of the app running at once are not
  serialized.
- **Crashes:** a crash or power loss in the middle of a removal was not
  simulated.
- **"Reveal in Finder":** read, not tested. It runs `open -R` (no shell) on
  any existing path it is given; see "For later passes".
- **The UI:** not exercised. Only the requests the duplicates screen sends
  were read (pass 3).

### For later passes

- **Organize flow (pass 1's area, for the final review):**
  - P2-03 added the Windows and Linux trash and the other self-emptying
    folders to the duplicates scan only. `SKIP_OS_DIRS` in `src/scanner.py`,
    which the organize flow uses, still has only `.Trash`, `.Trashes`,
    `.thumbnails`, `.fseventsd` and `.Spotlight-V100`, so organizing a drive
    that has been used on Windows or Linux presumably treats `$RECYCLE.BIN`
    and `.Trash-1000` as ordinary folders. Not verified.
  - `src/scanner.py` has no notion of packages (nothing like
    `PACKAGE_BUNDLE_EXTS`), so the organize flow may sort the files inside a
    `.photoslibrary`, `.app` or `.vmwarevm` into categories, which would
    break the package in the destination. Not verified.
  - `_force_remove` in `src/file_ops.py`, which clears the Finder "Locked"
    flag before deleting (P2-11), is used by the organize flow too.
- **Pass 3 (web UI):**
  - P2-06: removal now answers HTTP 409 when the path field no longer names
    the scanned folder. Check that the UI shows the message, and consider
    showing which folder the results belong to.
  - P2-08: a partly failed "Empty .Duplicates_Trash" now returns
    `success: false`. The UI's alert shows only `error`, which is why the
    message includes the number deleted; the UI could also show
    `bytes_freed`.
  - P2-03 and P2-09: files in OS trash folders and inside packages are no
    longer listed. The UI could say which kinds of folder are skipped, so
    the owner knows why a big library's duplicates do not appear.
- **Pass 4 (local API):**
  - `/api/dup_reveal` passes any existing path to `open -R`. A relative path
    that starts with `-` would be read by `open` as an option. That needs
    such a file in the app's working folder, but passing
    `os.path.abspath(path)` would rule it out. It also reveals any path, not
    only scan results.
  - `/api/dup_empty_trash` permanently deletes `<root_folder>/.Duplicates_Trash`
    for any existing `root_folder` in the request (by design; see P2-06). It
    only ever deletes a folder with that exact name. Check that only the app
    can call it.

---

## Pass 3: Web UI

### Scope

In scope: every screen of the web UI (welcome, setup, progress, dry-run
preview, duplicates, history and the post-transfer review tools), every button
and pop-up, in light and dark mode: `src/templates/index.html`,
`src/static/script.js` and `src/static/style.css`, and whether the UI shows
what the backend actually did, including the responses passes 1 and 2
changed. The backend was read only where the UI depends on it.

Out of scope (left to pass 4): the CLI, and the local API's auth and
endpoints as an attack surface.

### Method

- Automated browser tests drive the real app: `src/app.py`'s Flask app,
  served from a thread in the test process on 127.0.0.1 at a free port, in a
  Playwright browser at the app's window size (700 x 550). Every folder the
  UI works on is synthetic, in a private temp folder that the test deletes.
  The history file and the app's `base_dir` point into that folder, so
  History and "Clear History" never touch `src/run_history.json`, and
  `subprocess` in `src/app.py` and `src/api_organizer.py` is replaced by a
  recorder, so no Finder window, folder picker, `diskutil` run or sound is
  started. Fixtures are in `tests/pass3_helpers.py`.
- Every test fails on any JavaScript error in the console (uncaught
  exceptions and `console.error`). The browser's own "Failed to load
  resource" line for an HTTP error status is allowed only when the test
  provoked that status (the 409 and 500 responses from passes 1 and 2).
- **Engine:** pywebview draws the app with WebKit (WKWebView) on macOS.
  Playwright's WebKit build is ad-hoc signed and Santa blocks it on this Mac
  ("blocked from executing because its trustworthiness cannot be
  determined"), so the tests ran in Chromium (Chrome for Testing 148, which
  Santa allows). `DRIVE_ORGANIZER_UI_BROWSER=webkit` runs them in WebKit
  where it is allowed.
- **Tools:** Playwright 1.60.0 is listed in the new `requirements-dev.txt`,
  not in `requirements.txt`. It was installed into the app's `.venv` for this
  pass (`.venv/bin/pip install -r requirements-dev.txt`); it uses the
  browser builds already on this Mac (`chromium-1223`, `webkit-2287`), so no
  browser was downloaded. To remove it again:
  `.venv/bin/pip uninstall -y playwright pyee greenlet`. Without Playwright
  the browser tests are skipped, not failed.
- Each fix is its own commit, together with a test that fails on `92ed5c1`
  and passes with the fix. `make test` passes at every commit.

### Findings

Same severity scale as passes 1 and 2: **Critical** means data loss is
likely in normal use. **High** means data loss, or a broken guarantee, in a
plausible scenario. **Medium** means misleading results that could lead the
owner to delete data. **Low** means a narrow edge case or defence-in-depth.
Most UI defects cannot lose data by themselves; those are rated **Low** even
when a whole feature is broken, and the entry says what is broken.

Findings are numbered in the order they were identified, not by severity.
Commit hashes are listed in the summary table at the end of this section.

#### P3-01 (Low): the preview dashboard's buttons call functions that do not exist

- **Location:** `src/static/script.js`, `renderPreviewDashboard` and
  `inspectProject`.
- **What happens:** the dry-run dashboard was wired to functions that were
  never written. Clicking a category ("View samples") threw
  `showCategoryPreview is not defined`, "Inspect Ignored Files" threw
  `showGarbageModal is not defined`, and unticking a code project threw
  `toggleProjectExclusion is not defined`, so a project could not be marked
  to be split up from its checkbox. The project's inspect button called
  `inspectProject(path)` without the name, so its pop-up was titled
  "Inspect Folder: undefined". The working functions existed under other
  names (`inspectCategoryFiles`, `inspectGarbageFiles`,
  `onProjectCheckboxChange`). Known before the audit.
- **Steps to reproduce:** run a dry run on any folder with a code project
  and click a category, "Inspect Ignored Files" or a project checkbox.
- **Fix:** call the existing functions, and name the inspected folder from
  its path when no name is passed.
- **Status:** Fixed. Tests in `tests/test_pass3_preview_buttons.py` (all
  four fail on `92ed5c1`):
  - `test_category_button_fills_and_opens_the_samples_popup`
  - `test_inspect_ignored_files_fills_and_opens_the_breakdown`
  - `test_project_checkbox_marks_project_to_be_split_up`
  - `test_project_inspect_button_names_the_project`

  They check that each pop-up is filled and un-hidden; that it is actually
  visible is P3-03.

#### P3-02 (Medium): a folder name with an apostrophe breaks the project controls, and a crafted name runs code

- **Location:** `src/static/script.js`, `renderPreviewDashboard` (the code
  project rows).
- **What happens:** each project row put the folder's path inside a
  JavaScript string in an inline handler:
  `onchange="…('<path>', this.checked)"` and `onclick="inspectProject('<path>')"`.
  `escapeHtml` turns `'` into `&#39;`, but the HTML parser turns it back
  before the handler is compiled, so an apostrophe ends the string. For a
  project such as `Bob's app` (common on macOS), the checkbox and the
  inspect button threw `SyntaxError: missing ) after argument list` and did
  nothing. A folder name is also code: a project folder named
  ``evil');window.__ranFromFolderName=1;('`` runs
  `window.__ranFromFolderName=1` when its checkbox or inspect button is
  clicked, and marks the wrong path (`…/evil`) as split up. Any folder on a
  drive the owner is given can carry such a name, and the code runs in the
  page that holds the API token, which authorizes transfers, duplicate
  removal, "Clear History" and dissolving code projects in the destination
  (which deletes their `.git` history, P1-04). Rated Medium rather than High
  because it needs a crafted folder name that is also a code project, and a
  click. The apostrophe breakage was known before the audit; the code
  execution was not.
- **Steps to reproduce:** create `Projects/Bob's app/package.json` in a
  source folder, run a dry run, and untick the project or click its
  magnifier.
- **Fix:** the rows carry the path only in attributes (`value`,
  `data-path`), and the checkboxes and inspect buttons are wired with
  `addEventListener`, reading the path back from the element. No inline
  handler in the UI contains disk-derived text any more.
- **Status:** Fixed. Tests in `tests/test_pass3_quotes_in_paths.py` (all
  three fail on `92ed5c1`):
  - `test_checkbox_for_a_project_with_an_apostrophe`
  - `test_inspect_button_for_a_project_with_an_apostrophe`
  - `test_a_folder_name_cannot_run_code`

#### P3-03 (Low): the preview dashboard's pop-ups never appear

- **Location:** `src/templates/index.html` (the three pop-ups) and
  `src/static/style.css` (`.inspect-modal`).
- **What happens:** the pop-ups for a code project, the ignored files and a
  category's samples were written inside the progress view. Only the dry-run
  dashboard opens them, and the progress view is hidden while it shows, so
  opening one removed its `hidden` class and nothing appeared. Even with
  P3-01 fixed the buttons seemed dead, and the pop-up's "Sort This as Regular
  Files" button could not be reached at all. Moving them into the dashboard
  would not be enough: every `.view` keeps the `transform` left by its
  fade-in animation, which places a `position: fixed` child against the view
  instead of the window, and the card's `overflow-hidden` clips it. Once the
  pop-ups showed, a second defect appeared: leftover rules in `style.css`
  styled `.inspect-modal`, the full-window backdrop, as a card. The backdrop
  had rounded, bordered corners and, in dark mode, was opaque in the
  pop-up's own colour, so the page behind vanished and the pop-up had no
  visible edge. The nesting was known before the audit; the backdrop was not.
- **Steps to reproduce:** with P3-01 fixed, run a dry run and click a
  category, "Inspect Ignored Files" or a project's magnifier.
- **Fix:** the pop-ups are direct children of `<body>`, after `<main>`, and
  the card rules for `.inspect-modal` are removed; the Tailwind classes on
  the backdrop and the card already style both.
- **Status:** Fixed. Tests in `tests/test_pass3_modals.py` (all four fail on
  `92ed5c1`, and also on the P3-02 commit, which has P3-01's fix):
  - `test_popups_are_visible_in_light_mode`
  - `test_popups_are_visible_in_dark_mode`
  - `test_sort_as_regular_files_button_in_the_inspect_popup`
  - `test_popups_are_outside_every_view`

  "Visible" means displayed, inside the 700 x 550 window, and on top: the
  element at the centre of the pop-up belongs to it. The backdrop must be
  see-through with square corners; with the pop-ups moved but the old CSS,
  the dark-mode test fails with "backdrop is opaque: rgb(30, 41, 59)".

#### P3-04 (Low): the dry-run dashboard is outside the card's padding, and its footer is cut off and not pinned

- **Location:** `src/templates/index.html` (`#preview-view` and `<main>`) and
  `src/static/style.css`.
- **What happens:** every screen sits in the card's padded container except
  the dry-run dashboard, which was placed after it, directly in the card. Its
  content touched the card's left and right edges, under an empty 80 px band
  (the container's padding, with every screen in it hidden). The footer's
  negative margins, meant to cancel that padding, pushed the footer 40 px
  past both sides of the card, and the card's `overflow: hidden` cut off the
  right end of "Execute Full Transfer" (in the app's 700 x 550 window it
  read "Execute Full Transfe"). The footer is meant to stay pinned
  (`sticky bottom-0`), but `overflow: hidden` also makes the card the
  footer's scroll container, and the card never scrolls (the page does), so
  it was not pinned: on a long dashboard the owner had to scroll to the end
  to find the buttons. The nav bar's `sticky top-0` failed the same way. The
  misplaced dashboard and clipped footer were known before the audit; why
  the footer was not pinned was not.
- **Steps to reproduce:** run a dry run in the app's default window.
- **Fix:** `#preview-view` moved into the padded container, after the
  progress view. The card clips with `overflow: clip` instead of `hidden`
  (class `.app-card` in `style.css`): it still clips to the rounded corners
  but is not a scroll container, so the footer and the nav bar stick to the
  window. The nav bar now stays at the top of long screens, as its classes
  always asked. WebKit older than Safari 16 (2022) ignores `clip` and keeps
  `hidden`: there the footer is inside the card but not pinned.
- **Status:** Fixed. Tests in `tests/test_pass3_preview_layout.py` (all
  three fail on `92ed5c1`):
  - `test_footer_buttons_are_inside_the_card`
  - `test_footer_stays_in_the_window_while_scrolling`
  - `test_dashboard_lines_up_with_the_other_screens`

  With the dashboard moved but the card still `overflow: hidden`, the second
  test still fails.

#### P3-05 (Medium): a transfer with failures is reported as "Organization Complete!"

- **Location:** `src/static/script.js` (`startPolling`), `src/app.py`
  (`/api/status`, `run_organizer`) and `src/api_organizer.py` (`run()`).
- **What happens:** since P1-06 the organizer counts the files and code
  projects it could not copy, logs "⚠️ Finished with problems … Do not erase
  the source until they have been copied." and saves the run as "Completed
  with errors". But `run()` still returns `True`, so the status is
  `complete`, the progress screen's heading reads "Organization Complete!"
  and the review tools appear as after a clean run. `/api/status` carried no
  failure information, so the UI could not know. The only sign was one line
  in a long log. The owner decides from this screen whether the source can
  be erased (P1-06), so this is a misleading result that could lead to
  deleting data. Pass 1 listed it for pass 3.
- **Steps to reproduce:** organize a folder that holds a code project with
  one unreadable file (`chmod 000`). The project is not copied; the heading
  says "Organization Complete!".
- **Fix:** `run()` keeps the counts in `last_run_failures`, `run_organizer`
  copies them into the shared state, and `/api/status` returns them as
  `run_summary` (`failed_files`, `failed_projects`, and up to 20
  `failed_project_names`). When a full run ends with failures, the heading
  reads "⚠️ Finished with problems" and a red box under the progress bar
  says "N file(s) and M code project(s) were not copied. The warnings in the
  log say why. Do not erase the source until they have been copied.", and
  names the projects. `run()`'s return value and the `complete` status are
  unchanged.
- **Status:** Fixed. Tests in `tests/test_pass3_failed_transfer.py`:
  - `test_transfer_with_a_failed_project_is_not_reported_as_complete` (fails
    on `92ed5c1`)
  - `test_clean_transfer_is_still_reported_as_complete` (negative control)

#### P3-06 (High): the verification checker says the source is "100% safe to delete" without checking everything

- **Location:** `src/static/script.js` (`runVerificationChecker`,
  `verifyHistoryRun`). What it reports comes from `src/api_organizer.py`
  (`verify_transfer`).
- **What happens:** "Run Integrity Verification Checker" showed "Running 100%
  SHA-256 Hash & File Size Integrity Verification Check…", then "🟢 100%
  Integrity Verified & Safe to Delete!" and "It is now 100% safe to delete
  your original source folder!". `verify_transfer` checks much less:
  - Only files recorded one by one in the checkpoint's `copies` table.
    Code projects are copied as whole folders and recorded elsewhere, so
    they are never checked, and a project that failed to copy is recorded
    nowhere, so it cannot be reported missing.
  - Nothing the scan skipped on purpose (build and cache folders, whose
    names include ordinary ones such as `Caches`, `venv` and `.tmp`; system
    files) or could not read.
  - SHA-256 of a file's first and last 1 MB, not of the whole file, and only
    for the first 10,000 files; after that, size only. The owner's drive
    holds far more than 10,000 files.

  After the P3-05 transfer, where a code project was not copied, the checker
  still said the source was 100% safe to delete. Deleting it loses the
  project: data loss in a plausible scenario. Screenshot from before the
  fix: `audit_screenshots/pass3/evidence/p3-06-before-fix-verify-after-failed-project.png`.
- **Steps to reproduce:** as for P3-05, then click "Run Integrity
  Verification Checker".
- **Fix:** the result says what was checked: "✓ The copied files check
  out", the number of files checked, how many are at the destination with
  the right size, and how many also matched on their first and last 1 MB
  ("the rest by size only"; `verify_transfer` now also returns
  `hashes_checked` and `hash_check_limit`). A note lists what the check does
  not cover (code projects, skipped build or cache folders, ignored system
  files, anything the scan could not read) and asks the owner to check those
  before erasing the source. After a run with failures the result is amber,
  says what was not copied and "Do not erase the source until they have been
  copied." A check started from History takes the failure counts from that
  run's history record. The checker no longer says the source is safe to
  delete, and the loading line no longer claims a full SHA-256 check.
- **Not changed:** `verify_transfer` still does not check code projects or
  know about failed project copies; see Recommendations.
- **Status:** Fixed (what the UI claims). Tests in
  `tests/test_pass3_verification_claims.py` (all three fail on `92ed5c1`):
  - `test_after_a_failed_project_it_does_not_say_safe_to_delete`
  - `test_from_history_it_reports_that_runs_failures`
  - `test_after_a_clean_run_it_says_what_was_and_was_not_checked`

---


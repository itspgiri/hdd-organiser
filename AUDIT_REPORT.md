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

### For later passes

_Items noticed outside pass 1's scope are added here._

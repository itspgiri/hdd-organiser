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

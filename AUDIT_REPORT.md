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

_Findings are added below as each one is confirmed._

### For later passes

_Items noticed outside pass 1's scope are added here._

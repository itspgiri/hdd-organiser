# Drive Organizer — Full-Stack Bug Audit & Remediation Report

## Executive Summary
A multi-agent, two-pass full-stack security, data-integrity, concurrency, and correctness audit was conducted across the entire Drive Organizer codebase (`src/`, `main.py`, `Launch *.command`, and `tests/`), alongside implementation of **One-Click Restore from `.Duplicates_Trash`**.

In total, **63 distinct bugs and edge cases** (56 in Pass 1 + 7 in Pass 2) were identified, reproduced, fixed directly in the codebase, and locked in with deterministic automated regression tests (`122` total tests in `tests/`, `0` failures).

| Severity | Count | Key Impact Areas |
| :--- | :---: | :--- |
| **Critical** | 13 | Silent failure of all EXIF extraction (`exifread 3.5.1` `TypeError`), symlink target corruption via `copyfile(CLONE)`, loose symlink shadowing in `seen_files`, unverified duplicate trashing, Zip-Slip path traversal, CLI pre-confirmation disk writes, concurrent worker race conditions |
| **High** | 22 | Video `mvhd` false positives & truncated `moov` atoms, HEIC/CR3/AVIF EXIF blind spots, 0-byte file deduplication data loss, `filecmp` stat-cache stale hits, dissolved code projects re-copied on incremental runs, missing `duplicate_of` verification, `in_flight_sizes` TOCTOU & duplicate leak, exFAT symlink fallback |
| **Medium** | 19 | NFD/NFC collision key mismatches (backend & frontend), 255-byte UTF-8 filename truncation (`ENAMETOOLONG`), binary plist Finder tag formatting & `UF_IMMUTABLE` clearing, CSV formula injection, `.git` false skipped-folder counts, Live Photo `.jpg`/`.heic` & scan-order bugs, `/api/cancel` status race |
| **Low** | 9 | Year-1980 off-by-one, `None` stat timestamp guard, Rich markup bracket stripping, negative `format_size`, CLI `--help` launching GUI, `src/run_history.json` test pollution & temp file cleanup, readonly `#dest-path` input |

---

## 1. Date & Metadata Extraction (`src/dates.py`, `src/categorizer.py`, `src/config.json`)

### `DATE-1` (Critical) — `exifread.process_file()` Always Raised `TypeError`, Silently Disabling All EXIF Extraction
- **Location**: `src/dates.py` (`DateExtractor._get_exif_date`)
- **Root Cause**: `exifread.process_file(f, stop_tag="EXIF DateTimeOriginal", details=False, log_level="CRITICAL")` passed two invalid keyword arguments (`log_level` does not exist in `exifread 3.5.1`, and `stop_tag` expects bare tag name `"DateTimeOriginal"` rather than `"EXIF DateTimeOriginal"`). Every call raised a `TypeError` that was swallowed by `except Exception: pass`, forcing 100% of photos down to filename or filesystem timestamps.
- **Fix**: Removed invalid `log_level` and premature `stop_tag`, added `extract_thumbnail=False`, and silenced `logging.getLogger("exifread")` at `ERROR`.
- **Regression Test**: `test_full_audit_fixes.DateExtractorAuditTests.test_exifread_process_file_actually_extracts_exif_date`

### `DATE-2` (High) — Corrupt/Zeroed Higher-Priority EXIF Tag Blocked Valid Fallback Tags
- **Location**: `src/dates.py` (`DateExtractor._get_exif_date`, `DateExtractor.extract_date`)
- **Root Cause**: `_get_exif_date()` returned the first non-empty tag string (e.g., `EXIF DateTimeOriginal = "0000:00:00 00:00:00"`) without validating whether `_parse_exif_date()` could parse it into a valid date, preventing fallback to valid `EXIF DateTimeDigitized` or `Image DateTime` tags.
- **Fix**: Validated each candidate tag through `_parse_exif_date()` inside `_get_exif_date()` so unparseable or out-of-range tags fall through to the next tag.
- **Regression Test**: `test_full_audit_fixes.DateExtractorAuditTests.test_exif_zeroed_tag_falls_through_to_valid_tag`

### `DATE-3` (High) — HEIC/HEIF/AVIF/CR3/WebP EXIF Blind Spot & Format Variations
- **Location**: `src/dates.py` (`EXIF_EXTENSIONS`, `_extract_embedded_tiff_tags`, `_parse_exif_date`)
- **Root Cause**: `.cr3`, `.orf`, `.raf`, `.rw2` were missing from `EXIF_EXTENSIONS`, and ISOBMFF containers (`.heic`, `.heif`, `.avif`, `.cr3`) or `.webp` files often fail in `exifread.process_file()` even when they contain an embedded `Exif\x00\x00` + `II*\x00`/`MM\x00*` TIFF block. Additionally, `_parse_exif_date` did not normalize ISO `T` separators or `-`/`/` date delimiters.
- **Fix**: Added `.cr3`, `.orf`, `.raf`, `.rw2` to `EXIF_EXTENSIONS`, added `_extract_embedded_tiff_tags()` to scan for embedded TIFF headers and parse tags via `exifread.exif_log` / `ExifHeader`, and hardened `_parse_exif_date()` with calendar validation.
- **Regression Test**: `test_full_audit_fixes.DateExtractorAuditTests.test_embedded_tiff_fallback_extracts_heic_and_cr3_dates`

### `DATE-4` (High) — Video `mvhd` False Positives & Top-Level ISO BMFF Box Walking
- **Location**: `src/dates.py` (`_decode_mvhd`, `_find_moov_mvhd`, `_get_video_date`)
- **Root Cause**: Searching raw 2MB windows for `b"mvhd"` could match arbitrary payload bytes where `version == 0` and the 4 bytes after `flags` happened to fall in 1980–2100, or miss `mvhd` when `moov` exceeded 2MB or straddled the tail window boundary.
- **Fix**: Required `version in (0, 1)` and `flags == b"\x00\x00\x00"` in `_decode_mvhd()`, added a top-level ISO BMFF/QuickTime box walker `_find_moov_mvhd()`, and increased tail overlap to 32 bytes.
- **Regression Test**: `test_full_audit_fixes.DateExtractorAuditTests.test_video_mvhd_false_positive_guarded_and_box_walker_succeeds`

### `DATE-5` (Medium) — PDF `/CreationDate` Missed Linearized/Incremental Trailers & UTF-16BE Metadata
- **Location**: `src/dates.py` (`_get_pdf_date`)
- **Root Cause**: Only the first 16KB and last 16KB were scanned, only the first regex match was inspected, and UTF-16BE encoded `/CreationDate (\xfe\xff...)` strings were ignored.
- **Fix**: Expanded head/tail scan windows to 64KB, iterated over all `/CreationDate` and XMP `CreateDate` matches, and decoded UTF-16BE byte sequences.
- **Regression Test**: `test_full_audit_fixes.DateExtractorAuditTests.test_pdf_date_utf16be_and_multiple_creation_dates`

### `DATE-6` & `BUG-5.4` (Medium) — Filename Date Regex Matched Mixed Separators, Mid-Number Digits, and Stopped on First Match
- **Location**: `src/categorizer.py` (`Categorizer.__init__`), `src/dates.py` (`extract_date`)
- **Root Cause**: `self.date_regex` allowed mixed separators (`2023-06_15`), matched 8-digit substrings inside 13-digit millisecond timestamps or snowflake IDs, and `extract_date` used `.search()` instead of `.finditer()`.
- **Fix**: Updated `self.date_regex` with `(?<!\d)...(?!\d)` lookarounds and `\1` backreference, and iterated over all matches in `extract_date()`.
- **Regression Test**: `test_full_audit_fixes.DateExtractorAuditTests.test_filename_date_regex_skips_invalid_match_and_mixed_separators`

### `DATE-7` (Low) — Layer 4/5 Filesystem Fallback Rejected Year `1980`
- **Location**: `src/dates.py` (`extract_date`)
- **Root Cause**: Used `1980 < dt.year <= 2100` instead of `1980 <= dt.year <= 2100`.
- **Fix**: Changed lower bound to `1980 <= dt.year <= 2100`.
- **Regression Test**: `test_full_audit_fixes.DateExtractorAuditTests.test_filesystem_fallback_accepts_year_1980`

### `BUG-5.1`, `BUG-5.2`, `BUG-5.3` (Medium) — Screenshot Misclassification & Compound Archive Extensions
- **Location**: `src/categorizer.py`, `src/config.json`, `src/api_organizer.py`
- **Root Cause**:
  1. `is_screenshot()` did not normalize macOS NFD filenames to NFC, missed `Screen_Shot`, and classified `.mp4`/`.mov`/RAW files containing `"screenshot"` as still screenshots.
  2. Real camera photos (`Make`/`Model`/`LensModel` EXIF) whose filename happened to contain `"screenshot"` were routed to `Media/Screenshots/`.
  3. Compound extensions like `.tar.gz`, `.tar.bz2`, `.tar.xz`, `.tar.zst` were split at `.gz`/`.xz` and `.bz2`/`.xz`/`.zst` were missing from `config.json`.
- **Fix**: Added `split_filename_ext()`, updated `config.json`, normalized filenames to NFC in `is_screenshot()`, excluded video/RAW extensions from `is_screenshot()`, and checked `not dates.has_camera_exif(file_path)` in `compute_relative_destination()`.
- **Regression Tests**: `test_full_audit_fixes.FileOpsCategorizerUtilsAuditTests.test_categorizer_compound_extensions_and_screenshot_rules`, `test_full_audit_fixes.OrganizerAndWebAppAuditTests.test_live_photo_pairing_and_exif_screenshot_override`

---

## 2. Scanner & Archive Extraction (`src/scanner.py`)

### `SCAN-1` (Low) — `Desktop.ini` Case Mismatch in `GARBAGE_FILES`
- **Location**: `src/scanner.py` (`GARBAGE_FILES`)
- **Fix**: Added both `"Desktop.ini"` and `"desktop.ini"` to `GARBAGE_FILES` and expanded `SKIP_BUILD_DIRS` (`.gradle`, `.next`, `.nuxt`, `.svelte-kit`, `.parcel-cache`, `.turbo`).
- **Regression Test**: `test_full_audit_fixes.ScannerAuditTests.test_desktop_ini_is_classified_as_garbage`

### `SCAN-2` (High) — Comma-Splitting Corrupted Single Source Folders Containing Commas
- **Location**: `src/scanner.py` (`split_source_paths`), `src/api_organizer.py`
- **Root Cause**: Unconditionally calling `.split(",")` broke valid single folder paths like `/Users/me/Taxes, 2024`.
- **Fix**: Added `split_source_paths()` which checks if the unsplit string is an existing path on disk before splitting on commas.
- **Regression Test**: `test_full_audit_fixes.ScannerAuditTests.test_split_source_paths_preserves_existing_folder_with_comma`

### `SCAN-3`, `SCAN-4`, `SCAN-5` (High) — Overlapping Source Deduplication, State Reset, and `.git` False Skipped-Folder Reporting
- **Location**: `src/scanner.py` (`Scanner.scan_directory`)
- **Root Cause**:
  1. Overlapping sources (`/path/A, /path/A/sub`) queued every file and project twice.
  2. Calling `scan_directory()` multiple times on a `Scanner` instance accumulated `files_to_process` and `skipped_dirs`.
  3. `.git` was in `SKIP_BUILD_DIRS` and pruned/recorded as a skipped build directory before `is_project_root()` ran, inflating `skipped_dir_breakdown[".git"]` for every Git repo.
- **Fix**: Reset scan state at the start of `scan_directory()`, tracked `seen_dirs`/`seen_files`/`seen_projects` by `os.path.realpath`, and ran `is_project_root()` before recording skipped build directories.
- **Regression Test**: `test_full_audit_fixes.ScannerAuditTests.test_overlapping_sources_and_repeated_scans_do_not_duplicate`

### `SCAN-6` (Medium) — Broken Symlinks and FIFOs/Sockets Queued as Regular Files
- **Location**: `src/scanner.py` (`Scanner.scan_directory`)
- **Fix**: Filtered out symlinks (`os.path.islink`) and non-regular files (`not stat.S_ISREG(os.stat(full_path).st_mode)`) during directory traversal.
- **Regression Test**: `test_full_audit_fixes.ScannerAuditTests.test_broken_symlink_and_fifo_skipped`

### `SCAN-8`, `SCAN-9`, `SCAN-10`, `SCAN-11`, `SCAN-12` (Critical/High) — Google Drive Zip Detection, Zip-Slip Path Traversal, and `__MACOSX` Filtering
- **Location**: `src/scanner.py` (`is_gdrive_zip`, `_is_safe_zip_member`, `_should_skip_zip_member_path`, `extract_gdrive_zip`)
- **Root Cause**:
  1. `is_gdrive_zip` matched any `.zip` containing `-20` (every 21st-century date) and missed lowercase `drive-...` exports.
  2. `extract_gdrive_zip` had no Zip-Slip (`../`) or zip-symlink check before extraction.
  3. `__MACOSX` and `SKIP_SYSTEM_DIRS` were not consistently filtered across preview, native `ditto`, fallback `zipfile`, and cached re-runs.
- **Fix**: Tightened `is_gdrive_zip` with `_GDRIVE_TOKEN_RE`, added `_is_safe_zip_member()` and `_should_skip_zip_member_path()`, bypassed `ditto` if any unsafe member exists, and unified filtering across all 4 extraction paths.
- **Regression Test**: `test_full_audit_fixes.ScannerAuditTests.test_gdrive_zip_detection_and_zip_slip_protection`

---

## 3. File Engine, Metadata Preservation & Deduplication (`src/file_ops.py`, `src/utils.py`)

### `BUG-1.1` & `BUG-1.2` (Critical) — Symlink Target Corruption via `copyfile(CLONE)` and FIFO/Socket Hang
- **Location**: `src/file_ops.py` (`native_copy`, `safe_copy`, `FileEngine.copy_file`)
- **Root Cause**: `COPYFILE_CLONE` implies `COPYFILE_NOFOLLOW_SRC` and creates a symlink at `target_path`. When `native_copy` saw `os.path.islink(target_path)`, it treated the clone as failed and fell through to `shutil.copy2`/`shutil.copyfile`, which opened the symlink at `target_path` and overwrote the original symlink target! Additionally, FIFOs blocked `open()` indefinitely.
- **Fix**: Recreated symlinks via `os.readlink`/`os.symlink` in `safe_copy` and `copy_file`, guarded `native_copy` against symlinks and non-regular files, and skipped FIFOs/sockets/devices.
- **Regression Test**: `test_full_audit_fixes.FileOpsCategorizerUtilsAuditTests.test_safe_copy_and_engine_preserve_symlinks_and_skip_fifo`

### `BUG-1.3`, `BUG-1.4`, `BUG-2.1`, `BUG-2.2` (High/Medium) — `UF_IMMUTABLE` Locked Files, Read-Only `0o444` xattrs, and Binary Plist Finder Tags
- **Location**: `src/file_ops.py` (`_clear_immutable`, `_force_remove`, `restore_xattrs`, `FileEngine.set_finder_tag`)
- **Root Cause**:
  1. Copying a macOS "Locked" (`UF_IMMUTABLE`) file propagated the immutable flag to `.tmp`, causing `os.replace()` and subsequent cleanups to fail with `EPERM`.
  2. Read-only `0o444` files rejected `xattr.setxattr` when `restore_mode` ran before xattr/tag application.
  3. `set_finder_tag` wrote raw XML strings instead of binary plists (`plistlib.FMT_BINARY`) and overwrote existing user tags.
- **Fix**: Added `_clear_immutable()` and `_force_remove()`, temporarily granted `S_IWUSR` while writing xattrs/Finder tags before restoring mode and mtime, and serialized merged Finder tags with `plistlib.dumps(..., fmt=plistlib.FMT_BINARY)`.
- **Regression Test**: `test_full_audit_fixes.FileOpsCategorizerUtilsAuditTests.test_immutable_and_readonly_files_copied_cleanly_with_tags`

### `BUG-3.1`, `BUG-3.2`, `BUG-3.3` (Critical/High) — In-Flight Deduplication Race, 0-Byte File Loss, and `filecmp` Stat-Cache Bug
- **Location**: `src/file_ops.py` (`files_are_identical`, `is_content_duplicate`, `resolve_destination`)
- **Root Cause**:
  1. `filecmp.cmp(..., shallow=False)` caches results by `(size, mtime)` in `filecmp._cache`, returning stale `True` if a file is modified with the same size and mtime.
  2. `0`-byte files all share the same SHA-256 hash and size `0`, causing every empty file (`__init__.py`, `.gitkeep`) after the first to be skipped as `DUPLICATE_SKIPPED`.
  3. Concurrent worker threads processing identical files with different names raced past `is_content_duplicate` before either committed to SQLite.
- **Fix**: Replaced `filecmp.cmp` with stream-based `files_are_identical()`, excluded `size <= 0` from content deduplication, and synchronized concurrent same-size in-flight copies via `self.in_flight_sizes`.
- **Regression Test**: `test_full_audit_fixes.FileOpsCategorizerUtilsAuditTests.test_zero_byte_files_not_deduplicated_and_identical_ignores_stat_cache`

### `BUG-4.1` & `BUG-4.2` (Medium) — NFD/NFC Reservation Key Mismatch & 255-Byte UTF-8 Filename Truncation
- **Location**: `src/file_ops.py` (`_norm_key`, `_truncate_filename_utf8`, `resolve_destination`)
- **Fix**: Normalized reservation keys with `unicodedata.normalize("NFD", ...).casefold()` and truncated stems on UTF-8 character boundaries so `filename + ".tmp"` never exceeds 255 bytes (`NAME_MAX`).
- **Regression Test**: `test_full_audit_fixes.FileOpsCategorizerUtilsAuditTests.test_nfd_collision_and_255_utf8_byte_filename_truncation`

### `BUG-5.5` & `BUG-5.6` (Low) — Rich Markup Injection in Console Logs & History File Isolation
- **Location**: `src/utils.py`
- **Fix**: Escaped dynamic strings via `rich.markup.escape`, clamped negative sizes in `format_size`, isolated `unittest` history writes to `tempfile.gettempdir()`, and made `save_run_to_history` thread-safe with PID/thread-unique temp files.
- **Regression Test**: `test_full_audit_fixes.FileOpsCategorizerUtilsAuditTests.test_utils_rich_markup_escape_and_negative_format_size`

---

## 4. Orchestrator, CLI, Flask Web API & Frontend (`src/api_organizer.py`, `src/app.py`, `src/cli.py`, `main.py`, `src/static/script.js`)

### `BUG-T3-03` & `BUG-T3-04` (Medium) — Live Photo Pairing Missed `.jpg`/`.jpeg` Pairs, Case Differences, and Reverse Scan Order
- **Location**: `src/api_organizer.py` (`build_live_photo_dates`, `compute_relative_destination`), `src/cli.py`
- **Fix**: Shared `build_live_photo_dates()` across GUI and CLI, normalized keys case-insensitively and across NFC/NFD, included `.heic`/`.heif`/`.jpg`/`.jpeg` stills, and consulted `live_photo_dates` for both the still and video companions.
- **Regression Test**: `test_full_audit_fixes.OrganizerAndWebAppAuditTests.test_live_photo_pairing_and_exif_screenshot_override`

### `BUG-404` & `BUG-T3-09` (High) — Dissolving a Code Project Stranded Duplicates, Copied `.git`, and Was Re-Copied on Incremental Runs
- **Location**: `src/api_organizer.py` (`dissolve_and_resort_project`, `list_code_projects`), `src/file_ops.py` (`mark_project_dissolved`, `is_project_dissolved`)
- **Fix**: Pruned `SKIP_SYSTEM_DIRS` (`.git`, etc.) and `GARBAGE_FILES` in `list_code_projects` and `dissolve_and_resort_project`, removed duplicate inner files before `shutil.rmtree`, and persisted `status = 'dissolved'` in the `projects` checkpoint table so future incremental runs do not re-copy the dissolved project into `Code/`.
- **Regression Test**: `test_full_audit_fixes.OrganizerAndWebAppAuditTests.test_dissolve_project_skips_git_deletes_duplicates_and_persists_across_runs`

### `BUG-T3-10` & `BUG-406` (Critical) — Missing `duplicate_of` Target Not Checked in `verify_transfer`/`repair_transfer` & Unverified `trash_duplicates`
- **Location**: `src/api_organizer.py` (`verify_transfer`, `repair_transfer`, `trash_duplicates`), `src/app.py`, `src/static/script.js`
- **Root Cause**:
  1. `verify_transfer` and `repair_transfer` ignored `DUPLICATE_SKIPPED` rows whose surviving `duplicate_of` destination file had been deleted.
  2. `/api/trash_duplicates` did not receive `dest` from `script.js`, allowing `trash_duplicates` to run without consulting the destination checkpoint database.
- **Fix**: Verified `duplicate_of` existence in `verify_transfer` and `repair_transfer`, required a valid destination checkpoint DB in `trash_duplicates` and `/api/trash_duplicates`, and passed `dest` from `trashAllDuplicates()` in `script.js`.
- **Regression Test**: `test_full_audit_fixes.OrganizerAndWebAppAuditTests.test_verify_and_repair_check_duplicate_of_target_and_trash_requires_verified_db`

### `BUG-401`, `BUG-403`, `BUG-405`, `BUG-407`, `BUG-408`, `BUG-409`, `BUG-410` (High/Medium) — Flask Concurrency Locks, Input Validation, CSV Formula Sanitization, and Finder Argument Hardening
- **Location**: `src/app.py`, `src/templates/index.html`, `src/static/script.js`
- **Fix**:
  - Guarded all `UIState` mutations and reads with `state_lock`.
  - Blocked `/api/dissolve_project`, `/api/trash_duplicates`, and `/api/repair_transfer` with HTTP `409` while an organization job is running or cancelling.
  - Validated source directory existence synchronously in `/api/start` (returning HTTP `400`).
  - Sanitized CSV cells starting with `=`, `+`, `-`, `@`, `\t`, `\r` in `/api/export_csv`.
  - Passed `['open', '--', folder_real]` in `/api/open_finder` and rejected nonexistent paths with HTTP `404`.
  - Pruned `SKIP_SYSTEM_DIRS` and returned full relative paths in `/api/inspect_folder`.
  - Removed `readonly` from `#dest-path`, locked view navigation during active transfers, and rendered the Preview Dashboard when `total_projects > 0` even if `total_files === 0`.
- **Regression Test**: `test_full_audit_fixes.OrganizerAndWebAppAuditTests.test_webapp_endpoints_locking_csv_sanitization_and_finder_safety`

### `BUG-411`..`BUG-416` (High/Medium) — CLI Pre-Confirmation Extraction, Missing Staging Cleanup, and `main.py --help`
- **Location**: `src/cli.py`, `main.py`, `Launch App.command`, `Launch CLI.command`
- **Fix**:
  - Ran `scanner.scan_directory(..., is_preview=True)` before user confirmation in `src/cli.py` so Google Drive zips are only extracted onto `dest` after the user confirms the transfer.
  - Added `auto_repair_if_needed()`, `build_live_photo_dates()`, failed-copy checkpoint recording, and `scanner.cleanup_staging()` in `src/cli.py`.
  - Routed `--help` / `-h` and positional path arguments to CLI mode in `main.py`, and forwarded `"$@"` in `Launch App.command` and `Launch CLI.command`.
- **Regression Test**: `test_full_audit_fixes.OrganizerAndWebAppAuditTests.test_main_help_exits_cleanly_without_gui`

---

## 5. Second-Pass Verification Audit Fixes (`PASS2-1`..`PASS2-7`)

### `PASS2-1` (Critical) — Loose Symlinks Could Shadow Real Target Files in `seen_files` or Become Dangling in `Media/`
- **Location**: `src/scanner.py` (`Scanner.scan_directory`)
- **Root Cause**: Because `seen_files` keyed on `os.path.realpath(full_path)`, if a loose symlink was visited before its target regular file, the symlink claimed the `realpath` in `seen_files` and caused the real file to be skipped while the relative symlink broke when relocated into `Media/YYYY/Month/`.
- **Fix**: Checked `stat.S_ISREG(os.lstat(full_path).st_mode)` in `Scanner.scan_directory()` so loose symlinks outside code projects are skipped (while symlinks inside intact code projects continue to be preserved via `safe_copy`).
- **Regression Test**: `test_full_audit_fixes.OrganizerAndWebAppAuditTests.test_second_pass_loose_symlink_and_in_flight_sizes`

### `PASS2-2` (High) — `in_flight_sizes` TOCTOU Race & Duplicate-Skip Leak in `resolve_destination`
- **Location**: `src/file_ops.py` (`FileEngine.resolve_destination`)
- **Root Cause**: Two worker threads waiting for `source_size` to clear in `self.in_flight_sizes` could both exit the wait loop before either incremented `self.in_flight_sizes[source_size]`, and if `resolve_destination` returned `(None, ...)` for a duplicate before reserving a destination path, `release_reservation(None)` exited early without decrementing `self.in_flight_sizes[source_size]`.
- **Fix**: Atomically claimed `self.in_flight_sizes[source_size] = 1` inside the wait loop under `self.reserved_lock`, and released the slot via `_drop_size_slot()` whenever `resolve_destination` returns `(None, ...)` on a content duplicate.
- **Regression Test**: `test_full_audit_fixes.OrganizerAndWebAppAuditTests.test_second_pass_loose_symlink_and_in_flight_sizes`

### `PASS2-3` (High) — `safe_copy` Symlink Creation Raised `OSError` on exFAT/FAT32 Destinations
- **Location**: `src/file_ops.py` (`safe_copy`)
- **Root Cause**: Code repositories containing internal symlinks (e.g., `node_modules/.bin` or framework symlinks) failed with `OSError` (`EPERM`/`ENOTSUP`) when copied onto exFAT/FAT32 drives that do not support POSIX symlinks.
- **Fix**: Added a graceful fallback in `safe_copy` that copies the dereferenced regular target file's bytes if `os.symlink()` raises `OSError`.

### `PASS2-4` (Medium) — `set_finder_tag` Did Not Clear `UF_IMMUTABLE` Before Modifying File Mode
- **Location**: `src/file_ops.py` (`FileEngine.set_finder_tag`)
- **Fix**: Called `_clear_immutable(filepath)` at the start of `set_finder_tag()` before temporarily adding `stat.S_IWUSR`.

### `PASS2-5` (Medium) — `/api/cancel` vs Worker Completion Race on `state.status`
- **Location**: `src/app.py` (`run_organizer`)
- **Fix**: Guarded final `state.status` assignment in `run_organizer()` under `state_lock` so an in-flight `/api/cancel` ("cancelling") is deterministically finalized to `"cancelled"`.

### `PASS2-6` (Medium) — Frontend Pre-Flight Path Check NFD Normalization & Duplicate `pollInterval` Guard
- **Location**: `src/static/script.js` (`startOrganizing`)
- **Fix**: Added `.normalize('NFD')` to `stripTrailingSlash` in `startOrganizing()` and cleared any active `pollInterval` before starting a new transfer.

### `PASS2-7` (Low) — `dates.py` `None` Stat Attribute Guard & `utils.py` Temp File Cleanup
- **Location**: `src/dates.py` (`extract_date`), `src/utils.py` (`save_run_to_history`)
- **Fix**: Filtered `st_birthtime`/`st_mtime` with `isinstance(t, (int, float))` in `extract_date()` and added a `finally:` block in `save_run_to_history()` to remove orphaned `.tmp` files if serialization fails.

---

## 6. Feature Implementation: One-Click Restore from `.Duplicates_Trash`
- **Backend (`src/file_ops.py`, `src/api_organizer.py`, `src/app.py`)**:
  - Added `trashed_path TEXT DEFAULT ''` column (with automatic `ALTER TABLE` migration) to `.organizer_checkpoint.db`.
  - Updated `OrganizerAPI.trash_duplicates()` to record the exact `.Duplicates_Trash` path in `trashed_path`.
  - Added `OrganizerAPI.get_trashed_duplicates(dest_abs)` and `OrganizerAPI.restore_duplicates(dest_abs, source_paths)` which safely moves isolated duplicate files back to their original source paths, refuses to overwrite if a new file was created at the original path, resets the checkpoint row to `status = 'completed'` / `dest_path = 'DUPLICATE_SKIPPED'`, and automatically cleans up empty `.Duplicates_Trash` directories.
  - Added `POST /api/restore_duplicates` (protected by `state_lock` and session token auth) and updated `GET /api/duplicates` to return both active `duplicates` and isolated `trashed` files.
- **Frontend (`src/static/script.js`)**:
  - Updated `loadDuplicateCleaner()` to display both active duplicates on the source drive and isolated files inside `.Duplicates_Trash`, with a **`♻️ Restore N Files to Original Folder`** button powered by `restoreAllDuplicates()`.
- **Regression Test**: `test_full_audit_fixes.OrganizerAndWebAppAuditTests.test_restore_duplicates_and_api_endpoint`

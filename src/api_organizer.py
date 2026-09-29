import os
import time
import shutil
import sqlite3
import subprocess
import threading
import unicodedata
from concurrent.futures import ThreadPoolExecutor
from typing import Callable, Dict, Tuple, Set, List, Optional

from .categorizer import Categorizer, split_filename_ext
from .scanner import (
    Scanner, split_source_paths, SKIP_SYSTEM_DIRS, SKIP_ORGANIZER_DIRS,
    GARBAGE_FILES,
)
from .dates import DateExtractor
from .file_ops import (
    FileEngine, safe_copy, restore_timestamps, copy_project_intact,
    project_already_copied, files_are_identical, PROJECT_IGNORE_PATTERNS,
    _force_remove, _clear_immutable, PARTIAL_SUFFIX,
)

# Video containers Apple pairs with a still to make a Live Photo.
LIVE_PHOTO_VIDEO_EXTS = ("mov", "mp4")
LIVE_PHOTO_STILL_EXTS = ("heic", "heif", "jpg", "jpeg")


def _live_photo_norm_key(file_path: str) -> Tuple[str, str]:
    """Returns a case- and Unicode-normalized (directory, stem) key for Live Photo pairing."""
    dir_name, filename = os.path.split(file_path)
    name_only, _ext = split_filename_ext(filename)
    dir_norm = os.path.normcase(os.path.abspath(dir_name))
    stem_norm = unicodedata.normalize("NFC", name_only).lower()
    return (dir_norm, stem_norm)


def build_live_photo_dates(files_to_process: List[str], dates: DateExtractor) -> Dict[Tuple[str, str], Tuple[str, str]]:
    """Pre-computes authoritative capture dates for Live Photo pairs.

    Indexes both HEIC/HEIF stills and JPG/JPEG stills that share a folder and
    stem with a companion .mov/.mp4 video, storing both normalized and raw keys
    so neither the still nor the video needs to re-run extract_date.
    """
    video_keys: Set[Tuple[str, str]] = set()
    for fp in files_to_process:
        _, ext = split_filename_ext(os.path.basename(fp))
        if ext.lstrip(".").lower() in LIVE_PHOTO_VIDEO_EXTS:
            video_keys.add(_live_photo_norm_key(fp))

    live_photo_dates: Dict[Tuple[str, str], Tuple[str, str]] = {}
    for fp in files_to_process:
        filename = os.path.basename(fp)
        name_only, raw_ext = split_filename_ext(filename)
        ext = raw_ext.lstrip(".").lower()
        if ext not in LIVE_PHOTO_STILL_EXTS:
            continue
        norm_key = _live_photo_norm_key(fp)
        # Always index HEIC/HEIF, or JPG/JPEG when a companion video exists in the same directory
        if ext in ("heic", "heif") or norm_key in video_keys:
            y, m = dates.extract_date(fp)
            if y and m:
                dir_name = os.path.dirname(fp)
                live_photo_dates[norm_key] = (y, m)
                live_photo_dates[(dir_name, name_only)] = (y, m)
    return live_photo_dates


def _is_apfs_volume(path: str) -> bool:
    """Returns True if `path` resides on an APFS filesystem supporting copy-on-write clones."""
    try:
        res = subprocess.run(
            ["df", "-T", "apfs", path],
            capture_output=True,
            text=True,
            timeout=5,
        )
        return res.returncode == 0
    except Exception:
        return False


def compute_relative_destination(categorizer, dates, file_path: str,
                                 live_photo_dates: Optional[Dict[Tuple[str, str], Tuple[str, str]]] = None) -> str:
    """Works out a file's path relative to the destination root.

    Shared by the GUI (OrganizerAPI.run) and the CLI so the two cannot drift
    apart -- they previously carried two hand-maintained copies of this logic.
    """
    filename = os.path.basename(file_path)
    category = categorizer.get_file_category(filename)

    if category != "Media":
        return os.path.join(category, filename)

    live_photo_dates = live_photo_dates or {}
    name_only, raw_ext = split_filename_ext(filename)
    ext = raw_ext.lstrip(".").lower()
    raw_key = (os.path.dirname(file_path), name_only)
    norm_key = _live_photo_norm_key(file_path)

    year = month = None

    # Live Photo pairing, consulted BEFORE extract_date rather than after.
    #
    # The old guard was `if not (year and month): <consult pairing table>`,
    # but extract_date essentially always succeeds because it falls back to
    # filesystem mtime. So the table was never read, and a Live Photo whose
    # .mov had a reset mtime was filed years away from its own .heic.
    # The still's capture date is the authoritative one for the pair.
    if ext in LIVE_PHOTO_VIDEO_EXTS or ext in LIVE_PHOTO_STILL_EXTS:
        if norm_key in live_photo_dates:
            year, month = live_photo_dates[norm_key]
        elif raw_key in live_photo_dates:
            year, month = live_photo_dates[raw_key]

    if not (year and month):
        year, month = dates.extract_date(file_path)

    if categorizer.is_screenshot(filename) and not dates.has_camera_exif(file_path):
        if year and month:
            return os.path.join("Media", "Screenshots", year, month, filename)
        return os.path.join("Media", "Screenshots", "Unsorted", filename)

    if year and month:
        return os.path.join("Media", year, month, filename)
    return os.path.join("Unsorted", filename)


class OrganizerAPI:
    # Serialises the duplicates utility's removals (quarantine, permanent
    # delete, emptying .Duplicates_Trash) across every instance; see
    # trash_inplace_duplicates (audit P2-04).
    _dup_removal_lock = threading.Lock()

    def __init__(
        self,
        config_path: str,
        log_cb: Optional[Callable[[str], None]] = None,
        progress_cb: Optional[Callable[..., None]] = None,
    ):
        self.config_path = config_path
        self.log_cb = log_cb or (lambda msg: None)
        self.progress_cb = progress_cb or (lambda *args: None)
        self.cancelled = False

    def cancel(self):
        self.cancelled = True

    def _emit_progress(self, current: int, total: int, message: str, eta: str = ""):
        """Emits progress while remaining compatible with both 3-arg and 4-arg callbacks."""
        if not self.progress_cb:
            return
        if eta:
            try:
                self.progress_cb(current, total, message, eta)
                return
            except TypeError:
                pass
        self.progress_cb(current, total, message)

    @staticmethod
    def _split_sources(source_abs) -> List[str]:
        """Normalises the source argument (list, or comma-separated string)."""
        return split_source_paths(source_abs)

    @staticmethod
    def validate_paths(source_abs, dest_abs: str) -> Optional[str]:
        """Returns an error message if the source/destination pair is unsafe.

        Compares resolved real paths rather than raw strings: the old
        `dest.startswith(source)` test both rejected innocent siblings
        (/Volumes/Photos_Backup next to /Volumes/Photos) and could be walked
        around with symlinks or trailing separators. The GUI had no check at
        all, so a destination nested inside the source was accepted.
        """
        if not dest_abs or not str(dest_abs).strip():
            return "Safety Error: Destination path cannot be empty."
        sources = OrganizerAPI._split_sources(source_abs)
        if not sources:
            return "Safety Error: Source path cannot be empty."

        dest_expanded = os.path.expanduser(str(dest_abs).strip())
        dest_real = os.path.realpath(dest_expanded)
        dest_norm = unicodedata.normalize("NFD", dest_real).lower()

        if os.path.exists(dest_real) and not os.path.isdir(dest_real):
            return f"Safety Error: Destination '{dest_real}' is a file, not a folder."

        for src in sources:
            src_expanded = os.path.expanduser(str(src).strip())
            src_real = os.path.realpath(src_expanded)
            src_norm = unicodedata.normalize("NFD", src_real).lower()

            if src_real == dest_real or src_norm == dest_norm:
                return "Safety Error: Source and Destination are the same folder."
            try:
                if (
                    os.path.commonpath([dest_real, src_real]) == src_real
                    or os.path.commonpath([dest_norm, src_norm]) == src_norm
                ):
                    return (
                        f"Safety Error: Destination '{dest_real}' is inside source "
                        f"'{src_real}'. Choose a destination outside the source folder."
                    )
                if (
                    os.path.commonpath([dest_real, src_real]) == dest_real
                    or os.path.commonpath([dest_norm, src_norm]) == dest_norm
                ):
                    return (
                        f"Safety Error: Source '{src_real}' is inside destination "
                        f"'{dest_real}'. Choose a destination outside the source folder."
                    )
            except ValueError:
                # Different volumes - no containment possible.
                pass

            if os.path.exists(src_real) and not os.path.isdir(src_real):
                return f"Safety Error: Source '{src_real}' is a file, not a folder."

        return None

    def run(self, source_abs: str, dest_abs: str, is_preview: bool = False, dest_mode: str = "new", excluded_projects: list = None):
        self.cancelled = False
        mode_str = "Preview (Dry Run)" if is_preview else "Full Transfer"
        folder_type = "Brand New Folder" if dest_mode == "new" else "Merge with Existing Folder"
        self.log_cb(f"Mode: {mode_str} | Destination Strategy: {folder_type}")
        self.log_cb(f"Source: {source_abs}")
        self.log_cb(f"Destination: {dest_abs}")

        if not os.path.exists(self.config_path):
            self.log_cb("Error: config.json is missing!")
            return False

        path_error = self.validate_paths(source_abs, dest_abs)
        if path_error:
            self.log_cb(f"❌ {path_error}")
            return False

        dest_abs = os.path.expanduser(str(dest_abs).strip())
        sources_list = self._split_sources(source_abs)
        for src in sources_list:
            if not os.path.exists(src):
                self.log_cb(f"❌ Error: Source folder '{src}' does not exist.")
                return False
            if not os.path.isdir(src):
                self.log_cb(f"❌ Error: Source path '{src}' is not a directory.")
                return False

        categorizer = Categorizer(self.config_path)
        # Extract archives into a staging area on the destination volume, never
        # onto the source drive.
        staging_root = os.path.join(dest_abs, ".organizer_staging")
        scanner = Scanner(categorizer, staging_root=staging_root)

        self.log_cb(f"Scanning {source_abs} for files...")
        excluded_set = set(excluded_projects or [])
        scanner.scan_directory(
            sources_list,
            excluded_projects=excluded_set,
            progress_cb=self._emit_progress,
            cancel_check=lambda: self.cancelled,
            is_preview=is_preview,
        )

        if self.cancelled:
            self.log_cb("Operation cancelled by user. Progress saved.")
            return False

        total_bytes = 0
        unmeasured_files = 0
        category_counts: Dict[str, int] = {}
        category_samples: Dict[str, List[str]] = {}
        for fp in scanner.files_to_process:
            try:
                total_bytes += os.path.getsize(fp)
            except OSError:
                # In preview mode archives are not actually extracted, so their
                # members are *virtual* staging paths that do not exist on disk
                # yet. These used to silently contribute 0 bytes, which then fed
                # the free-space pre-flight check below and could wave through a
                # transfer that cannot fit.
                unmeasured_files += 1
            cat = categorizer.get_file_category(os.path.basename(fp))
            category_counts[cat] = category_counts.get(cat, 0) + 1
            if cat not in category_samples:
                category_samples[cat] = []
            if len(category_samples[cat]) < 25:
                category_samples[cat].append(fp)

        # Include intact code project sizes in total_bytes so free-space pre-flight checks are accurate
        project_bytes = 0
        ignore_fn = shutil.ignore_patterns(*PROJECT_IGNORE_PATTERNS)
        for proj_path in scanner.projects_found:
            for r, dirs, fs in os.walk(proj_path):
                ignored = ignore_fn(r, dirs + fs)
                dirs[:] = [d for d in dirs if d not in ignored]
                for f in fs:
                    if f in ignored:
                        continue
                    try:
                        project_bytes += os.path.getsize(os.path.join(r, f))
                    except OSError:
                        pass
        total_bytes += project_bytes

        estimated_archive_bytes = 0
        if unmeasured_files and is_preview:
            # Read the uncompressed sizes straight out of the zip directories.
            # Cheap: the central directory is metadata, not file content.
            import zipfile
            seen_zips = set()
            for zip_path in scanner.processed_zips:
                zp_real = os.path.realpath(zip_path) if os.path.exists(zip_path) else zip_path
                if zp_real in seen_zips:
                    continue
                seen_zips.add(zp_real)
                try:
                    with zipfile.ZipFile(zip_path, 'r') as zf:
                        estimated_archive_bytes += sum(
                            m.file_size for m in zf.infolist() if not m.is_dir()
                        )
                except Exception:
                    try:
                        # Fall back to the compressed size as a lower bound.
                        estimated_archive_bytes += os.path.getsize(zip_path)
                    except OSError:
                        pass
            total_bytes += estimated_archive_bytes

        from .utils import format_size, get_default_history_file, save_run_to_history
        size_str = format_size(total_bytes)
        self.log_cb(f"Found {len(scanner.files_to_process)} files ({size_str}) to organize.")
        if estimated_archive_bytes:
            self.log_cb(
                f"  (includes ~{format_size(estimated_archive_bytes)} estimated from "
                f"{len(scanner.gdrive_zip_names)} archive(s) that will be unzipped)"
            )
        self.log_cb(f"Found {len(scanner.projects_found)} intact code projects.")
        if scanner.ignored_garbage_count > 0:
            self.log_cb(f"Safely ignored {scanner.ignored_garbage_count} system/garbage files.")
        if scanner.skipped_dir_breakdown:
            detail = ", ".join(
                f"{name} x{count}"
                for name, count in sorted(scanner.skipped_dir_breakdown.items(),
                                          key=lambda kv: -kv[1])
            )
            self.log_cb(
                f"⏭️  Skipped {sum(scanner.skipped_dir_breakdown.values())} build/cache "
                f"folder(s), which will NOT be copied: {detail}"
            )

        free_space_str = "Unknown"
        free_bytes = None
        dest_parent = os.path.abspath(dest_abs)
        while dest_parent and not os.path.exists(dest_parent):
            parent = os.path.dirname(dest_parent)
            if parent == dest_parent:
                break
            dest_parent = parent
        try:
            free_bytes = shutil.disk_usage(dest_parent).free
            free_space_str = format_size(free_bytes)
            self.log_cb(f"Destination Drive Free Space: {free_space_str}")
        except OSError:
            pass

        # Hard block: refuse a fresh run that cannot possibly fit.
        # On a resume the destination may already hold most of the data, so warn only.
        if free_bytes is not None and total_bytes > free_bytes and not is_preview:
            # Check if all sources and destination are on the same APFS volume (for zero-copy cloning)
            same_volume = False
            try:
                dest_dev = os.stat(dest_parent).st_dev
                existing_sources = [src for src in sources_list if os.path.exists(src)]
                if existing_sources:
                    same_volume = all(os.stat(src).st_dev == dest_dev for src in existing_sources)
            except OSError:
                pass

            is_resume = os.path.exists(os.path.join(dest_abs, ".organizer_checkpoint.db"))
            msg = (
                f"Not enough free space on destination: need {size_str}, "
                f"only {free_space_str} available."
            )

            from .file_ops import _HAS_NATIVE_COPYFILE

            if is_resume:
                self.log_cb(f"⚠️  {msg} Continuing because this looks like a resumed transfer.")
            elif same_volume and _HAS_NATIVE_COPYFILE and _is_apfs_volume(dest_parent):
                self.log_cb(f"⚠️  {msg} Continuing because source and destination are on the same Mac APFS drive (cloning will use ~0 extra bytes).")
            else:
                self.log_cb(f"❌ Error: {msg}")
                scanner.cleanup_staging()
                return False

        project_details = [{"name": os.path.basename(p), "path": p} for p in scanner.projects_found]
        self.last_preview_summary = {
            "total_files": len(scanner.files_to_process),
            "total_size": size_str,
            "total_projects": len(scanner.projects_found),
            "projects": [os.path.basename(p) for p in scanner.projects_found[:10]],
            "project_details": project_details,
            "ignored_garbage": scanner.ignored_garbage_count,
            "garbage_breakdown": scanner.garbage_breakdown,
            "garbage_samples": scanner.garbage_samples,
            "skipped_dirs": scanner.skipped_dirs,
            "skipped_dir_breakdown": scanner.skipped_dir_breakdown,
            "gdrive_zips_extracted": scanner.gdrive_zips_extracted,
            "gdrive_zip_names": scanner.gdrive_zip_names,
            "free_space": free_space_str,
            "categories": category_counts,
            "category_samples": category_samples
        }

        if len(scanner.files_to_process) == 0 and len(scanner.projects_found) == 0:
            self.log_cb("Warning: No files found to move!")
            return True

        if is_preview:
            self._emit_progress(100, 100, "Preview Complete")
            self.log_cb("Preview Complete! No files were moved or altered.")

            # Save preview history
            try:
                from datetime import datetime
                history_file = get_default_history_file(os.path.dirname(self.config_path))
                timestamp_str = datetime.now().strftime("%B %d, %Y at %I:%M %p")
                run_record = {
                    "id": f"run_{int(time.time())}",
                    "timestamp": timestamp_str,
                    "source": source_abs,
                    "dest": dest_abs,
                    "is_preview": True,
                    "dest_mode": dest_mode,
                    "total_files": len(scanner.files_to_process),
                    "total_size": size_str,
                    "projects_count": len(scanner.projects_found),
                    "status": "Preview"
                }
                save_run_to_history(history_file, run_record)
            except Exception:
                pass

            return True

        # Execution
        try:
            os.makedirs(dest_abs, exist_ok=True)
            with open(os.path.join(dest_abs, ".metadata_never_index"), 'w') as f:
                f.write("")

            # Automatic pre-transfer health check & auto-repair
            self.auto_repair_if_needed(dest_abs)

            engine = FileEngine(dest_abs)
        except (OSError, IOError, sqlite3.OperationalError) as e:
            if getattr(e, 'errno', None) == 30 or "Read-only" in str(e) or "readonly" in str(e).lower():
                err_msg = (
                    f"Read-only file system on destination path '{dest_abs}'. "
                    "On macOS, external hard drives formatted as NTFS are read-only by default. "
                    "Please choose a writeable destination, reformat the drive to exFAT/APFS, or install an NTFS write driver."
                )
            else:
                err_msg = f"Cannot write to destination folder '{dest_abs}': {str(e)}"
            self.log_cb(f"❌ Error: {err_msg}")
            return False

        dates = DateExtractor(categorizer)
        engine.start_caffeinate()

        try:
            # Pre-pass: Index capture dates for Live Photo pairs
            live_photo_dates = build_live_photo_dates(scanner.files_to_process, dates)

            # Total items count for continuous progress calculation
            total_projects = len(scanner.projects_found)
            total_files = len(scanner.files_to_process)
            total_items = total_projects + total_files

            # 1. Code Projects
            # Failures are counted so the final summary cannot claim success
            # when something was not copied (audit pass 1, finding P1-06).
            failed_projects = []
            failed_files = []
            for i, proj in enumerate(scanner.projects_found):
                if self.cancelled:
                    self.log_cb("Operation cancelled by user. Progress saved.")
                    return False
                proj_name = os.path.basename(proj)
                dest_proj = os.path.join(dest_abs, "Code", proj_name)

                if engine.is_project_dissolved(proj):
                    self._emit_progress(i + 1, total_items, f"Skipped (Previously dissolved): {proj_name}")
                    continue

                # Projects are copied wholesale, so they need their own
                # "already done" check. Without it, re-running the same job
                # clones every repository again as project_1, project_2, ...
                previous = engine.get_project_copy(proj)
                if previous:
                    self._emit_progress(i + 1, total_items, f"Skipped (Already copied): {proj_name}")
                    continue

                # Collision handling for duplicate project folder names.
                # An existing folder that is already a faithful copy of this
                # project is a re-run, not a name collision.
                if os.path.exists(dest_proj):
                    if project_already_copied(proj, dest_proj):
                        engine.record_project(proj, dest_proj)
                        self._emit_progress(i + 1, total_items, f"Skipped (Already copied): {proj_name}")
                        continue
                    c = 1
                    while os.path.exists(os.path.join(dest_abs, "Code", f"{proj_name}_{c}")):
                        candidate = os.path.join(dest_abs, "Code", f"{proj_name}_{c}")
                        if project_already_copied(proj, candidate):
                            break
                        c += 1
                    dest_proj = os.path.join(dest_abs, "Code", f"{proj_name}_{c}")
                    if project_already_copied(proj, dest_proj):
                        engine.record_project(proj, dest_proj)
                        self._emit_progress(i + 1, total_items, f"Skipped (Already copied): {proj_name}")
                        continue

                self._emit_progress(i + 1, total_items, f"Copying project: {os.path.basename(dest_proj)}")
                ok, proj_err = copy_project_intact(proj, dest_proj)
                if ok:
                    engine.record_project(proj, dest_proj)
                else:
                    failed_projects.append(proj_name)
                    self.log_cb(f"Warning: Issue copying code project {proj_name}: {proj_err}")

            # 2. Files - Multi-Threaded Parallel Execution (4 Workers)
            completed_counter = [0]
            copied_this_run = [0]
            counter_lock = threading.Lock()
            start_time = time.time()

            def process_single_file(file_path):
                if self.cancelled:
                    return
                filename = os.path.basename(file_path)

                if engine.is_already_copied(file_path):
                    with counter_lock:
                        completed_counter[0] += 1
                        idx = total_projects + completed_counter[0]
                        if completed_counter[0] % 10 == 0 or completed_counter[0] == total_files:
                            self._emit_progress(idx, total_items, f"Skipped (Already copied): {filename}")
                    return

                rel_dest = compute_relative_destination(
                    categorizer, dates, file_path, live_photo_dates
                )

                target_base = os.path.join(dest_abs, rel_dest)

                try:
                    size = os.path.getsize(file_path)
                    mtime = os.path.getmtime(file_path)
                except OSError as stat_err:
                    # Gone or unreadable since the scan. It used to be dropped
                    # silently; say so, and record it so Verify reports it.
                    self.log_cb(f"Warning: Skipped {filename}: it disappeared or became unreadable after the scan ({stat_err}).")
                    engine.record_copy(file_path, "", 0, 0.0, part_hash="",
                                       status="failed: disappeared or unreadable after the scan")
                    with counter_lock:
                        failed_files.append(file_path)
                    return

                final_dest = None
                try:
                    final_dest, part_hash, twin = engine.resolve_destination(target_base, filename, size, file_path)
                    if final_dest is None:
                        engine.record_copy(file_path, "DUPLICATE_SKIPPED", size, mtime, part_hash,
                                           duplicate_of=twin)
                    else:
                        engine.copy_file(file_path, final_dest)
                        # Component test, not substring: a file literally named
                        # "Unsorted ideas.txt" must not be tagged To Review.
                        if "Unsorted" in rel_dest.split(os.sep)[:-1]:
                            engine.set_finder_tag(final_dest, "5", "To Review")
                        engine.record_copy(file_path, final_dest, size, mtime, part_hash)
                except Exception as file_err:
                    err_msg = str(file_err)
                    if "Errno 27" in err_msg or "File too large" in err_msg:
                        reason = "FAT32 file limit (>4GB)"
                        self.log_cb(f"⚠️ Skipped large file '{filename}': Target USB drive is formatted as FAT32 which has a 4 GB single file limit. Format drive as ExFAT or APFS to store files > 4 GB.")
                    elif "Errno 60" in err_msg or "timed out" in err_msg:
                        reason = "iCloud cloud-only timeout"
                        self.log_cb(f"☁️ Skipped cloud file '{filename}': File is stored in iCloud and not downloaded to your Mac yet. Click the download cloud icon in Finder next to this file, then re-run transfer.")
                    else:
                        reason = err_msg
                        self.log_cb(f"Warning: Skipped problem file {filename}: {err_msg}")

                    # Record the failed file so integrity check / re-sync track it.
                    # IMPORTANT: never record target_base here. Nothing was written to
                    # it, and another file may legitimately own that path -- recording
                    # it would make repair_transfer delete a healthy file.
                    engine.record_copy(file_path, final_dest or "", size, mtime, part_hash="", status=f"failed: {reason}")
                    with counter_lock:
                        failed_files.append(file_path)
                finally:
                    if final_dest:
                        engine.release_reservation(final_dest)

                with counter_lock:
                    completed_counter[0] += 1
                    copied_this_run[0] += 1
                    idx = total_projects + completed_counter[0]
                    elapsed = time.time() - start_time
                    eta_str = ""
                    if elapsed > 0.5 and copied_this_run[0] > 0:
                        rate = copied_this_run[0] / elapsed
                        rem_files = total_files - completed_counter[0]
                        rem_seconds = int(rem_files / rate)
                        if rem_seconds >= 3600:
                            h = rem_seconds // 3600
                            m = (rem_seconds % 3600) // 60
                            eta_str = f"⏳ ~{h}h {m}m remaining"
                        elif rem_seconds >= 60:
                            m = rem_seconds // 60
                            s = rem_seconds % 60
                            eta_str = f"⏳ ~{m}m {s}s remaining"
                        else:
                            eta_str = f"⏳ ~{rem_seconds}s remaining"

                    if completed_counter[0] % 5 == 0 or completed_counter[0] == total_files:
                        self._emit_progress(idx, total_items, f"Organizing: {filename}", eta_str)

            max_workers = 4
            self.log_cb(f"Using {max_workers} safe parallel worker threads for fast transfer.")

            with ThreadPoolExecutor(max_workers=max_workers) as executor:
                list(executor.map(process_single_file, scanner.files_to_process))

            # executor.map always drains the whole iterable; once cancelled the
            # workers just no-op, so the flag must be re-checked here or a
            # cancelled run would be reported as a success.
            if self.cancelled:
                self.log_cb("Operation cancelled by user. Progress saved.")
                return False

            scanner.cleanup_staging()

            self._emit_progress(total_items, total_items, "Complete")
            if failed_files or failed_projects:
                self.log_cb(
                    f"⚠️ Finished with problems: {len(failed_files)} file(s) and "
                    f"{len(failed_projects)} code project(s) were not copied (see the warnings above). "
                    "Do not erase the source until they have been copied."
                )
            else:
                self.log_cb("All done! 100% of files organized safely.")

            # Record history
            try:
                from datetime import datetime
                history_file = get_default_history_file(os.path.dirname(self.config_path))
                timestamp_str = datetime.now().strftime("%B %d, %Y at %I:%M %p")
                run_record = {
                    "id": f"run_{int(time.time())}",
                    "timestamp": timestamp_str,
                    "source": source_abs,
                    "dest": dest_abs,
                    "is_preview": False,
                    "dest_mode": dest_mode,
                    "total_files": len(scanner.files_to_process),
                    "total_size": size_str,
                    "projects_count": len(scanner.projects_found),
                    "failed_files": len(failed_files),
                    "failed_projects": len(failed_projects),
                    "status": "Completed with errors" if (failed_files or failed_projects) else "Completed"
                }
                save_run_to_history(history_file, run_record)
            except Exception:
                pass

            # Play the chime on a daemon thread so the child gets reaped.
            # A bare Popen here left an unreaped afplay process behind after
            # every completed transfer.
            def _chime():
                try:
                    subprocess.run(["afplay", "/System/Library/Sounds/Glass.aiff"],
                                   timeout=30)
                except Exception:
                    pass

            try:
                threading.Thread(target=_chime, daemon=True).start()
            except Exception:
                pass
            return True

        except Exception as e:
            self.log_cb(f"Error occurred: {str(e)}")
            return False
        finally:
            engine.close()

    def list_code_projects(self, dest_abs: str):
        """Returns a list of project folders inside dest_abs/Code/."""
        code_dir = os.path.join(dest_abs, "Code")
        if not os.path.exists(code_dir):
            return []
        projects = []
        for item in sorted(os.listdir(code_dir)):
            full_path = os.path.join(code_dir, item)
            if os.path.isdir(full_path) and item.lower() != "snippets" and not item.endswith(PARTIAL_SUFFIX):
                file_count = 0
                for _r, dirs, files in os.walk(full_path):
                    dirs[:] = [d for d in dirs if d not in SKIP_SYSTEM_DIRS and not d.startswith(".unzipped_")]
                    for f in files:
                        if f not in GARBAGE_FILES and not f.startswith(("._", "~$")):
                            file_count += 1
                projects.append({"name": item, "path": full_path, "file_count": file_count})
        return projects

    def dissolve_and_resort_project(self, dest_abs: str, project_folder_path: str):
        """
        Dissolves a project folder in Code/ and re-sorts all its contents into
        Media, Documents, etc.
        """
        # This path arrives from an HTTP request and this function ends in an
        # rmtree, so confine it to a real subfolder of <dest>/Code.
        code_root = os.path.realpath(os.path.join(dest_abs, "Code"))
        target = os.path.realpath(project_folder_path)
        if target == code_root or os.path.commonpath([code_root, target]) != code_root:
            return False, "Refused: project folder must be inside the destination's Code/ folder."

        if os.path.basename(target).lower() == "snippets":
            return False, "Refused: Code/Snippets is a category folder, not a code project."

        if not os.path.isdir(project_folder_path):
            return False, "Folder does not exist"

        categorizer = Categorizer(self.config_path)
        scanner_helper = Scanner(categorizer)
        dates = DateExtractor(categorizer)
        engine = FileEngine(dest_abs)

        try:
            files_to_move = []
            for root, dirs, files in os.walk(project_folder_path):
                # Do not dissolve .git or build/system caches into user categories!
                dirs[:] = [d for d in dirs if d not in SKIP_SYSTEM_DIRS and not d.startswith(".unzipped_")]
                for f in files:
                    if scanner_helper.is_garbage(f):
                        continue
                    files_to_move.append(os.path.join(root, f))

            live_photo_dates = build_live_photo_dates(files_to_move, dates)

            for file_path in files_to_move:
                filename = os.path.basename(file_path)
                rel_dest = compute_relative_destination(categorizer, dates, file_path, live_photo_dates)

                target_base = os.path.join(dest_abs, rel_dest)
                try:
                    size = os.path.getsize(file_path)
                    mtime = os.path.getmtime(file_path)
                except OSError:
                    continue

                final_dest = None
                try:
                    final_dest, part_hash, _twin = engine.resolve_destination(target_base, filename, size, file_path)
                    if final_dest:
                        os.makedirs(os.path.dirname(final_dest), exist_ok=True)
                        try:
                            shutil.move(file_path, final_dest)
                        except OSError:
                            safe_copy(file_path, final_dest)
                            _force_remove(file_path)
                        if "Unsorted" in rel_dest.split(os.sep)[:-1]:
                            engine.set_finder_tag(final_dest, "5", "To Review")
                        engine.record_copy(file_path, final_dest, size, mtime, part_hash)
                    else:
                        # Duplicate of an existing file in destination: remove the copy inside the project
                        # so it does not block rmtree of the dissolved project folder.
                        _force_remove(file_path)
                finally:
                    if final_dest:
                        engine.release_reservation(final_dest)

            # Only remove the folder if no user files are left in it. An unconditional
            # rmtree would destroy any user file that failed to move.
            leftovers = []
            for root, dirs, files in os.walk(project_folder_path):
                dirs[:] = [d for d in dirs if d not in SKIP_SYSTEM_DIRS and not d.startswith(".unzipped_")]
                for f in files:
                    if not scanner_helper.is_garbage(f):
                        leftovers.append(os.path.join(root, f))

            if leftovers:
                return True, (
                    f"Dissolved, but {len(leftovers)} file(s) could not be moved and were "
                    f"left in place at {project_folder_path}. Nothing was deleted."
                )

            def _handle_remove_readonly(func, path, _exc_info):
                _clear_immutable(path)
                try:
                    os.chmod(path, 0o700)
                except OSError:
                    pass
                try:
                    func(path)
                except OSError:
                    pass

            shutil.rmtree(project_folder_path, onerror=_handle_remove_readonly)
            engine.mark_project_dissolved(project_folder_path)
            return True, "Project dissolved and files re-sorted successfully!"
        except Exception as e:
            return False, str(e)
        finally:
            engine.close()

    def find_duplicates_inplace(self, folder_abs: str) -> List[dict]:
        """
        Scans a folder in-place for exact duplicate files with ZERO disk writes
        (safe even when the target drive has 0 bytes of free space).
        Groups by size -> partial hash -> full byte compare.
        Returns a list of groups sorted by wasted space descending:
        [{"size": int, "files": [keep_path, dup1, dup2, ...]}, ...]
        """
        import os
        import re
        import stat
        from .scanner import SKIP_SYSTEM_DIRS, SKIP_ORGANIZER_DIRS, GARBAGE_FILES
        from .file_ops import get_part_hash, files_are_identical, _is_regular_file

        self.cancelled = False
        self.log_cb(f"Scanning {folder_abs} for in-place duplicates (read-only)...")

        # OrganizerAPI has no `categorizer` attribute; reading self.categorizer
        # here raised AttributeError at the first subfolder, so the scan never
        # completed on a real drive (audit P2-01).
        categorizer = Categorizer(self.config_path)

        PACKAGE_BUNDLE_EXTS = (
            ".app", ".photoslibrary", ".photolibrary", ".aplibrary",
            ".fcpbundle", ".logicx", ".band", ".xcodeproj", ".xcworkspace",
            ".rtfd", ".framework", ".bundle", ".plugin", ".kext",
            ".imovielibrary", ".tvlibrary", ".musiclibrary",
        )

        # Folders whose contents another system deletes on its own: the
        # Windows and Linux trash, OS scratch and version stores, and sync
        # tools' caches and old versions. A copy in one of these must never be
        # the one that is kept (and nothing inside them is the owner's to
        # dedupe). Without this, the recycled "$R3XK2P1.JPG" in $RECYCLE.BIN
        # outranked the live "IMG_1234.JPG" (whose "_1234" looks like a copy
        # suffix), so the live photo was the one deleted (audit P2-03).
        # Compared case-insensitively; ".Trash-<uid>" is matched by prefix.
        OTHER_SYSTEM_DIRS = {
            "$recycle.bin", "recycler", "recycled", "system volume information",
            ".trash", ".trashes", ".temporaryitems", ".documentrevisions-v100",
            ".mobilebackups", "backups.backupdb",
            ".dropbox.cache", ".stversions",
        }

        size_groups: Dict[int, List[str]] = {}
        scanned_files = 0
        seen_dirs = set()
        seen_inodes = set()

        folder_abs = os.path.abspath(os.path.expanduser(folder_abs))

        for root, dirs, files in os.walk(folder_abs):
            if self.cancelled:
                return []

            try:
                root_real = os.path.realpath(root)
            except OSError:
                root_real = root
            if root_real in seen_dirs:
                dirs.clear()
                continue
            seen_dirs.add(root_real)

            unpruned_dirs = list(dirs)
            items_set = set(unpruned_dirs) | set(files)

            # Never reach inside an intact code repository and delete internal files
            if root != folder_abs and categorizer.is_project_root(root, items_set):
                dirs.clear()
                continue

            # Prune system dirs, organizer dirs, trash dirs, and macOS package bundles
            dirs[:] = [
                d for d in dirs
                if d not in SKIP_SYSTEM_DIRS
                and d not in SKIP_ORGANIZER_DIRS
                and not d.startswith(".unzipped_")
                and d != ".Duplicates_Trash"
                and not d.lower().endswith(PACKAGE_BUNDLE_EXTS)
                and d.lower() not in OTHER_SYSTEM_DIRS
                and not d.lower().startswith(".trash-")
                and not os.path.islink(os.path.join(root, d))
            ]

            for f in files:
                if self.cancelled:
                    return []
                if f in GARBAGE_FILES or f.startswith(("._", "~$")):
                    continue
                path = os.path.join(root, f)
                try:
                    st = os.lstat(path)
                    if not stat.S_ISREG(st.st_mode):
                        continue
                    sz = st.st_size
                    if sz <= 0:
                        continue
                    # Skip hardlinks to the exact same physical inode
                    if st.st_ino and st.st_dev:
                        inode_key = (st.st_dev, st.st_ino)
                        if inode_key in seen_inodes:
                            continue
                        seen_inodes.add(inode_key)

                    size_groups.setdefault(sz, []).append(path)
                    scanned_files += 1
                    if scanned_files % 300 == 0:
                        self._emit_progress(
                            0, 0,
                            f"Phase 1/2: Scanning drive... ({scanned_files:,} files inspected)"
                        )
                except OSError:
                    pass

        candidate_sizes = {sz: paths for sz, paths in size_groups.items() if len(paths) > 1}
        total_candidates = sum(len(paths) for paths in candidate_sizes.values())
        self.log_cb(
            f"Found {len(candidate_sizes):,} file sizes ({total_candidates:,} files) with potential duplicates. Hashing..."
        )

        copy_Copy_re = re.compile(r"(?:\bcopy\b|\(\d+\)|[ _-]\d+)$", re.IGNORECASE)

        def _original_sort_key(p: str):
            stem = os.path.splitext(os.path.basename(p))[0]
            looks_like_copy = 1 if copy_Copy_re.search(stem) else 0
            depth = p.count(os.sep)
            return (looks_like_copy, depth, len(p), p)

        duplicate_groups = []
        processed = 0

        # Check largest candidate sizes first so progress reflects heavy I/O accurately
        for sz, paths in sorted(candidate_sizes.items(), key=lambda kv: kv[0], reverse=True):
            if self.cancelled:
                break
            hash_groups: Dict[str, List[str]] = {}
            for p in paths:
                if self.cancelled:
                    break
                phash = get_part_hash(p, sz)
                if phash:
                    hash_groups.setdefault(phash, []).append(p)
                processed += 1
                if processed % 25 == 0 or sz >= 50 * 1024 * 1024:
                    self._emit_progress(
                        processed,
                        max(1, total_candidates),
                        f"Phase 2/2: Verifying duplicates... ({processed:,} / {total_candidates:,} candidate files)"
                    )

            for phash, hpaths in hash_groups.items():
                if len(hpaths) > 1:
                    exact_groups: List[List[str]] = []
                    for hp in hpaths:
                        if self.cancelled:
                            break
                        matched = False
                        for eg in exact_groups:
                            if files_are_identical(hp, eg[0]):
                                eg.append(hp)
                                matched = True
                                break
                        if not matched:
                            exact_groups.append([hp])

                    for eg in exact_groups:
                        if len(eg) > 1:
                            eg_sorted = sorted(eg, key=_original_sort_key)
                            duplicate_groups.append({"size": sz, "files": eg_sorted})

        duplicate_groups.sort(key=lambda g: g["size"] * (len(g["files"]) - 1), reverse=True)
        self.log_cb(f"Found {len(duplicate_groups):,} duplicate groups.")
        return duplicate_groups

    def trash_inplace_duplicates(
        self,
        source_paths: list,
        root_folder: str,
        permanent_delete: bool = False,
        groups: Optional[List[dict]] = None,
    ) -> Tuple[int, list, int]:
        """
        Removes or quarantines duplicate files in-place on the scanned drive.
        - If permanent_delete=True: unlinks files directly (_force_remove) to immediately
          free space on a 100% full drive without needing a single free byte.
        - If permanent_delete=False: moves files into root_folder/.Duplicates_Trash/.
        - Every file is re-verified against a surviving unselected twin from its group
          before removal so the user can NEVER delete all copies of a file.
        Returns (removed_count, refused_list, bytes_reclaimed). For a permanent
        delete, bytes_reclaimed is the free space the drive actually gained,
        capped at the deleted files' total size; for a quarantine it is the
        total size of the files moved (no space is freed until the trash is
        emptied).
        """
        # The app builds a new OrganizerAPI per request and Flask serves
        # requests in parallel. Two requests keeping different copies of the
        # same file could both pass the "surviving twin exists" check before
        # either removed anything, and together remove every copy (audit
        # P2-04). Removals therefore run one at a time, process-wide.
        with OrganizerAPI._dup_removal_lock:
            return self._trash_inplace_duplicates_locked(
                source_paths, root_folder, permanent_delete, groups
            )

    def _trash_inplace_duplicates_locked(
        self,
        source_paths: list,
        root_folder: str,
        permanent_delete: bool,
        groups: Optional[List[dict]],
    ) -> Tuple[int, list, int]:
        """trash_inplace_duplicates' body; the caller holds _dup_removal_lock."""
        import os
        import errno
        import shutil
        from .categorizer import split_filename_ext
        from .file_ops import _force_remove, files_are_identical

        requested_set = {os.path.abspath(p) for p in source_paths if p}
        if not requested_set:
            return 0, [], 0

        # Build a map of file -> surviving twin from scan groups
        twin_map: Dict[str, str] = {}
        if groups:
            for g in groups:
                g_files = [os.path.abspath(f) for f in g.get("files", []) if f]
                existing_g = [f for f in g_files if os.path.exists(f)]
                if len(existing_g) < 2:
                    continue
                # Find a member NOT requested for deletion; if user selected ALL members
                # in the group, force-preserve existing_g[0] so at least 1 copy survives!
                unselected = [f for f in existing_g if f not in requested_set]
                keeper = unselected[0] if unselected else existing_g[0]
                for f in existing_g:
                    if f != keeper and f in requested_set:
                        twin_map[f] = keeper

        trash_dir = os.path.join(root_folder, ".Duplicates_Trash")
        if not permanent_delete:
            if os.path.islink(trash_dir):
                # Would move files out of the scanned folder, possibly to
                # another drive, and emptying it would not be allowed (P2-07).
                return 0, [
                    f"{trash_dir} is a symbolic link, so nothing was quarantined. "
                    "Replace it with a real folder, or use 'Permanently Delete'."
                ], 0
            try:
                os.makedirs(trash_dir, exist_ok=True)
            except OSError as e:
                if e.errno == errno.ENOSPC:
                    return 0, [
                        "Drive is 100% full (0 bytes free): cannot create .Duplicates_Trash folder. "
                        "Use 'Permanently Delete Selected' to free space immediately."
                    ], 0
                return 0, [f"Could not create .Duplicates_Trash: {e}"], 0

        removed_count = 0
        bytes_reclaimed = 0
        refused = []
        free_before = self._volume_free_bytes(root_folder) if permanent_delete else None

        for raw_path in source_paths:
            if not raw_path:
                continue
            file_path = os.path.abspath(raw_path)
            if not os.path.exists(file_path):
                continue

            # Verify a surviving twin exists and matches
            twin = twin_map.get(file_path, "")
            if groups is not None:
                if not twin:
                    refused.append(
                        f"{os.path.basename(file_path)}: preserved as the surviving original copy in its group."
                    )
                    continue
                if not os.path.exists(twin):
                    refused.append(
                        f"{os.path.basename(file_path)}: surviving original ({os.path.basename(twin)}) is missing - kept safe."
                    )
                    continue
                try:
                    if os.path.samefile(file_path, twin):
                        refused.append(
                            f"{os.path.basename(file_path)}: points to the exact same file as original - kept safe."
                        )
                        continue
                    # The sampled hash only reads the first and last 1 MB, so
                    # a file changed in the middle after the scan (same size)
                    # still matched and was deleted although it was no longer
                    # a duplicate (audit P2-02). Compare every byte instead.
                    if not files_are_identical(file_path, twin):
                        refused.append(
                            f"{os.path.basename(file_path)}: no longer matches original copy - kept safe."
                        )
                        continue
                except OSError:
                    refused.append(
                        f"{os.path.basename(file_path)}: could not verify original copy - kept safe."
                    )
                    continue

            try:
                file_sz = os.path.getsize(file_path)
            except OSError:
                file_sz = 0

            if permanent_delete:
                if _force_remove(file_path):
                    removed_count += 1
                    bytes_reclaimed += file_sz
                else:
                    refused.append(f"{os.path.basename(file_path)}: permission denied or locked.")
            else:
                try:
                    filename = os.path.basename(file_path)
                    target_path = os.path.join(trash_dir, filename)

                    counter = 1
                    name, ext = split_filename_ext(filename)
                    ext_dot = ext if ext.startswith(".") else (f".{ext}" if ext else "")
                    while os.path.exists(target_path):
                        target_path = os.path.join(trash_dir, f"{name}_{counter}{ext_dot}")
                        counter += 1

                    shutil.move(file_path, target_path)
                    removed_count += 1
                    bytes_reclaimed += file_sz
                except OSError as e:
                    if e.errno == errno.ENOSPC:
                        refused.append(
                            f"{os.path.basename(file_path)}: drive is 100% full (No space left on device to update .Duplicates_Trash). Use 'Permanently Delete' instead."
                        )
                    else:
                        refused.append(f"{os.path.basename(file_path)}: could not be moved ({e}).")
                except Exception as e:
                    refused.append(f"{os.path.basename(file_path)}: could not be moved ({e}).")

        if permanent_delete:
            bytes_reclaimed = self._space_actually_freed(
                bytes_reclaimed, free_before, root_folder, f"Deleted {removed_count:,} duplicate(s)"
            )
        return removed_count, refused, bytes_reclaimed

    @staticmethod
    def _volume_free_bytes(path: str) -> Optional[int]:
        try:
            st = os.statvfs(path)
        except OSError:
            return None
        return st.f_bavail * st.f_frsize

    def _space_actually_freed(
        self, logical: int, free_before: Optional[int], path: str, what: str
    ) -> int:
        """Free space the drive gained, capped at the deleted files' total size.

        Deleting an APFS clone (what Finder's Duplicate, or a copy within one
        APFS volume, makes), a file with another hard link, or a file a
        snapshot still holds frees almost nothing. The utility used to report
        the files' sizes as freed regardless (audit P2-05). A gain within 1%
        (at least 1 MiB) of the files' size counts as all of it, so metadata
        blocks the file system allocates meanwhile are not reported as a
        shortfall. Anything else writing to the drive at the same time makes
        the result an underestimate, never an overestimate.
        """
        free_after = self._volume_free_bytes(path)
        if free_before is None or free_after is None:
            return logical
        gained = max(0, free_after - free_before)
        if gained + max(1024 * 1024, logical // 100) >= logical:
            return logical
        self.log_cb(
            f"{what}: {logical:,} bytes of file data, but the drive's free space "
            f"grew by only {gained:,} bytes. The deleted copies shared storage with "
            f"other files (APFS clones or hard links), or a snapshot still holds them."
        )
        return gained

    def empty_duplicates_trash(self, root_folder: str) -> Tuple[int, int, str]:
        """Permanently deletes root_folder/.Duplicates_Trash to reclaim disk space."""
        with OrganizerAPI._dup_removal_lock:
            return self._empty_duplicates_trash_locked(root_folder)

    def _empty_duplicates_trash_locked(self, root_folder: str) -> Tuple[int, int, str]:
        """empty_duplicates_trash's body; the caller holds _dup_removal_lock."""
        import os
        import shutil
        from .file_ops import _clear_immutable

        trash_dir = os.path.join(os.path.abspath(os.path.expanduser(root_folder)), ".Duplicates_Trash")
        # os.walk follows the top folder when it is a symbolic link, so a
        # linked .Duplicates_Trash had the contents of whatever it pointed to
        # deleted (audit P2-07).
        if os.path.islink(trash_dir):
            return 0, 0, (
                f"{trash_dir} is a symbolic link (to {os.path.realpath(trash_dir)}). "
                "It was not emptied, so that nothing outside the scanned folder is deleted."
            )
        if not os.path.isdir(trash_dir):
            return 0, 0, ""

        files_deleted = 0
        bytes_freed = 0
        free_before = self._volume_free_bytes(trash_dir)
        for root, dirs, files in os.walk(trash_dir, topdown=False):
            for f in files:
                fp = os.path.join(root, f)
                try:
                    sz = os.path.getsize(fp)
                except OSError:
                    sz = 0
                try:
                    _clear_immutable(fp)
                    os.unlink(fp)
                    files_deleted += 1
                    bytes_freed += sz
                except OSError:
                    pass
            for d in dirs:
                dp = os.path.join(root, d)
                try:
                    os.rmdir(dp)
                except OSError:
                    pass
        err = ""
        try:
            shutil.rmtree(trash_dir, ignore_errors=True)
        except Exception as e:
            err = str(e)
        bytes_freed = self._space_actually_freed(
            bytes_freed, free_before, os.path.dirname(trash_dir),
            f"Emptied .Duplicates_Trash ({files_deleted:,} files)",
        )
        return files_deleted, bytes_freed, err


    def get_duplicate_records(self, dest_abs: str):
        """Returns list of duplicate source files recorded during run."""
        db_path = os.path.join(dest_abs, ".organizer_checkpoint.db")
        if not os.path.exists(db_path):
            return []
        staging_prefix = os.path.realpath(os.path.join(dest_abs, ".organizer_staging")) + os.sep
        conn = sqlite3.connect(db_path, timeout=30.0)
        try:
            cursor = conn.execute(
                "SELECT source_path, size, COALESCE(duplicate_of, '') FROM copies "
                "WHERE status = 'completed' AND dest_path = 'DUPLICATE_SKIPPED'"
            )
            rows = cursor.fetchall()
        except sqlite3.OperationalError:
            # Database written before duplicate_of existed.
            cursor = conn.execute(
                "SELECT source_path, size FROM copies "
                "WHERE status = 'completed' AND dest_path = 'DUPLICATE_SKIPPED'"
            )
            rows = [(r[0], r[1], "") for r in cursor.fetchall()]
        finally:
            conn.close()

        records = []
        for src_p, sz, dup_of in rows:
            if not src_p or not os.path.exists(src_p):
                continue
            try:
                if os.path.realpath(src_p).startswith(staging_prefix):
                    continue
            except OSError:
                pass
            records.append({"source_path": src_p, "size": sz, "duplicate_of": dup_of})
        return records

    def trash_duplicates(self, source_paths: list, dest_abs: str = ""):
        """
        Safely moves duplicate source files to a '.Duplicates_Trash' folder on the source drive.
        Bypasses Finder AppleScript popups and Touch ID prompts completely (100% automated & zero-prompt).

        Every file is re-verified against its surviving twin immediately before
        being moved. This is the last point at which the user's only remaining
        original can be taken away, and the duplicate record may be arbitrarily
        old: the destination copy could have been deleted, moved, or removed by
        a repair pass since it was written. Anything that cannot be proven safe
        is left exactly where it is and reported back.

        Returns (trashed_count, refused_list).
        """
        valid_paths = [p for p in source_paths if p and os.path.exists(p)]
        if not valid_paths:
            return 0, []

        db_path = os.path.join(dest_abs, ".organizer_checkpoint.db") if dest_abs else ""
        if not dest_abs or not os.path.exists(db_path):
            refused = [
                f"{os.path.basename(p)}: no verified destination checkpoint database provided - left in place."
                for p in valid_paths
            ]
            return 0, refused

        # Look up the surviving twin recorded for each duplicate.
        twins: Dict[str, str] = {}
        for rec in self.get_duplicate_records(dest_abs):
            sp = rec["source_path"]
            dup_of = rec.get("duplicate_of") or ""
            twins[sp] = dup_of
            try:
                twins[os.path.realpath(sp)] = dup_of
            except OSError:
                pass

        trashed_count = 0
        trashed_pairs: List[Tuple[str, str]] = []
        refused = []
        for file_path in valid_paths:
            twin = twins.get(file_path) or twins.get(os.path.realpath(file_path), "")

            if not twin:
                refused.append(
                    f"{os.path.basename(file_path)}: no record of which destination "
                    f"copy this matched (organized by an older version) - left in place."
                )
                continue
            if not os.path.exists(twin):
                refused.append(
                    f"{os.path.basename(file_path)}: its copy at the destination is "
                    f"missing - left in place so you still have the original."
                )
                continue
            try:
                if os.path.samefile(file_path, twin):
                    refused.append(
                        f"{os.path.basename(file_path)}: source and destination point to the "
                        f"same underlying file - left in place."
                    )
                    continue
            except OSError:
                pass
            if not files_are_identical(file_path, twin):
                refused.append(
                    f"{os.path.basename(file_path)}: no longer byte-identical to the "
                    f"destination copy - left in place."
                )
                continue

            moved_ok = False
            recorded_trash_path = ""
            try:
                parent_dir = os.path.dirname(file_path)
                trash_dir = os.path.join(parent_dir, ".Duplicates_Trash")
                os.makedirs(trash_dir, exist_ok=True)

                filename = os.path.basename(file_path)
                target_path = os.path.join(trash_dir, filename)

                # Handle filename collisions in trash folder
                counter = 1
                name, ext = split_filename_ext(filename)
                ext_dot = ext if ext.startswith(".") else (f".{ext}" if ext else "")
                while os.path.exists(target_path):
                    target_path = os.path.join(trash_dir, f"{name}_{counter}{ext_dot}")
                    counter += 1

                shutil.move(file_path, target_path)
                trashed_count += 1
                moved_ok = True
                recorded_trash_path = target_path
            except Exception:
                # Fallback to AppleScript if direct filesystem move fails; pass path via argv
                try:
                    script = (
                        "on run argv\n"
                        "  tell application \"Finder\" to delete (POSIX file (item 1 of argv))\n"
                        "end run"
                    )
                    res = subprocess.run(
                        ["osascript", "-e", script, file_path],
                        capture_output=True,
                        text=True,
                        timeout=15,
                    )
                    if res.returncode == 0:
                        trashed_count += 1
                        moved_ok = True
                    else:
                        refused.append(f"{os.path.basename(file_path)}: could not be moved.")
                except Exception:
                    refused.append(f"{os.path.basename(file_path)}: could not be moved.")

            if moved_ok:
                trashed_pairs.append((recorded_trash_path, file_path))

        if trashed_pairs and os.path.exists(db_path):
            try:
                conn = sqlite3.connect(db_path, timeout=30.0)
                try:
                    cols = [r[1] for r in conn.execute("PRAGMA table_info(copies)").fetchall()]
                    if "trashed_path" not in cols:
                        conn.execute("ALTER TABLE copies ADD COLUMN trashed_path TEXT DEFAULT ''")
                    conn.executemany(
                        "UPDATE copies SET status = 'trashed', trashed_path = ? WHERE source_path = ?",
                        trashed_pairs,
                    )
                except sqlite3.OperationalError:
                    conn.executemany(
                        "UPDATE copies SET status = 'trashed' WHERE source_path = ?",
                        [(src_p,) for _tp, src_p in trashed_pairs],
                    )
                conn.commit()
                conn.close()
            except Exception:
                pass

        return trashed_count, refused

    def get_trashed_duplicates(self, dest_abs: str):
        """Returns list of duplicate source files currently isolated in .Duplicates_Trash."""
        db_path = os.path.join(dest_abs, ".organizer_checkpoint.db") if dest_abs else ""
        if not dest_abs or not os.path.exists(db_path):
            return []
        conn = sqlite3.connect(db_path, timeout=30.0)
        try:
            cursor = conn.execute(
                "SELECT source_path, size, COALESCE(duplicate_of, ''), COALESCE(trashed_path, '') FROM copies "
                "WHERE status = 'trashed'"
            )
            rows = cursor.fetchall()
        except sqlite3.OperationalError:
            try:
                cursor = conn.execute(
                    "SELECT source_path, size, COALESCE(duplicate_of, '') FROM copies WHERE status = 'trashed'"
                )
                rows = [(r[0], r[1], r[2], "") for r in cursor.fetchall()]
            except sqlite3.OperationalError:
                rows = []
        finally:
            conn.close()

        records = []
        for src_p, sz, dup_of, tr_p in rows:
            if not src_p:
                continue
            cand_trash = tr_p or os.path.join(os.path.dirname(src_p), ".Duplicates_Trash", os.path.basename(src_p))
            if os.path.exists(cand_trash):
                records.append({
                    "source_path": src_p,
                    "trashed_path": cand_trash,
                    "size": sz,
                    "duplicate_of": dup_of,
                })
        return records

    def restore_duplicates(self, dest_abs: str, source_paths: Optional[list] = None):
        """
        Restores duplicate files from .Duplicates_Trash back to their original source locations.
        Returns (restored_count, refused_list).
        """
        db_path = os.path.join(dest_abs, ".organizer_checkpoint.db") if dest_abs else ""
        if not dest_abs or not os.path.exists(db_path):
            return 0, ["No verified destination checkpoint database found."]

        trashed_records = self.get_trashed_duplicates(dest_abs)
        if not trashed_records:
            return 0, []

        if source_paths:
            requested = set(source_paths) | {os.path.realpath(p) for p in source_paths if p}
            trashed_records = [
                r for r in trashed_records
                if r["source_path"] in requested or os.path.realpath(r["source_path"]) in requested
            ]

        restored_count = 0
        restored_sources: List[str] = []
        refused: List[str] = []

        for rec in trashed_records:
            src_p = rec["source_path"]
            tr_p = rec["trashed_path"]
            if not os.path.exists(tr_p):
                refused.append(f"{os.path.basename(src_p)}: trashed file no longer exists in .Duplicates_Trash.")
                continue

            if os.path.exists(src_p):
                if files_are_identical(tr_p, src_p):
                    try:
                        os.remove(tr_p)
                        restored_count += 1
                        restored_sources.append(src_p)
                    except OSError as e:
                        refused.append(f"{os.path.basename(src_p)}: {e}")
                else:
                    refused.append(
                        f"{os.path.basename(src_p)}: a different file now exists at the original path - left in .Duplicates_Trash."
                    )
                    continue
            else:
                try:
                    os.makedirs(os.path.dirname(src_p), exist_ok=True)
                    shutil.move(tr_p, src_p)
                    restored_count += 1
                    restored_sources.append(src_p)
                except Exception as e:
                    refused.append(f"{os.path.basename(src_p)}: could not be restored ({e}).")
                    continue

            trash_dir = os.path.dirname(tr_p)
            try:
                if os.path.basename(trash_dir) == ".Duplicates_Trash" and os.path.isdir(trash_dir) and not os.listdir(trash_dir):
                    os.rmdir(trash_dir)
            except OSError:
                pass

        if restored_sources and os.path.exists(db_path):
            try:
                conn = sqlite3.connect(db_path, timeout=30.0)
                try:
                    conn.executemany(
                        "UPDATE copies SET status = 'completed', dest_path = 'DUPLICATE_SKIPPED', trashed_path = '' WHERE source_path = ?",
                        [(s,) for s in restored_sources],
                    )
                except sqlite3.OperationalError:
                    conn.executemany(
                        "UPDATE copies SET status = 'completed', dest_path = 'DUPLICATE_SKIPPED' WHERE source_path = ?",
                        [(s,) for s in restored_sources],
                    )
                conn.commit()
                conn.close()
            except Exception:
                pass

        return restored_count, refused

    def verify_transfer(self, dest_abs: str) -> dict:
        """
        Fast automated verification checker.
        Verifies existence, file size, and sample SHA-256 hashes of copied files in <1 second.
        """
        db_path = os.path.join(dest_abs, ".organizer_checkpoint.db")
        if not os.path.exists(db_path):
            return {
                "success": False,
                "error": "No checkpoint database found at destination."
            }

        try:
            conn = sqlite3.connect(db_path, timeout=30.0)
            try:
                cursor = conn.execute(
                    "SELECT source_path, dest_path, status, size, part_hash, COALESCE(duplicate_of, '') FROM copies"
                )
                rows = cursor.fetchall()
            except sqlite3.OperationalError:
                cursor = conn.execute("SELECT source_path, dest_path, status, size, part_hash FROM copies")
                rows = [(r[0], r[1], r[2], r[3], r[4], "") for r in cursor.fetchall()]
            conn.close()
        except Exception as db_err:
            return {"success": False, "error": f"Database error: {str(db_err)}"}

        engine = FileEngine(dest_abs)

        total_files = len(rows)
        verified_count = 0
        missing_count = 0
        mismatched_count = 0
        skipped_dup_count = 0
        missing_list = []
        mismatched_list = []
        hash_check_limit = 10000  # Check SHA-256 hashes for up to 10,000 files for comprehensive verification
        hashes_checked = 0
        # Built lazily, at most once, only if some file is not where we expect.
        dest_index: Optional[Dict[str, List[str]]] = None

        for source_path, dest_path, status, size, part_hash, duplicate_of in rows:
            if dest_path == "DUPLICATE_SKIPPED":
                if duplicate_of and not os.path.exists(duplicate_of):
                    missing_count += 1
                    missing_list.append(f"{os.path.basename(source_path)} (Surviving duplicate copy missing)")
                else:
                    skipped_dup_count += 1
                continue

            if status and status.startswith("failed"):
                missing_count += 1
                reason = status.split("failed: ", 1)[-1] if "failed: " in status else status
                missing_list.append(f"{os.path.basename(source_path)} ({reason})")
                continue

            # Path resolution across volume mounts.
            # The old code re-walked the whole drive for every missing file and
            # accepted the first basename match, which could "verify" a totally
            # unrelated file. Build the index once and demand size+hash agreement.
            target_check_path = dest_path
            if not os.path.exists(target_check_path):
                if dest_index is None:
                    dest_index = {}
                    for root, dirs, files in os.walk(dest_abs):
                        dirs[:] = [d for d in dirs if d not in SKIP_ORGANIZER_DIRS and not d.startswith(".unzipped_")]
                        for f in files:
                            # Skip in-progress copies, not the owner's *.tmp files.
                            if f.endswith(PARTIAL_SUFFIX) or f in GARBAGE_FILES:
                                continue
                            dest_index.setdefault(f, []).append(os.path.join(root, f))

                found = False
                for cand in dest_index.get(os.path.basename(dest_path), []):
                    try:
                        if os.path.getsize(cand) != size:
                            continue
                        if part_hash:
                            cand_hash = engine._get_part_hash(cand, size)
                            if cand_hash and cand_hash != part_hash:
                                continue
                    except OSError:
                        continue
                    target_check_path = cand
                    found = True
                    break

                if not found:
                    missing_count += 1
                    missing_list.append(f"{os.path.basename(source_path)} (File missing on destination)")
                    continue

            try:
                dest_size = os.path.getsize(target_check_path)
                if dest_size != size:
                    mismatched_count += 1
                    mismatched_list.append(f"{os.path.basename(target_check_path)} (Size mismatch: expected {size}B, found {dest_size}B)")
                    continue

                if part_hash and hashes_checked < hash_check_limit:
                    dest_hash = engine._get_part_hash(target_check_path, dest_size)
                    hashes_checked += 1
                    if dest_hash and dest_hash != part_hash:
                        mismatched_count += 1
                        mismatched_list.append(f"{os.path.basename(target_check_path)} (Hash mismatch)")
                        continue
            except OSError:
                missing_count += 1
                missing_list.append(f"{os.path.basename(source_path)} (Inaccessible file)")
                continue

            verified_count += 1

        engine.close()

        is_perfect = (missing_count == 0 and mismatched_count == 0 and (verified_count + skipped_dup_count) == total_files)

        return {
            "success": True,
            "total_files": total_files,
            "verified_count": verified_count,
            "skipped_duplicates": skipped_dup_count,
            "missing_count": missing_count,
            "mismatched_count": mismatched_count,
            "missing_list": missing_list[:10],
            "mismatched_list": mismatched_list[:10],
            "is_perfect": is_perfect
        }

    def repair_transfer(self, dest_abs: str, deep: bool = True) -> dict:
        """
        Clears invalid/missing/mismatched entries from the checkpoint DB so a
        later run re-copies them.

        Deleting a destination file here is dangerous: a bad row's dest_path may
        be a path that some *other*, perfectly healthy file legitimately owns.
        So a file is only ever deleted when all of the following hold:
          1. exactly one DB row claims that path,
          2. no successfully-completed row claims that path,
          3. the path really lives inside dest_abs,
          4. the file is a cut-short (or complete) copy of its source, and the
             source still exists (see _is_cut_short_copy). Otherwise the file
             may be the last copy of its data (source gone, or edited after
             organizing), so it is kept and only its record is cleared.

        `deep` controls content verification. When True every healthy row is
        re-hashed, which reads 1MB from the head and 1MB from the tail of every
        file already at the destination. That is the right behaviour for the
        explicit "Verify & Repair" button, but it is far too expensive to do
        automatically before every transfer: measured at roughly 2.2 hours on
        an external HDD holding 400,000 files. The automatic pre-flight pass
        therefore runs with deep=False, checking only existence and size.
        """
        db_path = os.path.join(dest_abs, ".organizer_checkpoint.db")
        if not os.path.exists(db_path):
            return {"success": False, "error": "No checkpoint database found at destination."}

        try:
            conn = sqlite3.connect(db_path, timeout=30.0)
            try:
                cursor = conn.execute(
                    "SELECT source_path, dest_path, size, part_hash, status, COALESCE(duplicate_of, '') FROM copies"
                )
                rows = cursor.fetchall()
            except sqlite3.OperationalError:
                cursor = conn.execute("SELECT source_path, dest_path, size, part_hash, status FROM copies")
                rows = [(r[0], r[1], r[2], r[3], r[4], "") for r in cursor.fetchall()]
            conn.close()
        except Exception as db_err:
            return {"success": False, "error": f"Database error: {str(db_err)}"}

        dest_real = os.path.realpath(dest_abs)

        def is_inside_dest(p: str) -> bool:
            if not p:
                return False
            try:
                pr = os.path.realpath(p)
                return pr != dest_real and os.path.commonpath([dest_real, pr]) == dest_real
            except (OSError, ValueError):
                return False

        # How many rows point at each destination path, and which paths are
        # owned by a row that actually succeeded.
        path_claims: Dict[str, int] = {}
        completed_paths = set()
        for source_path, dest_path, size, part_hash, status, _dup_of in rows:
            if not dest_path or dest_path == "DUPLICATE_SKIPPED":
                continue
            path_claims[dest_path] = path_claims.get(dest_path, 0) + 1
            if not (status and str(status).startswith("failed")):
                completed_paths.add(dest_path)

        engine = FileEngine(dest_abs)
        purged_sources = []
        bad_dest_paths = set()
        deleted_count = 0
        kept_paths = []

        try:
            for source_path, dest_path, size, part_hash, status, duplicate_of in rows:
                if dest_path == "DUPLICATE_SKIPPED":
                    continue

                failed_row = bool(status and str(status).startswith("failed"))

                # A failed row with no recorded destination: nothing was ever
                # written, so just drop the record.
                if failed_row and not dest_path:
                    purged_sources.append(source_path)
                    continue

                is_bad = failed_row
                if not is_bad:
                    if not os.path.exists(dest_path):
                        is_bad = True
                    else:
                        try:
                            dest_size = os.path.getsize(dest_path)
                            if dest_size != size:
                                is_bad = True
                            elif deep and part_hash:
                                dest_hash = engine._get_part_hash(dest_path, dest_size)
                                if dest_hash and dest_hash != part_hash:
                                    is_bad = True
                        except OSError:
                            is_bad = True

                if not is_bad:
                    continue

                purged_sources.append(source_path)
                if dest_path:
                    bad_dest_paths.add(dest_path)

                # Only delete a file this row unambiguously owns.
                if not os.path.exists(dest_path):
                    continue
                if not is_inside_dest(dest_path):
                    continue
                if path_claims.get(dest_path, 0) != 1:
                    continue
                if failed_row and dest_path in completed_paths:
                    continue
                # ...and only when nothing is lost by deleting it: the file must
                # be a cut-short (or complete) copy of a source file that still
                # exists, so the next run can copy it again. A file that differs
                # in any other way was edited after organizing, or its source is
                # gone, and may be the only copy of that data. Keep it; its
                # record is still cleared above.
                if not self._is_cut_short_copy(dest_path, source_path):
                    kept_paths.append(dest_path)
                    continue

                if _force_remove(dest_path):
                    deleted_count += 1

            # Second pass: purge DUPLICATE_SKIPPED records whose surviving twin
            # is missing or was just invalidated/deleted above.
            for source_path, dest_path, size, part_hash, status, duplicate_of in rows:
                if dest_path != "DUPLICATE_SKIPPED":
                    continue
                if status == "trashed":
                    continue
                if duplicate_of and (not os.path.exists(duplicate_of) or duplicate_of in bad_dest_paths):
                    purged_sources.append(source_path)
        finally:
            engine.close()

        if purged_sources:
            try:
                conn = sqlite3.connect(db_path, timeout=30.0)
                cursor = conn.cursor()
                cursor.executemany("DELETE FROM copies WHERE source_path = ?", [(s,) for s in purged_sources])
                conn.commit()
                conn.close()
            except Exception as db_err:
                return {"success": False, "error": f"Failed to update database: {str(db_err)}"}

        return {
            "success": True,
            "repaired_count": len(purged_sources),
            "deleted_count": deleted_count,
            "kept_count": len(kept_paths),
        }

    @staticmethod
    def _is_cut_short_copy(candidate: str, source: str) -> bool:
        """True only when every byte of `candidate` matches the start of
        `source`, and `source` is still a regular file: the signature of a
        copy that was cut short (or of a complete copy). Deleting such a file
        loses nothing, because the next run can copy the source again.

        Anything else is False: the source is gone (so `candidate` may be the
        last copy), the file was edited, appended to, or replaced after
        organizing, or it cannot be read.
        """
        try:
            if os.path.islink(source) or not os.path.isfile(source):
                return False
            if os.path.islink(candidate) or not os.path.isfile(candidate):
                return False
            if os.path.getsize(candidate) > os.path.getsize(source):
                return False
            with open(candidate, "rb") as cand_fh, open(source, "rb") as src_fh:
                while True:
                    chunk = cand_fh.read(1024 * 1024)
                    if not chunk:
                        return True
                    if src_fh.read(len(chunk)) != chunk:
                        return False
        except OSError:
            return False

    def auto_repair_if_needed(self, dest_abs: str):
        """
        Automatically inspects destination checkpoint database before transfer start,
        purging any corrupted or missing file records from past interrupted runs.

        Deliberately shallow (deep=False): this runs before every merge/resume,
        and content-hashing the entire destination first made the app look
        frozen for hours on a large external drive. Existence and size catch
        the interrupted-copy case this is here for; byte-level verification is
        available on demand via the Verify & Repair button.
        """
        db_path = os.path.join(dest_abs, ".organizer_checkpoint.db")
        if not os.path.exists(db_path):
            return

        self.log_cb("🔎 Checking destination for records left by a previous interrupted run...")
        res = self.repair_transfer(dest_abs, deep=False)
        if res.get("success") and res.get("repaired_count", 0) > 0:
            count = res["repaired_count"]
            self.log_cb(f"🧹 [Auto-Repair] Detected {count} missing/corrupted file record(s) from a previous interrupted run. Automatically cleared bad records so they will be re-transferred cleanly.")
        if res.get("success") and res.get("kept_count", 0) > 0:
            self.log_cb(f"🛡️ [Auto-Repair] Kept {res['kept_count']} file(s) that no longer match their record (edited after organizing, or the original is gone). Nothing was deleted; if the original still exists it will be copied again next to the kept file.")





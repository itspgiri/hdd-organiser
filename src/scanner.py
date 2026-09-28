import os
import re
import stat
import hashlib
import tempfile
from typing import List, Dict, Set, Tuple, Optional
from .categorizer import Categorizer

GARBAGE_FILES = {
    ".DS_Store", ".localized", "Thumbs.db", "Desktop.ini", "desktop.ini",
    ".Spotlight-V100", ".fseventsd",
    # The organizer's own bookkeeping. Pointing the tool at a folder it has
    # already organized would otherwise treat these as user data and file them
    # under Unsorted/ -- and a stale copy of a checkpoint database is worse
    # than useless.
    ".organizer_checkpoint.db",
    ".organizer_checkpoint.db-shm",
    ".organizer_checkpoint.db-wal",
    ".metadata_never_index",
}

GARBAGE_PREFIXES = (
    "._", "~$"
)

# Staging area for extracted archives; never user content.
SKIP_ORGANIZER_DIRS = {".organizer_staging", ".Duplicates_Trash"}

# Operating-system internals. Never user data, not worth reporting.
SKIP_OS_DIRS = {
    ".Trash", ".Trashes", ".thumbnails", ".fseventsd", ".Spotlight-V100",
}

# Regenerable build output and dependency caches. These are *probably* junk,
# but "Caches", "venv" and ".tmp" are also perfectly ordinary folder names a
# person might use for real files. They are still skipped (restoring a
# node_modules tree across 1.7TB is pointless), but they are now counted and
# surfaced in the preview instead of disappearing without trace -- someone who
# trusts the preview and then wipes the source should not lose a folder just
# because of what they named it.
SKIP_BUILD_DIRS = {
    "node_modules", ".git", "__pycache__", ".venv", "venv",
    ".cache", "Caches", ".tmp",
    ".next", ".firebase", ".turbo", ".gradle", ".cargo",
}

SKIP_SYSTEM_DIRS = SKIP_OS_DIRS | SKIP_BUILD_DIRS | SKIP_ORGANIZER_DIRS

_GDRIVE_TOKEN_RE = re.compile(
    r"(?:^|[^a-z0-9])(gdrive|takeout|icloud|onedrive|drive|cloud|download)(?:[^a-z0-9]|$)",
    re.IGNORECASE,
)


def split_source_paths(source_path) -> List[str]:
    """Normalizes source_path (list or comma-separated string) into unique paths.

    Preserves single existing directory paths that legitimately contain a comma
    in their folder name instead of splitting them into non-existent halves.
    """
    raw_list: List[str] = []
    if isinstance(source_path, (list, tuple, set)):
        for item in source_path:
            s = os.path.expanduser(str(item).strip())
            if s:
                raw_list.append(s)
    elif isinstance(source_path, str):
        stripped = os.path.expanduser(source_path.strip())
        if stripped and os.path.exists(stripped):
            raw_list.append(stripped)
        else:
            for part in source_path.split(","):
                s = os.path.expanduser(part.strip())
                if s:
                    raw_list.append(s)

    deduped: List[str] = []
    seen: Set[str] = set()
    for p in raw_list:
        norm = os.path.normcase(os.path.realpath(p)) if os.path.exists(p) else os.path.normcase(os.path.abspath(p))
        if norm not in seen:
            seen.add(norm)
            deduped.append(p)
    return deduped


class Scanner:
    def __init__(self, categorizer: Categorizer, staging_root: Optional[str] = None):
        self.categorizer = categorizer
        # Where auto-extracted archives are unpacked. Deliberately NOT the
        # source drive: the organizer advertises a read-only source policy,
        # and a large Takeout archive unpacked in place silently doubles its
        # footprint on a drive that may be full or mounted read-only.
        self.staging_root = staging_root or os.path.join(
            tempfile.gettempdir(), "drive_organizer_staging"
        )
        self.staging_dirs_used: List[str] = []
        self.projects_found: List[str] = []
        self.files_to_process: List[str] = []
        self.ignored_garbage_count = 0
        self.garbage_breakdown: Dict[str, int] = {}
        self.garbage_samples: List[str] = []
        self.gdrive_zips_extracted = 0
        self.gdrive_zip_names: List[str] = []
        self.processed_zips: Set[str] = set()
        # Build/cache folders that were pruned. Recorded so the preview can
        # tell the user what will not be transferred, rather than leaving them
        # to discover it after wiping the source.
        self.skipped_dirs: List[str] = []
        self.skipped_dir_breakdown: Dict[str, int] = {}

    def _reset_scan_state(self):
        self.projects_found = []
        self.files_to_process = []
        self.ignored_garbage_count = 0
        self.garbage_breakdown = {}
        self.garbage_samples = []
        self.gdrive_zips_extracted = 0
        self.gdrive_zip_names = []
        self.processed_zips = set()
        self.skipped_dirs = []
        self.skipped_dir_breakdown = {}

    def is_garbage(self, filename: str) -> bool:
        if filename in GARBAGE_FILES:
            return True
        if filename.startswith(GARBAGE_PREFIXES):
            return True
        return False

    def is_gdrive_zip(self, filename: str, root_path: str = "") -> bool:
        # Never unzip zip files that are inside a staging folder
        if ".unzipped_" in root_path or ".organizer_staging" in root_path:
            return False
        if root_path:
            try:
                root_real = os.path.realpath(root_path)
                staging_real = os.path.realpath(self.staging_root)
                if root_real == staging_real or os.path.commonpath([root_real, staging_real]) == staging_real:
                    return False
            except (OSError, ValueError):
                if root_path.startswith(self.staging_root):
                    return False
        fn = filename.lower()
        if not fn.endswith('.zip'):
            return False
        stem = fn[:-4]
        return bool(_GDRIVE_TOKEN_RE.search(stem))

    def cleanup_staging(self):
        """Removes archive staging directories created during this scan.

        Called only after a successful transfer: on cancellation or failure the
        extracted data is left in place so the next run can resume without
        unzipping everything again.
        """
        import shutil as _shutil
        removed = 0
        for d in self.staging_dirs_used:
            if os.path.isdir(d):
                _shutil.rmtree(d, ignore_errors=True)
                removed += 1
        self.staging_dirs_used = []
        # Drop the staging root too, once nothing is left inside it.
        try:
            if os.path.isdir(self.staging_root) and not os.listdir(self.staging_root):
                os.rmdir(self.staging_root)
        except OSError:
            pass
        return removed

    def classify_garbage(self, filename: str) -> str:
        if filename.startswith("._"):
            return "macOS AppleDouble Sidecars (._*)"
        if filename.startswith("~$"):
            return "Office Temp / Lock Files (~$)"
        if filename in (".DS_Store", ".localized"):
            return "macOS Finder (.DS_Store / .localized)"
        if filename in ("Thumbs.db", "Desktop.ini", "desktop.ini"):
            return "Windows System Junk (Thumbs.db)"
        return "Other System Garbage"

    def _is_safe_zip_member(self, member, staging_dir: str) -> bool:
        """Returns False if a ZipInfo member attempts path traversal (Zip-Slip) or is a symlink."""
        raw_name = member.filename.replace("\\", "/")
        if raw_name.startswith("/") or raw_name.startswith("../") or "/../" in raw_name or raw_name == "..":
            return False
        parts = [p for p in raw_name.split("/") if p]
        if ".." in parts:
            return False
        # Reject Unix symlinks encoded in external_attr
        mode = (member.external_attr >> 16) & 0o170000
        if mode == stat.S_IFLNK:
            return False
        staging_abs = os.path.abspath(staging_dir)
        target_abs = os.path.abspath(os.path.join(staging_abs, *parts)) if parts else staging_abs
        try:
            if os.path.commonpath([staging_abs, target_abs]) != staging_abs:
                return False
        except ValueError:
            return False
        return True

    def _should_skip_zip_member_path(self, member_filename: str) -> bool:
        raw_name = member_filename.replace("\\", "/")
        parts = [p for p in raw_name.split("/") if p]
        if not parts:
            return True
        for dir_part in parts[:-1]:
            if dir_part == "__MACOSX" or dir_part in SKIP_SYSTEM_DIRS or dir_part.startswith(".unzipped_"):
                return True
        base_f = parts[-1]
        if base_f == ".unzip_completed" or self.is_garbage(base_f):
            return True
        return False

    def extract_gdrive_zip(self, zip_path: str, progress_cb=None, is_preview: bool = False, total_zips: int = 0, cancel_check=None) -> List[str]:
        """High-speed non-blocking extraction with completeness verification (.unzip_completed marker) and instant cancellation."""
        import zipfile
        import subprocess
        import shutil
        import time

        if cancel_check and cancel_check():
            return []

        zip_real = os.path.realpath(zip_path) if os.path.exists(zip_path) else os.path.abspath(zip_path)
        if zip_real in self.processed_zips or zip_path in self.processed_zips:
            return []
        self.processed_zips.add(zip_real)
        self.processed_zips.add(zip_path)

        extracted_files: List[str] = []
        zip_filename = os.path.basename(zip_path)
        staging_dir = None
        counted_extraction = False

        try:
            zip_name = os.path.splitext(zip_filename)[0]
            # Unpack under the staging root rather than beside the archive, so
            # the source drive is never written to. The directory name is
            # keyed by the full zip path so two archives with the same name
            # cannot collide, and so a re-run can still find (and re-use) a
            # previous complete extraction.
            path_key = hashlib.sha256(os.path.abspath(zip_path).encode("utf-8")).hexdigest()[:16]
            staging_dir = os.path.join(self.staging_root, f"{zip_name}_{path_key}")
            completed_marker = os.path.join(staging_dir, ".unzip_completed")
            if staging_dir not in self.staging_dirs_used:
                self.staging_dirs_used.append(staging_dir)

            # 1. INSTANT PREVIEW MODE (Memory-Only Inspection in ~0.05s)
            if is_preview:
                with zipfile.ZipFile(zip_path, 'r') as zip_ref:
                    for member in zip_ref.infolist():
                        if cancel_check and cancel_check():
                            return []
                        if member.is_dir():
                            continue
                        if not self._is_safe_zip_member(member, staging_dir):
                            continue
                        if self._should_skip_zip_member_path(member.filename):
                            continue
                        norm_rel = os.path.normpath(member.filename.replace("\\", "/")).lstrip(os.sep)
                        virtual_path = os.path.join(staging_dir, norm_rel)
                        extracted_files.append(virtual_path)
                self.gdrive_zips_extracted += 1
                if zip_filename not in self.gdrive_zip_names:
                    self.gdrive_zip_names.append(zip_filename)
                return extracted_files

            # 2. FULL TRANSFER MODE: CHECK IF ALREADY EXTRACTED & VERIFY COMPLETENESS
            if os.path.exists(staging_dir):
                if os.path.exists(completed_marker):
                    # Verified complete! Re-use extracted files without unzipping again.
                    if progress_cb:
                        zip_count_str = f"[{self.gdrive_zips_extracted + 1}/{total_zips}]" if total_zips > 0 else ""
                        progress_cb(0, 0, f"⏩ Skipping {zip_count_str} (Already verified unzipped): {zip_filename}")
                    for root, dirs, files in os.walk(staging_dir):
                        if cancel_check and cancel_check():
                            return []
                        dirs[:] = [
                            d for d in dirs
                            if d not in SKIP_SYSTEM_DIRS and d != "__MACOSX" and not d.startswith(".unzipped_")
                        ]
                        for f in files:
                            base_f = os.path.basename(f)
                            if base_f != ".unzip_completed" and not self.is_garbage(base_f):
                                full_f = os.path.join(root, f)
                                if not os.path.islink(full_f):
                                    extracted_files.append(full_f)
                    self.gdrive_zips_extracted += 1
                    if zip_filename not in self.gdrive_zip_names:
                        self.gdrive_zip_names.append(zip_filename)
                    return extracted_files
                else:
                    # Incomplete previous unzip attempt! Wipe and re-extract cleanly.
                    shutil.rmtree(staging_dir, ignore_errors=True)

            if cancel_check and cancel_check():
                return []

            # Pre-inspect archive structure to ensure it is a valid zip and has no Zip-Slip / symlink traps
            all_members_safe = True
            with zipfile.ZipFile(zip_path, 'r') as zip_check:
                for member in zip_check.infolist():
                    if not self._is_safe_zip_member(member, staging_dir):
                        all_members_safe = False
                        break

            os.makedirs(staging_dir, exist_ok=True)
            self.gdrive_zips_extracted += 1
            counted_extraction = True
            zip_count_str = f"[{self.gdrive_zips_extracted}/{total_zips}]" if total_zips > 0 else f"[{self.gdrive_zips_extracted}]"

            if progress_cb:
                progress_cb(0, 0, f"📦 Unzipping {zip_count_str}: {zip_filename}...")

            # Use native macOS unzip with process monitoring for instant mid-unzip cancellation
            extracted_via_native = False
            if all_members_safe:
                try:
                    proc = subprocess.Popen(
                        ["unzip", "-q", "-o", zip_path, "-d", staging_dir],
                        stdin=subprocess.DEVNULL,
                        stdout=subprocess.DEVNULL,
                        stderr=subprocess.DEVNULL
                    )
                    try:
                        archive_bytes = os.path.getsize(zip_path)
                    except OSError:
                        archive_bytes = 0
                    # Allow ~1 minute per 100MB, floor 5 min, ceiling 6 hours.
                    hard_deadline = time.time() + min(
                        max(300, (archive_bytes / (100 * 1024 * 1024)) * 60), 21600
                    )
                    stall_limit = 120  # seconds with zero new bytes written
                    last_size, last_change = 0, time.time()
                    last_report = 0.0

                    def _staged_bytes():
                        total = 0
                        for r, _d, fs in os.walk(staging_dir):
                            for nm in fs:
                                try:
                                    total += os.path.getsize(os.path.join(r, nm))
                                except OSError:
                                    pass
                        return total

                    while proc.poll() is None:
                        if cancel_check and cancel_check():
                            proc.kill()
                            proc.wait()
                            shutil.rmtree(staging_dir, ignore_errors=True)
                            return []

                        now = time.time()
                        if now - last_report >= 3:
                            last_report = now
                            grown = _staged_bytes()
                            if grown > last_size:
                                last_size, last_change = grown, now
                                if progress_cb:
                                    mb = grown / (1024 * 1024)
                                    progress_cb(0, 0, f"📦 Unzipping {zip_count_str}: {zip_filename} ({mb:,.0f} MB extracted)...")

                        if now - last_change > stall_limit or now > hard_deadline:
                            proc.kill()
                            proc.wait()
                            break
                        time.sleep(0.1)

                    if proc.returncode == 0 and not (cancel_check and cancel_check()):
                        extracted_via_native = True
                except Exception:
                    extracted_via_native = False

            if not extracted_via_native:
                shutil.rmtree(staging_dir, ignore_errors=True)
                os.makedirs(staging_dir, exist_ok=True)
                with zipfile.ZipFile(zip_path, 'r') as zip_ref:
                    for member in zip_ref.infolist():
                        if cancel_check and cancel_check():
                            shutil.rmtree(staging_dir, ignore_errors=True)
                            return []
                        if member.is_dir():
                            continue
                        if not self._is_safe_zip_member(member, staging_dir):
                            continue
                        if self._should_skip_zip_member_path(member.filename):
                            continue
                        extracted_path = zip_ref.extract(member, staging_dir)
                        if not os.path.islink(extracted_path):
                            extracted_files.append(extracted_path)

            if cancel_check and cancel_check():
                shutil.rmtree(staging_dir, ignore_errors=True)
                return []

            if extracted_via_native:
                for root, dirs, files in os.walk(staging_dir):
                    dirs[:] = [
                        d for d in dirs
                        if d not in SKIP_SYSTEM_DIRS and d != "__MACOSX" and not d.startswith(".unzipped_")
                    ]
                    for f in files:
                        base_f = os.path.basename(f)
                        if base_f != ".unzip_completed" and not self.is_garbage(base_f):
                            full_f = os.path.join(root, f)
                            if not os.path.islink(full_f):
                                extracted_files.append(full_f)

            # Mark extraction as 100% complete!
            try:
                with open(completed_marker, 'w') as f:
                    f.write("COMPLETED")
            except Exception:
                pass

            if zip_filename not in self.gdrive_zip_names:
                self.gdrive_zip_names.append(zip_filename)
        except Exception:
            if counted_extraction and self.gdrive_zips_extracted > 0:
                self.gdrive_zips_extracted -= 1
            if staging_dir and os.path.exists(staging_dir):
                shutil.rmtree(staging_dir, ignore_errors=True)
            extracted_files = []
            if not (cancel_check and cancel_check()):
                extracted_files.append(zip_path)

        return extracted_files

    def scan_directory(self, source_path, excluded_projects: Set[str] = None, progress_cb = None, cancel_check = None, auto_unzip_gdrive: bool = True, is_preview: bool = False):
        """Recursively scan directory or multiple directories, finding files and project folders."""
        self._reset_scan_state()
        excluded_raw = excluded_projects or set()
        excluded = set(excluded_raw) | {os.path.realpath(p) for p in excluded_raw if os.path.exists(p)}
        sources = split_source_paths(source_path)

        # Pre-pass: Count total Google Drive zips for progress tracking
        gdrive_zips_to_extract = []
        seen_prepass_zips: Set[str] = set()
        if auto_unzip_gdrive:
            for src in sources:
                if not os.path.exists(src):
                    continue
                for root, dirs, files in os.walk(src):
                    if cancel_check and cancel_check():
                        return
                    unpruned_dirs = list(dirs)
                    dirs[:] = [d for d in dirs if d not in SKIP_SYSTEM_DIRS and not d.startswith(".unzipped_")]
                    items_set = set(unpruned_dirs) | set(files)
                    if root != src and root not in excluded and os.path.realpath(root) not in excluded and self.categorizer.is_project_root(root, items_set):
                        dirs.clear()
                        continue
                    for f in files:
                        if self.is_gdrive_zip(f, root_path=root):
                            zp = os.path.join(root, f)
                            zp_real = os.path.realpath(zp)
                            if zp_real not in seen_prepass_zips:
                                seen_prepass_zips.add(zp_real)
                                gdrive_zips_to_extract.append(zp)

        total_zips = len(gdrive_zips_to_extract)

        scan_count = 0
        seen_dirs: Set[str] = set()
        seen_files: Set[str] = set()
        seen_projects: Set[str] = set()

        for src in sources:
            if not os.path.exists(src):
                continue
            for root, dirs, files in os.walk(src):
                if cancel_check and cancel_check():
                    return

                root_real = os.path.realpath(root)
                if root_real in seen_dirs:
                    dirs.clear()
                    continue
                seen_dirs.add(root_real)

                # Several project markers are directories that get pruned just
                # below (.git above all), so snapshot the listing first.
                unpruned_dirs = list(dirs)

                # 1. Check if this is a Code Project BEFORE recording skipped build dirs
                # so that a project's .git directory (which IS copied intact) is not
                # falsely reported as a skipped folder that will not be transferred.
                items_set = set(unpruned_dirs) | set(files)
                if root != src and root not in excluded and root_real not in excluded and self.categorizer.is_project_root(root, items_set):
                    if root_real not in seen_projects:
                        seen_projects.add(root_real)
                        self.projects_found.append(root)
                    # Record any build/cache directories inside the project root that
                    # copy_project_intact will ignore (everything in SKIP_BUILD_DIRS except .git).
                    for d in unpruned_dirs:
                        if d in SKIP_BUILD_DIRS and d != ".git":
                            self.skipped_dir_breakdown[d] = self.skipped_dir_breakdown.get(d, 0) + 1
                            if len(self.skipped_dirs) < 50:
                                self.skipped_dirs.append(os.path.join(root, d))
                    dirs.clear()  # Do not traverse INSIDE the project!
                    continue

                # 2. Prune skipped system / heavy build / unzipped staging directories in-place BEFORE entering them
                for d in dirs:
                    if d in SKIP_BUILD_DIRS:
                        self.skipped_dir_breakdown[d] = self.skipped_dir_breakdown.get(d, 0) + 1
                        if len(self.skipped_dirs) < 50:
                            self.skipped_dirs.append(os.path.join(root, d))

                dirs[:] = [d for d in dirs if d not in SKIP_SYSTEM_DIRS and not d.startswith(".unzipped_")]

                # 3. Otherwise, process individual files
                for file in files:
                    full_path = os.path.join(root, file)

                    if self.is_garbage(file):
                        self.ignored_garbage_count += 1
                        g_type = self.classify_garbage(file)
                        self.garbage_breakdown[g_type] = self.garbage_breakdown.get(g_type, 0) + 1
                        if len(self.garbage_samples) < 25:
                            self.garbage_samples.append(full_path)
                        continue

                    # Skip loose symlinks (which would become dangling when moved out of
                    # their relative directory or shadow their real target in seen_files)
                    # and special non-regular files (FIFOs, sockets, devices)
                    try:
                        lstat_res = os.lstat(full_path)
                        if not stat.S_ISREG(lstat_res.st_mode):
                            continue
                    except OSError:
                        continue

                    file_real = os.path.realpath(full_path)
                    if file_real in seen_files:
                        continue
                    seen_files.add(file_real)

                    # 4. Auto-extract Google Drive / Takeout / Download Zip Archives (with non-blocking execution & instant cancellation)
                    if auto_unzip_gdrive and self.is_gdrive_zip(file, root_path=root):
                        unzipped_items = self.extract_gdrive_zip(full_path, progress_cb=progress_cb, is_preview=is_preview, total_zips=total_zips, cancel_check=cancel_check)
                        if cancel_check and cancel_check():
                            return
                        for u_item in unzipped_items:
                            u_base = os.path.basename(u_item)
                            if not self.is_garbage(u_base):
                                self.files_to_process.append(u_item)
                                scan_count += 1
                        continue

                    self.files_to_process.append(full_path)
                    scan_count += 1

                    if progress_cb and (scan_count % 100 == 0):
                        progress_cb(0, 0, f"🔍 Scanning: {len(self.files_to_process)} files found... ({os.path.basename(root)})")








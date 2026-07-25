import os
from typing import List, Dict, Set, Tuple
from .categorizer import Categorizer

GARBAGE_FILES = {
    ".DS_Store", ".localized", "Thumbs.db", ".Spotlight-V100", ".fseventsd"
}

GARBAGE_PREFIXES = (
    "._", "~$"
)

SKIP_SYSTEM_DIRS = {
    ".Trash", ".thumbnails", ".fseventsd", ".Spotlight-V100", ".Trashes",
    "node_modules", ".git", "__pycache__", ".venv", "venv", ".cache", "Caches", ".tmp",
    ".Duplicates_Trash"
}

class Scanner:
    def __init__(self, categorizer: Categorizer):
        self.categorizer = categorizer
        self.projects_found: List[str] = []
        self.files_to_process: List[str] = []
        self.ignored_garbage_count = 0
        self.garbage_breakdown: Dict[str, int] = {}
        self.garbage_samples: List[str] = []
        self.gdrive_zips_extracted = 0
        self.gdrive_zip_names: List[str] = []
        self.processed_zips: Set[str] = set()

    def is_garbage(self, filename: str) -> bool:
        if filename in GARBAGE_FILES:
            return True
        if filename.startswith(GARBAGE_PREFIXES):
            return True
        return False

    def is_gdrive_zip(self, filename: str, root_path: str = "") -> bool:
        # Never unzip zip files that are inside a staging folder
        if ".unzipped_" in root_path:
            return False
        fn = filename.lower()
        if not fn.endswith('.zip'):
            return False
        gdrive_keywords = ["drive", "gdrive", "takeout", "cloud", "download"]
        return any(kw in fn for kw in gdrive_keywords)

    def classify_garbage(self, filename: str) -> str:
        if filename.startswith("._"):
            return "macOS AppleDouble Sidecars (._*)"
        if filename.startswith("~$"):
            return "Office Temp / Lock Files (~$)"
        if filename in (".DS_Store", ".localized"):
            return "macOS Finder (.DS_Store / .localized)"
        if filename in ("Thumbs.db", "Desktop.ini"):
            return "Windows System Junk (Thumbs.db)"
        return "Other System Garbage"

    def extract_gdrive_zip(self, zip_path: str, progress_cb=None, is_preview: bool = False, total_zips: int = 0, cancel_check=None) -> List[str]:
        """High-speed non-blocking extraction with completeness verification (.unzip_completed marker) and instant cancellation."""
        import zipfile
        import subprocess
        import shutil
        import time

        if cancel_check and cancel_check():
            return []

        if zip_path in self.processed_zips:
            return []
        self.processed_zips.add(zip_path)

        extracted_files = []
        zip_filename = os.path.basename(zip_path)

        try:
            zip_dir = os.path.dirname(zip_path)
            zip_name = os.path.splitext(zip_filename)[0]
            staging_dir = os.path.join(zip_dir, f".unzipped_{zip_name}")
            completed_marker = os.path.join(staging_dir, ".unzip_completed")

            # 1. INSTANT PREVIEW MODE (Memory-Only Inspection in ~0.05s)
            if is_preview:
                with zipfile.ZipFile(zip_path, 'r') as zip_ref:
                    for member in zip_ref.infolist():
                        if cancel_check and cancel_check():
                            return []
                        if member.is_dir():
                            continue
                        base_f = os.path.basename(member.filename)
                        if member.filename.startswith("__MACOSX/") or base_f.startswith("._") or base_f in GARBAGE_FILES:
                            continue
                        virtual_path = os.path.join(staging_dir, member.filename)
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
                        for f in files:
                            base_f = os.path.basename(f)
                            if base_f != ".unzip_completed" and not self.is_garbage(base_f):
                                extracted_files.append(os.path.join(root, f))
                    if extracted_files:
                        self.gdrive_zips_extracted += 1
                        if zip_filename not in self.gdrive_zip_names:
                            self.gdrive_zip_names.append(zip_filename)
                        return extracted_files
                else:
                    # Incomplete previous unzip attempt! Wipe and re-extract cleanly.
                    shutil.rmtree(staging_dir, ignore_errors=True)

            if cancel_check and cancel_check():
                return []

            os.makedirs(staging_dir, exist_ok=True)
            self.gdrive_zips_extracted += 1
            zip_count_str = f"[{self.gdrive_zips_extracted}/{total_zips}]" if total_zips > 0 else f"[{self.gdrive_zips_extracted}]"

            if progress_cb:
                progress_cb(0, 0, f"📦 Unzipping {zip_count_str}: {zip_filename}...")

            # Use native macOS unzip with process monitoring for instant mid-unzip cancellation
            extracted_via_native = False
            try:
                proc = subprocess.Popen(
                    ["unzip", "-q", "-o", zip_path, "-d", staging_dir],
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL
                )
                start_time = time.time()
                while proc.poll() is None:
                    if cancel_check and cancel_check():
                        proc.kill()
                        proc.wait()
                        shutil.rmtree(staging_dir, ignore_errors=True)
                        return []
                    if time.time() - start_time > 60: # 60s timeout safety
                        proc.kill()
                        proc.wait()
                        break
                    time.sleep(0.1)

                if proc.returncode == 0 and not (cancel_check and cancel_check()):
                    extracted_via_native = True
            except Exception:
                extracted_via_native = False

            if not extracted_via_native:
                with zipfile.ZipFile(zip_path, 'r') as zip_ref:
                    for member in zip_ref.infolist():
                        if cancel_check and cancel_check():
                            shutil.rmtree(staging_dir, ignore_errors=True)
                            return []
                        if member.is_dir():
                            continue
                        base_f = os.path.basename(member.filename)
                        if member.filename.startswith("__MACOSX/") or base_f.startswith("._") or base_f in GARBAGE_FILES:
                            continue
                        extracted_files.append(zip_ref.extract(member, staging_dir))

            if cancel_check and cancel_check():
                shutil.rmtree(staging_dir, ignore_errors=True)
                return []

            if extracted_via_native:
                for root, dirs, files in os.walk(staging_dir):
                    for f in files:
                        base_f = os.path.basename(f)
                        if base_f != ".unzip_completed" and not self.is_garbage(base_f):
                            extracted_files.append(os.path.join(root, f))

            # Mark extraction as 100% complete!
            try:
                with open(completed_marker, 'w') as f:
                    f.write("COMPLETED")
            except Exception:
                pass

            if zip_filename not in self.gdrive_zip_names:
                self.gdrive_zip_names.append(zip_filename)
        except Exception:
            if not (cancel_check and cancel_check()):
                extracted_files.append(zip_path)

        return extracted_files


    def scan_directory(self, source_path, excluded_projects: Set[str] = None, progress_cb = None, cancel_check = None, auto_unzip_gdrive: bool = True, is_preview: bool = False):
        """Recursively scan directory or multiple directories, finding files and project folders."""
        excluded = excluded_projects or set()
        if isinstance(source_path, list):
            sources = source_path
        else:
            sources = [p.strip() for p in source_path.split(',') if p.strip()]

        # Pre-pass: Count total Google Drive zips for progress tracking
        gdrive_zips_to_extract = []
        if auto_unzip_gdrive:
            for src in sources:
                if not os.path.exists(src):
                    continue
                for root, dirs, files in os.walk(src):
                    dirs[:] = [d for d in dirs if d not in SKIP_SYSTEM_DIRS and not d.startswith(".unzipped_")]
                    for f in files:
                        if self.is_gdrive_zip(f, root_path=root):
                            gdrive_zips_to_extract.append(os.path.join(root, f))
        
        total_zips = len(gdrive_zips_to_extract)

        scan_count = 0
        for src in sources:
            if not os.path.exists(src):
                continue
            for root, dirs, files in os.walk(src):
                if cancel_check and cancel_check():
                    return

                # 1. Prune skipped system / heavy build / unzipped staging directories in-place BEFORE entering them
                dirs[:] = [d for d in dirs if d not in SKIP_SYSTEM_DIRS and not d.startswith(".unzipped_")]

                # 2. Check if this is a Code Project
                items_set = set(dirs) | set(files)
                if root != src and root not in excluded and self.categorizer.is_project_root(root, items_set):
                    self.projects_found.append(root)
                    dirs.clear() # Do not traverse INSIDE the project!
                    continue

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







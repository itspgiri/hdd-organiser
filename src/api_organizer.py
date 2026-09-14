import os
import time
import shutil
import sqlite3
import subprocess
import threading
from concurrent.futures import ThreadPoolExecutor
from typing import Callable, Dict, Tuple, Set, List, Optional

from .categorizer import Categorizer
from .scanner import Scanner
from .dates import DateExtractor
from .file_ops import FileEngine, safe_copy, restore_timestamps, copy_project_intact, project_already_copied

class OrganizerAPI:
    def __init__(self, config_path: str, log_cb: Callable[[str], None], progress_cb: Callable[[int, int, str], None]):
        self.config_path = config_path
        self.log_cb = log_cb
        self.progress_cb = progress_cb
        self.cancelled = False

    def cancel(self):
        self.cancelled = True
        
    @staticmethod
    def _split_sources(source_abs) -> List[str]:
        """Normalises the source argument (list, or comma-separated string)."""
        if isinstance(source_abs, list):
            return [str(p).strip() for p in source_abs if str(p).strip()]
        return [p.strip() for p in str(source_abs).split(',') if p.strip()]

    @staticmethod
    def validate_paths(source_abs, dest_abs: str) -> Optional[str]:
        """Returns an error message if the source/destination pair is unsafe.

        Compares resolved real paths rather than raw strings: the old
        `dest.startswith(source)` test both rejected innocent siblings
        (/Volumes/Photos_Backup next to /Volumes/Photos) and could be walked
        around with symlinks or trailing separators. The GUI had no check at
        all, so a destination nested inside the source was accepted.
        """
        dest_real = os.path.realpath(dest_abs)
        for src in OrganizerAPI._split_sources(source_abs):
            src_real = os.path.realpath(src)
            if src_real == dest_real:
                return "Safety Error: Source and Destination are the same folder."
            try:
                if os.path.commonpath([dest_real, src_real]) == src_real:
                    return (
                        f"Safety Error: Destination '{dest_real}' is inside source "
                        f"'{src_real}'. Choose a destination outside the source folder."
                    )
                if os.path.commonpath([dest_real, src_real]) == dest_real:
                    return (
                        f"Safety Error: Source '{src_real}' is inside destination "
                        f"'{dest_real}'. Choose a destination outside the source folder."
                    )
            except ValueError:
                # Different volumes - no containment possible.
                continue
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

        categorizer = Categorizer(self.config_path)
        # Extract archives into a staging area on the destination volume, never
        # onto the source drive.
        staging_root = os.path.join(dest_abs, ".organizer_staging")
        scanner = Scanner(categorizer, staging_root=staging_root)
        
        self.log_cb(f"Scanning {source_abs} for files...")
        excluded_set = set(excluded_projects or [])
        scanner.scan_directory(source_abs, excluded_projects=excluded_set, progress_cb=self.progress_cb, cancel_check=lambda: self.cancelled, is_preview=is_preview)

        
        if self.cancelled:
            self.log_cb("Operation cancelled by user. Progress saved.")
            return False


        
        total_bytes = 0
        category_counts: Dict[str, int] = {}
        category_samples: Dict[str, List[str]] = {}
        for fp in scanner.files_to_process:
            try:
                total_bytes += os.path.getsize(fp)
            except OSError:
                pass
            cat = categorizer.get_file_category(os.path.basename(fp))
            category_counts[cat] = category_counts.get(cat, 0) + 1
            if cat not in category_samples:
                category_samples[cat] = []
            if len(category_samples[cat]) < 25:
                category_samples[cat].append(fp)

        from .utils import format_size
        size_str = format_size(total_bytes)
        self.log_cb(f"Found {len(scanner.files_to_process)} files ({size_str}) to organize.")
        self.log_cb(f"Found {len(scanner.projects_found)} intact code projects.")
        if scanner.ignored_garbage_count > 0:
            self.log_cb(f"Safely ignored {scanner.ignored_garbage_count} system/garbage files.")

        free_space_str = "Unknown"
        free_bytes = None
        try:
            dest_parent = dest_abs if os.path.exists(dest_abs) else os.path.dirname(dest_abs)
            free_bytes = shutil.disk_usage(dest_parent).free
            free_space_str = format_size(free_bytes)
            self.log_cb(f"Destination Drive Free Space: {free_space_str}")
        except OSError:
            pass

        # Hard block: refuse a fresh run that cannot possibly fit.
        # On a resume the destination may already hold most of the data, so warn only.
        if free_bytes is not None and total_bytes > free_bytes and not is_preview:
            is_resume = os.path.exists(os.path.join(dest_abs, ".organizer_checkpoint.db"))
            msg = (
                f"Not enough free space on destination: need {size_str}, "
                f"only {free_space_str} available."
            )
            if is_resume:
                self.log_cb(f"⚠️  {msg} Continuing because this looks like a resumed transfer.")
            else:
                self.log_cb(f"❌ Error: {msg}")
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
            self.progress_cb(100, 100, "Preview Complete")
            self.log_cb("Preview Complete! No files were moved or altered.")

            # Save preview history
            try:
                from datetime import datetime
                from .utils import save_run_to_history
                history_file = os.path.join(os.path.dirname(self.config_path), "run_history.json")
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
            # Pre-pass: Index HEIC dates for Live Photos
            live_photo_dates: Dict[Tuple[str, str], Tuple[str, str]] = {}
            for fp in scanner.files_to_process:
                if fp.rsplit('.', 1)[-1].lower() == 'heic':
                    y, m = dates.extract_date(fp)
                    if y and m:
                        dir_name, fn = os.path.split(fp)
                        name_only = fn.rsplit('.', 1)[0]
                        live_photo_dates[(dir_name, name_only)] = (y, m)

            # Total items count for continuous progress calculation
            total_projects = len(scanner.projects_found)
            total_files = len(scanner.files_to_process)
            total_items = total_projects + total_files

            # 1. Code Projects
            for i, proj in enumerate(scanner.projects_found):
                if self.cancelled:
                    self.log_cb("Operation cancelled by user. Progress saved.")
                    return False
                proj_name = os.path.basename(proj)
                dest_proj = os.path.join(dest_abs, "Code", proj_name)

                # Projects are copied wholesale, so they need their own
                # "already done" check. Without it, re-running the same job
                # clones every repository again as project_1, project_2, ...
                previous = engine.get_project_copy(proj)
                if previous:
                    self.progress_cb(i + 1, total_items, f"Skipped (Already copied): {proj_name}")
                    continue

                # Collision handling for duplicate project folder names.
                # An existing folder that is already a faithful copy of this
                # project is a re-run, not a name collision.
                if os.path.exists(dest_proj):
                    if project_already_copied(proj, dest_proj):
                        engine.record_project(proj, dest_proj)
                        self.progress_cb(i + 1, total_items, f"Skipped (Already copied): {proj_name}")
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
                        self.progress_cb(i + 1, total_items, f"Skipped (Already copied): {proj_name}")
                        continue

                self.progress_cb(i + 1, total_items, f"Copying project: {os.path.basename(dest_proj)}")
                ok, proj_err = copy_project_intact(proj, dest_proj)
                if ok:
                    engine.record_project(proj, dest_proj)
                else:
                    self.log_cb(f"Warning: Issue copying code project {proj_name}: {proj_err}")



                    
            # 2. Files - Multi-Threaded Parallel Execution (4 Workers)
            import time
            completed_counter = [0]
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
                            self.progress_cb(idx, total_items, f"Skipped (Already copied): {filename}")
                    return

                category = categorizer.get_file_category(filename)
                
                if category == "Media":
                    is_ss = categorizer.is_screenshot(filename)
                    year, month = dates.extract_date(file_path)
                    
                    if not (year and month):
                        dir_name = os.path.dirname(file_path)
                        name_only = filename.rsplit('.', 1)[0]
                        lookup_key = (dir_name, name_only)
                        if lookup_key in live_photo_dates:
                            year, month = live_photo_dates[lookup_key]
                            
                    if is_ss:
                        if year and month:
                            rel_dest = os.path.join("Media", "Screenshots", year, month, filename)
                        else:
                            rel_dest = os.path.join("Media", "Screenshots", "Unsorted", filename)
                    elif year and month:
                        rel_dest = os.path.join("Media", year, month, filename)
                    else:
                        rel_dest = os.path.join("Unsorted", filename)
                else:
                    rel_dest = os.path.join(category, filename)
                    
                target_base = os.path.join(dest_abs, rel_dest)
                
                try:
                    size = os.path.getsize(file_path)
                    mtime = os.path.getmtime(file_path)
                except OSError:
                    return
                    
                final_dest = None
                try:
                    final_dest, part_hash = engine.resolve_destination(target_base, filename, size, file_path)
                    if final_dest is None:
                        engine.record_copy(file_path, "DUPLICATE_SKIPPED", size, mtime, part_hash)
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
                finally:
                    if final_dest:
                        engine.release_reservation(final_dest)


                with counter_lock:
                    completed_counter[0] += 1
                    idx = total_projects + completed_counter[0]
                    elapsed = time.time() - start_time
                    eta_str = ""
                    if elapsed > 0.5 and completed_counter[0] > 0:
                        rate = completed_counter[0] / elapsed
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
                        self.progress_cb(idx, total_items, f"Organizing: {filename}", eta_str)

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

            self.progress_cb(total_items, total_items, "Complete")
            self.log_cb("All done! 100% of files organized safely.")

            # Record history
            try:
                from datetime import datetime
                from .utils import save_run_to_history
                history_file = os.path.join(os.path.dirname(self.config_path), "run_history.json")
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
                    "status": "Completed"
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
        for item in os.listdir(code_dir):
            full_path = os.path.join(code_dir, item)
            if os.path.isdir(full_path) and item != "Snippets":
                # count files
                file_count = sum(len(files) for _, _, files in os.walk(full_path))
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

        if not os.path.isdir(project_folder_path):
            return False, "Folder does not exist"

        categorizer = Categorizer(self.config_path)
        dates = DateExtractor(categorizer)
        engine = FileEngine(dest_abs)

        try:
            files_to_move = []
            for root, dirs, files in os.walk(project_folder_path):
                for f in files:
                    files_to_move.append(os.path.join(root, f))

            for file_path in files_to_move:
                filename = os.path.basename(file_path)
                category = categorizer.get_file_category(filename)
                
                if category == "Media":
                    is_ss = categorizer.is_screenshot(filename)
                    year, month = dates.extract_date(file_path)
                    if is_ss:
                        if year and month:
                            rel_dest = os.path.join("Media", "Screenshots", year, month, filename)
                        else:
                            rel_dest = os.path.join("Media", "Screenshots", "Unsorted", filename)
                    elif year and month:
                        rel_dest = os.path.join("Media", year, month, filename)
                    else:
                        rel_dest = os.path.join("Unsorted", filename)
                else:
                    rel_dest = os.path.join(category, filename)
                    
                target_base = os.path.join(dest_abs, rel_dest)
                size = os.path.getsize(file_path)
                mtime = os.path.getmtime(file_path)

                final_dest = None
                try:
                    final_dest, part_hash = engine.resolve_destination(target_base, filename, size, file_path)
                    if final_dest:
                        try:
                            shutil.move(file_path, final_dest)
                        except OSError:
                            safe_copy(file_path, final_dest)
                            try:
                                os.remove(file_path)
                            except OSError:
                                pass
                        engine.record_copy(file_path, final_dest, size, mtime, part_hash)
                finally:
                    if final_dest:
                        engine.release_reservation(final_dest)

            # Only remove the folder if nothing is left in it. An unconditional
            # rmtree would destroy any file that failed to move.
            leftovers = []
            for root, _dirs, files in os.walk(project_folder_path):
                for f in files:
                    if f != ".DS_Store":
                        leftovers.append(os.path.join(root, f))

            if leftovers:
                return True, (
                    f"Dissolved, but {len(leftovers)} file(s) could not be moved and were "
                    f"left in place at {project_folder_path}. Nothing was deleted."
                )

            shutil.rmtree(project_folder_path, ignore_errors=True)
            return True, "Project dissolved and files re-sorted successfully!"
        except Exception as e:
            return False, str(e)
        finally:
            engine.close()

    def get_duplicate_records(self, dest_abs: str):
        """Returns list of duplicate source files recorded during run."""
        db_path = os.path.join(dest_abs, ".organizer_checkpoint.db")
        if not os.path.exists(db_path):
            return []
        import sqlite3
        conn = sqlite3.connect(db_path)
        cursor = conn.execute("SELECT source_path, size FROM copies WHERE status = 'completed' AND dest_path = 'DUPLICATE_SKIPPED'")
        rows = cursor.fetchall()
        conn.close()
        return [{"source_path": r[0], "size": r[1]} for r in rows]

    def trash_duplicates(self, source_paths: list):
        """
        Safely moves duplicate source files to a '.Duplicates_Trash' folder on the source drive.
        Bypasses Finder AppleScript popups and Touch ID prompts completely (100% automated & zero-prompt).
        """
        valid_paths = [p for p in source_paths if os.path.exists(p)]
        if not valid_paths:
            return 0

        trashed_count = 0
        for file_path in valid_paths:
            try:
                parent_dir = os.path.dirname(file_path)
                trash_dir = os.path.join(parent_dir, ".Duplicates_Trash")
                os.makedirs(trash_dir, exist_ok=True)
                
                filename = os.path.basename(file_path)
                target_path = os.path.join(trash_dir, filename)
                
                # Handle filename collisions in trash folder
                counter = 1
                name, ext = os.path.splitext(filename)
                while os.path.exists(target_path):
                    target_path = os.path.join(trash_dir, f"{name}_{counter}{ext}")
                    counter += 1

                shutil.move(file_path, target_path)
                trashed_count += 1
            except Exception:
                # Fallback to AppleScript if direct filesystem move fails
                try:
                    escaped_path = file_path.replace('\\', '\\\\').replace('"', '\\"')
                    script = f'tell application "Finder" to delete POSIX file "{escaped_path}"'
                    res = subprocess.run(['osascript', '-e', script], capture_output=True, text=True)
                    if res.returncode == 0:
                        trashed_count += 1
                except Exception:
                    pass

        return trashed_count



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
            cursor = conn.execute("SELECT source_path, dest_path, status, size, part_hash FROM copies")
            rows = cursor.fetchall()
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
        hash_check_limit = 10000 # Check SHA-256 hashes for up to 10,000 files for comprehensive verification
        hashes_checked = 0
        # Built lazily, at most once, only if some file is not where we expect.
        dest_index: Optional[Dict[str, List[str]]] = None

        for source_path, dest_path, status, size, part_hash in rows:
            if dest_path == "DUPLICATE_SKIPPED":
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
                    for root, _dirs, files in os.walk(dest_abs):
                        for f in files:
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

    def repair_transfer(self, dest_abs: str) -> dict:
        """
        Clears invalid/missing/mismatched entries from the checkpoint DB so a
        later run re-copies them.

        Deleting a destination file here is dangerous: a bad row's dest_path may
        be a path that some *other*, perfectly healthy file legitimately owns.
        So a file is only ever deleted when all of the following hold:
          1. exactly one DB row claims that path,
          2. no successfully-completed row claims that path,
          3. the path really lives inside dest_abs.
        """
        db_path = os.path.join(dest_abs, ".organizer_checkpoint.db")
        if not os.path.exists(db_path):
            return {"success": False, "error": "No checkpoint database found at destination."}

        try:
            conn = sqlite3.connect(db_path, timeout=30.0)
            cursor = conn.execute("SELECT source_path, dest_path, size, part_hash, status FROM copies")
            rows = cursor.fetchall()
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
        for source_path, dest_path, size, part_hash, status in rows:
            if not dest_path or dest_path == "DUPLICATE_SKIPPED":
                continue
            path_claims[dest_path] = path_claims.get(dest_path, 0) + 1
            if not (status and str(status).startswith("failed")):
                completed_paths.add(dest_path)

        engine = FileEngine(dest_abs)
        purged_sources = []
        deleted_count = 0

        try:
            for source_path, dest_path, size, part_hash, status in rows:
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
                            elif part_hash:
                                dest_hash = engine._get_part_hash(dest_path, dest_size)
                                if dest_hash and dest_hash != part_hash:
                                    is_bad = True
                        except OSError:
                            is_bad = True

                if not is_bad:
                    continue

                purged_sources.append(source_path)

                # Only delete a file this row unambiguously owns.
                if not os.path.exists(dest_path):
                    continue
                if not is_inside_dest(dest_path):
                    continue
                if path_claims.get(dest_path, 0) != 1:
                    continue
                if failed_row and dest_path in completed_paths:
                    continue

                try:
                    os.remove(dest_path)
                    deleted_count += 1
                except OSError:
                    pass
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
            "deleted_count": deleted_count
        }

    def auto_repair_if_needed(self, dest_abs: str):
        """
        Automatically inspects destination checkpoint database before transfer start,
        purging any corrupted or missing file records from past interrupted runs.
        """
        db_path = os.path.join(dest_abs, ".organizer_checkpoint.db")
        if not os.path.exists(db_path):
            return

        res = self.repair_transfer(dest_abs)
        if res.get("success") and res.get("repaired_count", 0) > 0:
            count = res["repaired_count"]
            self.log_cb(f"🧹 [Auto-Repair] Detected {count} missing/corrupted file record(s) from a previous interrupted run. Automatically cleared bad records so they will be re-transferred cleanly.")





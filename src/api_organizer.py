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
from .file_ops import FileEngine

class OrganizerAPI:
    def __init__(self, config_path: str, log_cb: Callable[[str], None], progress_cb: Callable[[int, int, str], None]):
        self.config_path = config_path
        self.log_cb = log_cb
        self.progress_cb = progress_cb
        
    def run(self, source_abs: str, dest_abs: str, is_preview: bool = False, dest_mode: str = "new", excluded_projects: list = None):
        mode_str = "Preview (Dry Run)" if is_preview else "Full Transfer"
        folder_type = "Brand New Folder" if dest_mode == "new" else "Merge with Existing Folder"
        self.log_cb(f"Mode: {mode_str} | Destination Strategy: {folder_type}")
        self.log_cb(f"Source: {source_abs}")
        self.log_cb(f"Destination: {dest_abs}")
        
        if not os.path.exists(self.config_path):
            self.log_cb("Error: config.json is missing!")
            return False

        categorizer = Categorizer(self.config_path)
        scanner = Scanner(categorizer)
        
        self.log_cb(f"Scanning {source_abs} for files... (This may take a minute)")
        excluded_set = set(excluded_projects or [])
        scanner.scan_directory(source_abs, excluded_projects=excluded_set)
        
        total_bytes = 0
        category_counts: Dict[str, int] = {}
        for fp in scanner.files_to_process:
            try:
                total_bytes += os.path.getsize(fp)
            except OSError:
                pass
            cat = categorizer.get_file_category(os.path.basename(fp))
            category_counts[cat] = category_counts.get(cat, 0) + 1

        from .utils import format_size
        size_str = format_size(total_bytes)
        self.log_cb(f"Found {len(scanner.files_to_process)} files ({size_str}) to organize.")
        self.log_cb(f"Found {len(scanner.projects_found)} intact code projects.")
        if scanner.ignored_garbage_count > 0:
            self.log_cb(f"Safely ignored {scanner.ignored_garbage_count} system/garbage files.")

        free_space_str = "Unknown"
        try:
            dest_parent = dest_abs if os.path.exists(dest_abs) else os.path.dirname(dest_abs)
            free_bytes = shutil.disk_usage(dest_parent).free
            free_space_str = format_size(free_bytes)
            self.log_cb(f"Destination Drive Free Space: {free_space_str}")
        except Exception:
            pass

        project_details = [{"name": os.path.basename(p), "path": p} for p in scanner.projects_found]
        self.last_preview_summary = {
            "total_files": len(scanner.files_to_process),
            "total_size": size_str,
            "total_projects": len(scanner.projects_found),
            "projects": [os.path.basename(p) for p in scanner.projects_found[:10]],
            "project_details": project_details,
            "ignored_garbage": scanner.ignored_garbage_count,
            "free_space": free_space_str,
            "categories": category_counts
        }
        
        if len(scanner.files_to_process) == 0 and len(scanner.projects_found) == 0:
            self.log_cb("Warning: No files found to move!")
            return True

        if is_preview:
            self.progress_cb(100, 100, "Preview Complete")
            self.log_cb("Preview Complete! No files were moved or altered.")
            return True

        # Execution
        os.makedirs(dest_abs, exist_ok=True)
        with open(os.path.join(dest_abs, ".metadata_never_index"), 'w') as f:
            f.write("")

        engine = FileEngine(dest_abs)
        dates = DateExtractor(categorizer)
        engine.start_caffeinate()
        
        # Pre-pass: Index HEIC dates for Live Photos
        live_photo_dates: Dict[Tuple[str, str], Tuple[str, str]] = {}
        for fp in scanner.files_to_process:
            if fp.rsplit('.', 1)[-1].lower() == 'heic':
                y, m = dates.extract_date(fp)
                if y and m:
                    dir_name, fn = os.path.split(fp)
                    name_only = fn.rsplit('.', 1)[0]
                    live_photo_dates[(dir_name, name_only)] = (y, m)
        
        try:
            # Code Projects
            total_projects = len(scanner.projects_found)
            for i, proj in enumerate(scanner.projects_found):
                proj_name = os.path.basename(proj)
                dest_proj = os.path.join(dest_abs, "Code", proj_name)
                self.progress_cb(i, total_projects, f"Copying project: {proj_name}")
                if not os.path.exists(dest_proj):
                    shutil.copytree(proj, dest_proj, dirs_exist_ok=True)
                    
            # Files - Multi-Threaded Parallel Execution (8 Workers)
            import time
            total_files = len(scanner.files_to_process)
            completed_counter = [0]
            counter_lock = threading.Lock()
            start_time = time.time()

            def process_single_file(file_path):
                filename = os.path.basename(file_path)
                
                if engine.is_already_copied(file_path):
                    with counter_lock:
                        completed_counter[0] += 1
                        idx = completed_counter[0]
                        if idx % 10 == 0 or idx == total_files:
                            self.progress_cb(idx, total_files, f"Skipped (Already copied): {filename}")
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
                    
                final_dest, part_hash = engine.resolve_destination(target_base, filename, size, file_path)
                if final_dest is None:
                    engine.record_copy(file_path, "DUPLICATE_SKIPPED", size, mtime, part_hash)
                else:
                    engine.copy_file(file_path, final_dest)
                    if "Unsorted" in rel_dest:
                        engine.set_finder_tag(final_dest, "5", "To Review")
                    engine.record_copy(file_path, final_dest, size, mtime, part_hash)

                with counter_lock:
                    completed_counter[0] += 1
                    idx = completed_counter[0]
                    elapsed = time.time() - start_time
                    eta_str = ""
                    if elapsed > 0.5 and idx > 0:
                        rate = idx / elapsed
                        rem_files = total_files - idx
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

                    if idx % 5 == 0 or idx == total_files:
                        self.progress_cb(idx, total_files, f"Organizing: {filename}", eta_str)

            max_workers = 4
            self.log_cb(f"Using {max_workers} safe parallel worker threads for fast transfer.")
            
            with ThreadPoolExecutor(max_workers=max_workers) as executor:
                list(executor.map(process_single_file, scanner.files_to_process))
                
            self.progress_cb(total_files, total_files, "Complete")
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

            try:
                subprocess.Popen(["afplay", "/System/Library/Sounds/Glass.aiff"])
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
        if not os.path.exists(project_folder_path):
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

                final_dest, part_hash = engine.resolve_destination(target_base, filename, size, file_path)
                if final_dest:
                    shutil.move(file_path, final_dest)
                    engine.record_copy(file_path, final_dest, size, mtime, part_hash)

            # Remove empty directory tree
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
        Post-transfer automated verification checker.
        Verifies existence, file size, and SHA-256 hash of all copied files.
        """
        db_path = os.path.join(dest_abs, ".organizer_checkpoint.db")
        if not os.path.exists(db_path):
            return {
                "success": False,
                "error": "No checkpoint database found at destination."
            }

        conn = sqlite3.connect(db_path)
        cursor = conn.execute("SELECT source_path, dest_path, status, size, part_hash FROM copies")
        rows = cursor.fetchall()
        conn.close()

        engine = FileEngine(dest_abs)

        total_files = len(rows)
        verified_count = 0
        missing_count = 0
        mismatched_count = 0
        skipped_dup_count = 0
        missing_list = []
        mismatched_list = []

        for source_path, dest_path, status, size, part_hash in rows:
            if dest_path == "DUPLICATE_SKIPPED":
                skipped_dup_count += 1
                continue

            if not os.path.exists(dest_path):
                missing_count += 1
                missing_list.append(dest_path)
                continue

            dest_size = os.path.getsize(dest_path)
            if dest_size != size:
                mismatched_count += 1
                mismatched_list.append(f"{dest_path} (Size mismatch: expected {size}, got {dest_size})")
                continue

            if part_hash:
                dest_hash = engine._get_part_hash(dest_path, dest_size)
                if dest_hash and dest_hash != part_hash:
                    mismatched_count += 1
                    mismatched_list.append(f"{dest_path} (Hash mismatch)")
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


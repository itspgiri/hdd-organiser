import os
import shutil
from typing import Callable, Dict, Tuple

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
        live_photo_dates: Dict[str, Tuple[str, str]] = {}
        for fp in scanner.files_to_process:
            fn = os.path.basename(fp)
            if fn.lower().endswith('.heic'):
                y, m = dates.extract_date(fp)
                if y and m:
                    name_only, _ = os.path.splitext(fn)
                    lookup_key = os.path.join(os.path.dirname(fp), name_only)
                    live_photo_dates[lookup_key] = (y, m)
        
        try:
            # Code Projects
            total_projects = len(scanner.projects_found)
            for i, proj in enumerate(scanner.projects_found):
                proj_name = os.path.basename(proj)
                dest_proj = os.path.join(dest_abs, "Code", proj_name)
                self.progress_cb(i, total_projects, f"Copying project: {proj_name}")
                if not os.path.exists(dest_proj):
                    shutil.copytree(proj, dest_proj, dirs_exist_ok=True)
                    
            # Files
            total_files = len(scanner.files_to_process)
            for i, file_path in enumerate(scanner.files_to_process):
                filename = os.path.basename(file_path)
                
                # Update progress
                if i % 5 == 0 or i == total_files - 1:
                    self.progress_cb(i, total_files, f"Organizing: {filename}")
                
                if engine.is_already_copied(file_path):
                    continue
                    
                category = categorizer.get_file_category(filename)
                
                if category == "Media":
                    is_ss = categorizer.is_screenshot(filename)
                    year, month = dates.extract_date(file_path)
                    
                    name_only, ext = os.path.splitext(filename)
                    lookup_key = os.path.join(os.path.dirname(file_path), name_only)
                    
                    if not (year and month):
                        if lookup_key in live_photo_dates:
                            year, month = live_photo_dates[lookup_key]
                            
                    if is_ss:
                        if year and month:
                            rel_dest = os.path.join("Media", year, month, "Screenshots", filename)
                        else:
                            rel_dest = os.path.join("Media", "Screenshots", filename)
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
                    continue
                    
                final_dest, part_hash = engine.resolve_destination(target_base, filename, size, file_path)
                if final_dest is None:
                    engine.record_copy(file_path, "DUPLICATE_SKIPPED", size, mtime, part_hash)
                    continue
                    
                engine.copy_file(file_path, final_dest)
                if "Unsorted" in rel_dest:
                    engine.set_finder_tag(final_dest, "5", "To Review")
                engine.record_copy(file_path, final_dest, size, mtime, part_hash)
                
            self.progress_cb(total_files, total_files, "Complete")
            self.log_cb("All done! 100% of files organized safely.")
            try:
                import subprocess
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
                    year, month = dates.extract_date(file_path)
                    if year and month:
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
        """Safely moves duplicate source files to macOS Trash via osascript."""
        import subprocess
        trashed_count = 0
        for path in source_paths:
            if os.path.exists(path):
                escaped_path = path.replace('\\', '\\\\').replace('"', '\\"')
                script = f'tell application "Finder" to delete POSIX file "{escaped_path}"'
                res = subprocess.run(['osascript', '-e', script], capture_output=True, text=True)
                if res.returncode == 0:
                    trashed_count += 1
        return trashed_count

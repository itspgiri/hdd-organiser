import os
import sys
import time
import shutil
import argparse
import subprocess
from datetime import datetime
from typing import Dict, Tuple

from rich.prompt import Prompt, Confirm
from .utils import (
    print_header, print_success, print_error, print_info, print_warning,
    get_progress_bar, format_size, get_default_history_file, save_run_to_history,
)
from .categorizer import Categorizer
from .scanner import Scanner, split_source_paths
from .dates import DateExtractor
from .file_ops import (
    FileEngine, safe_copy, copy_project_intact, project_already_copied,
    PROJECT_IGNORE_PATTERNS, _HAS_NATIVE_COPYFILE,
)
from .api_organizer import (
    OrganizerAPI, compute_relative_destination, build_live_photo_dates,
    _is_apfs_volume,
)


def run_cli():
    print_header("Drive Organizer 🚀")

    parser = argparse.ArgumentParser(description="Organize hard drives safely.")
    parser.add_argument("source", nargs="?", help="Source directory (or comma-separated directories)")
    parser.add_argument("dest", nargs="?", help="Destination directory")
    parser.add_argument("--preview", action="store_true", help="Run a dry-run preview without copying")
    parser.add_argument("--copy", action="store_true", help="Execute file transfer without asking for confirmation")
    args = parser.parse_args()

    source = args.source
    if source is None:
        print_info("Welcome! Let's organize your drive.")
        print_info("Opening folder selection dialog...")
        source = _macos_choose_folder("Select your messy SOURCE folder to organize:")
        if not source:
            print_error("Operation cancelled.")
            return

    dest = args.dest
    if dest is None:
        print_info("Opening folder selection dialog for destination...")
        dest = _macos_choose_folder("Select your DESTINATION folder (where organized files will go):")
        if not dest:
            print_error("Operation cancelled.")
            return

    sources_list = split_source_paths(source)
    if not sources_list:
        print_error("No valid source directory provided.")
        return

    if not dest or not str(dest).strip():
        print_error("Safety Error: Destination path cannot be empty.")
        return

    for src_item in sources_list:
        if not os.path.exists(src_item):
            print_error(f"Source path does not exist: {src_item}")
            return
        if not os.path.isdir(src_item):
            print_error(f"Source path is not a directory: {src_item}")
            return

    source_abs_list = [os.path.abspath(s) for s in sources_list]
    source_abs = ", ".join(source_abs_list) if len(source_abs_list) > 1 else source_abs_list[0]
    dest_abs = os.path.abspath(os.path.expanduser(dest.strip()))

    # Shared with the GUI: realpath+commonpath, so symlinks and sibling names
    # like /data vs /data-backup are handled correctly.
    path_error = OrganizerAPI.validate_paths(source_abs_list, dest_abs)
    if path_error:
        print_error(path_error)
        return

    print_success(f"Source: {source_abs}")
    print_success(f"Destination: {dest_abs}")

    if getattr(sys, 'frozen', False):
        config_path = os.path.join(sys._MEIPASS, "config.json")
    else:
        config_path = os.path.join(os.path.dirname(__file__), "config.json")

    if not os.path.exists(config_path):
        print_error("config.json is missing!")
        return

    print_info("Loading configurations...")
    categorizer = Categorizer(config_path)
    # Stage unzipped archives on the destination, never on the source drive.
    scanner = Scanner(categorizer, staging_root=os.path.join(dest_abs, ".organizer_staging"))

    print_header("Scan & Preview")
    print_info(f"Scanning {source_abs} for files... (This may take a minute)")
    # Always scan in preview mode first unless --copy was explicitly passed,
    # so archives are never extracted onto disk before the user confirms.
    initial_preview_scan = bool(args.preview or not args.copy)
    scanner.scan_directory(source_abs_list, is_preview=initial_preview_scan)

    # Calculate total size of files and intact code projects to process
    total_size_bytes = 0
    unmeasured_files = 0
    for fp in scanner.files_to_process:
        try:
            total_size_bytes += os.path.getsize(fp)
        except OSError:
            unmeasured_files += 1

    ignore_fn = shutil.ignore_patterns(*PROJECT_IGNORE_PATTERNS)
    for proj_path in scanner.projects_found:
        for r, dirs, fs in os.walk(proj_path):
            ignored = ignore_fn(r, dirs + fs)
            dirs[:] = [d for d in dirs if d not in ignored]
            for f in fs:
                if f in ignored:
                    continue
                try:
                    total_size_bytes += os.path.getsize(os.path.join(r, f))
                except OSError:
                    pass

    if unmeasured_files and initial_preview_scan:
        import zipfile
        seen_zips = set()
        for zip_path in scanner.processed_zips:
            zp_real = os.path.realpath(zip_path) if os.path.exists(zip_path) else zip_path
            if zp_real in seen_zips:
                continue
            seen_zips.add(zp_real)
            try:
                with zipfile.ZipFile(zip_path, 'r') as zf:
                    total_size_bytes += sum(m.file_size for m in zf.infolist() if not m.is_dir())
            except Exception:
                try:
                    total_size_bytes += os.path.getsize(zip_path)
                except OSError:
                    pass

    size_str = format_size(total_size_bytes)
    print_success(f"Found {len(scanner.files_to_process)} files ({size_str}) to organize.")
    print_success(f"Found {len(scanner.projects_found)} entire code projects.")
    if scanner.ignored_garbage_count > 0:
        print_info(f"Safely ignored {scanner.ignored_garbage_count} system/garbage files.")
    if scanner.skipped_dir_breakdown:
        detail = ", ".join(
            f"{name} x{count}"
            for name, count in sorted(scanner.skipped_dir_breakdown.items(), key=lambda kv: -kv[1])
        )
        print_info(f"Skipped build/cache folders (not copied): {detail}")

    # Pre-flight disk space check
    dest_parent = dest_abs
    while dest_parent and not os.path.exists(dest_parent):
        parent = os.path.dirname(dest_parent)
        if parent == dest_parent:
            break
        dest_parent = parent

    free_bytes = None
    try:
        free_bytes = shutil.disk_usage(dest_parent).free
        print_info(f"Destination free space: {format_size(free_bytes)}")
    except Exception:
        pass

    if free_bytes is not None and total_size_bytes > free_bytes and not args.preview:
        same_volume = False
        try:
            dest_dev = os.stat(dest_parent).st_dev
            same_volume = all(os.stat(s).st_dev == dest_dev for s in source_abs_list if os.path.exists(s))
        except OSError:
            pass
        is_resume = os.path.exists(os.path.join(dest_abs, ".organizer_checkpoint.db"))
        if is_resume:
            print_warning("Destination free space is less than total source size, but continuing because this is a resumed transfer.")
        elif same_volume and _HAS_NATIVE_COPYFILE and _is_apfs_volume(dest_parent):
            print_warning("Destination free space is less than total source size, but continuing on same APFS volume (zero-copy cloning).")
        else:
            print_error(f"Not enough free space on destination: need {size_str}, only {format_size(free_bytes)} available.")
            scanner.cleanup_staging()
            return

    if len(scanner.files_to_process) == 0 and len(scanner.projects_found) == 0:
        print_warning("No files to move!")
        if not initial_preview_scan:
            scanner.cleanup_staging()
        return

    print_warning("This is a preview. No files have been moved yet.")
    if args.preview:
        print_info("Preview complete. Exiting (--preview flag provided).")
        return

    if not args.copy:
        if not Confirm.ask("Do you want to proceed with copying files?"):
            print_warning("Operation cancelled by user.")
            return
        # If archives were only inspected in memory during the preview scan,
        # perform the real extraction scan now that the user has confirmed.
        if scanner.processed_zips:
            scanner.scan_directory(source_abs_list, is_preview=False)

    # -------- Execution Phase --------
    print_header("Executing File Transfers")

    # 1. Spotlight Suppression, Auto-Repair & Write Check
    try:
        os.makedirs(dest_abs, exist_ok=True)
        with open(os.path.join(dest_abs, ".metadata_never_index"), 'w') as f:
            f.write("")
        api_helper = OrganizerAPI(config_path, print_info, lambda *a, **k: None)
        api_helper.auto_repair_if_needed(dest_abs)
        engine = FileEngine(dest_abs)
    except (OSError, IOError, Exception) as e:
        if getattr(e, 'errno', None) == 30 or "Read-only" in str(e) or "readonly" in str(e).lower():
            print_error(
                f"Read-only file system error on '{dest_abs}'.\n"
                "On macOS, external hard drives formatted as NTFS are read-only by default.\n"
                "Please choose a writeable destination drive, reformat the drive to exFAT/APFS, or install an NTFS write driver."
            )
        else:
            print_error(f"Cannot write to destination directory '{dest_abs}': {str(e)}")
        return

    dates = DateExtractor(categorizer)
    engine.start_caffeinate()

    try:
        # Pre-pass: Index capture dates for Live Photo pairs
        live_photo_dates = build_live_photo_dates(scanner.files_to_process, dates)

        failed_projects = []
        failed_files = []

        with get_progress_bar() as progress:

            # 2. Transfer Code Projects Intact
            proj_task = progress.add_task("[magenta]Copying Code Projects...", total=len(scanner.projects_found))
            for proj in scanner.projects_found:
                proj_name = os.path.basename(proj)
                dest_proj = os.path.join(dest_abs, "Code", proj_name)

                if engine.is_project_dissolved(proj):
                    progress.advance(proj_task)
                    continue

                # Projects are copied wholesale, so they need their own
                # "already done" check, or a re-run clones each repository
                # again as project_1, project_2, ...
                if engine.get_project_copy(proj):
                    progress.advance(proj_task)
                    continue

                if os.path.exists(dest_proj):
                    if project_already_copied(proj, dest_proj):
                        engine.record_project(proj, dest_proj)
                        progress.advance(proj_task)
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
                        progress.advance(proj_task)
                        continue

                ok, proj_err = copy_project_intact(proj, dest_proj)
                if ok:
                    engine.record_project(proj, dest_proj)
                else:
                    failed_projects.append(proj_name)
                    print_warning(f"Issue copying code project {proj_name}: {proj_err}")

                progress.advance(proj_task)

            # 3. Transfer Files
            file_task = progress.add_task("[cyan]Organizing Files...", total=len(scanner.files_to_process))

            for file_path in scanner.files_to_process:
                progress.advance(file_task)

                if engine.is_already_copied(file_path):
                    continue

                filename = os.path.basename(file_path)
                rel_dest = compute_relative_destination(
                    categorizer, dates, file_path, live_photo_dates
                )

                target_base = os.path.join(dest_abs, rel_dest)

                try:
                    size = os.path.getsize(file_path)
                    mtime = os.path.getmtime(file_path)
                except OSError as stat_err:
                    print_warning(
                        f"Skipped {filename}: it disappeared or became unreadable after the scan ({stat_err})."
                    )
                    engine.record_copy(
                        file_path, "", 0, 0.0, part_hash="",
                        status="failed: disappeared or unreadable after the scan"
                    )
                    failed_files.append(file_path)
                    continue

                final_dest = None
                try:
                    final_dest, part_hash, twin = engine.resolve_destination(target_base, filename, size, file_path)
                    if final_dest is None:
                        engine.record_copy(file_path, "DUPLICATE_SKIPPED", size, mtime, part_hash,
                                           duplicate_of=twin)
                    else:
                        engine.copy_file(file_path, final_dest)
                        # Component test, not substring, so a file named
                        # "Unsorted ideas.txt" isn't tagged To Review.
                        if "Unsorted" in rel_dest.split(os.sep)[:-1]:
                            engine.set_finder_tag(final_dest, "5", "To Review")
                        engine.record_copy(file_path, final_dest, size, mtime, part_hash)
                except Exception as file_err:
                    print_warning(f"Skipped problem file {filename}: {str(file_err)}")
                    engine.record_copy(
                        file_path, final_dest or "", size, mtime,
                        part_hash="", status=f"failed: {str(file_err)}"
                    )
                    failed_files.append(file_path)
                finally:
                    if final_dest:
                        engine.release_reservation(final_dest)

        scanner.cleanup_staging()

        try:
            history_file = get_default_history_file(os.path.dirname(config_path))
            timestamp_str = datetime.now().strftime("%B %d, %Y at %I:%M %p")
            run_record = {
                "id": f"run_{int(time.time())}",
                "timestamp": timestamp_str,
                "source": source_abs,
                "dest": dest_abs,
                "is_preview": False,
                "dest_mode": "cli",
                "total_files": len(scanner.files_to_process),
                "total_size": size_str,
                "projects_count": len(scanner.projects_found),
                "failed_files": len(failed_files),
                "failed_projects": len(failed_projects),
                "status": "Completed with errors" if (failed_files or failed_projects) else "Completed",
            }
            save_run_to_history(history_file, run_record)
        except Exception:
            pass

        if failed_files or failed_projects:
            print_warning(
                f"\nFinished with problems: {len(failed_files)} file(s) and "
                f"{len(failed_projects)} code project(s) were not copied (see the warnings above). "
                "Do not erase the source until they have been copied."
            )
        else:
            print_success("\nAll done! 100% of files organized safely.")

    except Exception as e:
        print_error(f"\nError occurred: {str(e)}")
        print_info("Don't worry, progress is saved. Run again to resume where it left off.")
    finally:
        engine.close()


def _macos_choose_folder(prompt_text: str) -> str:
    """Uses AppleScript to open a native macOS folder selection dialog."""
    # Prompt goes through argv, not string interpolation, so it cannot be
    # evaluated as AppleScript.
    script = '''
    on run argv
        try
            tell application (path to frontmost application as text)
                set theFolder to choose folder with prompt (item 1 of argv)
                POSIX path of theFolder
            end tell
        on error number -128
            return ""
        end try
    end run
    '''
    result = subprocess.run(
        ['osascript', '-e', script, prompt_text],
        capture_output=True, text=True
    )
    return result.stdout.strip()


if __name__ == "__main__":
    run_cli()


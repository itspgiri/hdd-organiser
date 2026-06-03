import os
import shutil
import argparse
import subprocess
from typing import Dict, Tuple

from rich.prompt import Prompt, Confirm
from src.utils import print_header, print_success, print_error, print_info, print_warning, get_progress_bar
from src.categorizer import Categorizer
from src.scanner import Scanner
from src.dates import DateExtractor
from src.file_ops import FileEngine

def main():
    print_header("Drive Organizer 🚀")
    
    parser = argparse.ArgumentParser(description="Organize hard drives safely.")
    parser.add_argument("source", nargs="?", help="Source directory")
    parser.add_argument("dest", nargs="?", help="Destination directory")
    parser.add_argument("--preview", action="store_true", help="Run a dry-run preview without copying")
    parser.add_argument("--copy", action="store_true", help="Execute file transfer without asking for confirmation")
    args = parser.parse_args()

    source = args.source
    if not source:
        print_info("Welcome! Let's organize your drive.")
        print_info("Opening folder selection dialog...")
        source = _macos_choose_folder("Select your messy SOURCE folder to organize:")
        if not source:
            print_error("Operation cancelled.")
            return
            
    dest = args.dest
    if not dest:
        print_info("Opening folder selection dialog for destination...")
        dest = _macos_choose_folder("Select your DESTINATION folder (where organized files will go):")
        if not dest:
            print_error("Operation cancelled.")
            return

    if not os.path.exists(source):
        print_error(f"Source path does not exist: {source}")
        return

    source_abs = os.path.abspath(source)
    dest_abs = os.path.abspath(dest)
    
    if dest_abs.startswith(source_abs):
        print_error("Safety Error: Destination folder cannot be inside the Source folder!")
        return

    print_success(f"Source: {source_abs}")
    print_success(f"Destination: {dest_abs}")
    
    config_path = os.path.join(os.path.dirname(__file__), "config.json")
    if not os.path.exists(config_path):
        print_error("config.json is missing!")
        return
        
    print_info("Loading configurations...")
    categorizer = Categorizer(config_path)
    scanner = Scanner(categorizer)
    
    print_header("Scan & Preview")
    print_info(f"Scanning {source_abs} for files... (This may take a minute)")
    scanner.scan_directory(source_abs)
    
    print_success(f"Found {len(scanner.files_to_process)} files to organize.")
    print_success(f"Found {len(scanner.projects_found)} entire code projects.")
    if scanner.ignored_garbage_count > 0:
        print_info(f"Safely ignored {scanner.ignored_garbage_count} system/garbage files.")
    
    if len(scanner.files_to_process) == 0 and len(scanner.projects_found) == 0:
        print_warning("No files to move!")
        return

    print_warning("This is a preview. No files have been moved yet.")
    if args.preview:
        print_info("Preview complete. Exiting (--preview flag provided).")
        return

    if not args.copy:
        if not Confirm.ask("Do you want to proceed with copying files?"):
            print_warning("Operation cancelled by user.")
            return

    # -------- Execution Phase --------
    print_header("Executing File Transfers")
    
    # 1. Spotlight Suppression
    os.makedirs(dest_abs, exist_ok=True)
    with open(os.path.join(dest_abs, ".metadata_never_index"), 'w') as f:
        f.write("")

    engine = FileEngine(dest_abs)
    dates = DateExtractor(categorizer)
    engine.start_caffeinate()
    
    # A tiny cache for Live Photos to pair MOV and HEIC dates
    live_photo_dates: Dict[str, Tuple[str, str]] = {}

    try:
        with get_progress_bar() as progress:
            # 2. Transfer Code Projects Intact
            proj_task = progress.add_task("[magenta]Copying Code Projects...", total=len(scanner.projects_found))
            for proj in scanner.projects_found:
                proj_name = os.path.basename(proj)
                dest_proj = os.path.join(dest_abs, "Code", proj_name)
                if not os.path.exists(dest_proj):
                    shutil.copytree(proj, dest_proj, dirs_exist_ok=True)
                progress.advance(proj_task)

            # 3. Transfer Files
            file_task = progress.add_task("[cyan]Organizing Files...", total=len(scanner.files_to_process))
            
            for file_path in scanner.files_to_process:
                progress.advance(file_task)
                
                if engine.is_already_copied(file_path):
                    continue
                
                filename = os.path.basename(file_path)
                category = categorizer.get_file_category(filename)
                
                # Determine relative destination
                if category == "Media":
                    year, month = dates.extract_date(file_path)
                    
                    # Live Photo Glue logic (Pairing HEIC and MOV)
                    name_only, ext = os.path.splitext(filename)
                    ext = ext.lower()
                    lookup_key = os.path.join(os.path.dirname(file_path), name_only)
                    
                    if year and month:
                        if ext == ".heic":
                            live_photo_dates[lookup_key] = (year, month)
                    else:
                        # Fallback for missing date
                        if ext == ".mov" and lookup_key in live_photo_dates:
                            year, month = live_photo_dates[lookup_key]
                            
                    if year and month:
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
                    continue # File disappeared during run

                final_dest = engine.resolve_destination(target_base, filename, size, file_path)
                
                if final_dest is None:
                    # Duplicate skipped
                    engine.record_copy(file_path, "DUPLICATE_SKIPPED", size, mtime)
                    continue
                    
                engine.copy_file(file_path, final_dest)
                
                # If it ended up in Unsorted, add the yellow Finder Tag "To Review"
                if "Unsorted" in rel_dest:
                    engine.set_finder_tag(final_dest, "5", "To Review")
                    
                engine.record_copy(file_path, final_dest, size, mtime)

        print_success("\nAll done! 100% of files organized safely.")
    
    except Exception as e:
        print_error(f"\nError occurred: {str(e)}")
        print_info("Don't worry, progress is saved. Run again to resume where it left off.")
    finally:
        engine.close()

def _macos_choose_folder(prompt_text: str) -> str:
    """Uses AppleScript to open a native macOS folder selection dialog."""
    script = f'''
    try
        tell application (path to frontmost application as text)
            set theFolder to choose folder with prompt "{prompt_text}"
            POSIX path of theFolder
        end tell
    on error number -128
        return ""
    end try
    '''
    result = subprocess.run(['osascript', '-e', script], capture_output=True, text=True)
    return result.stdout.strip()

if __name__ == "__main__":
    main()

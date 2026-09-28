import os
import sys
import json
import tempfile
import threading
from rich.console import Console
from rich.markup import escape
from rich.theme import Theme
from rich.progress import Progress, SpinnerColumn, TextColumn, BarColumn, TimeRemainingColumn

# Create a custom theme for a premium feel
custom_theme = Theme({
    "info": "dim cyan",
    "warning": "yellow",
    "error": "bold red",
    "success": "bold green",
    "highlight": "bold magenta"
})

console = Console(theme=custom_theme)
_HISTORY_LOCK = threading.Lock()

def print_header(text: str):
    """Prints a beautiful formatted header."""
    console.print(f"\n[bold white on #4f46e5] {escape(str(text))} [/]\n")

def print_success(text: str):
    console.print(f"[success]✓ {escape(str(text))}[/]")

def print_error(text: str):
    console.print(f"[error]✗ {escape(str(text))}[/]")

def print_warning(text: str):
    console.print(f"[warning]! {escape(str(text))}[/]")

def print_info(text: str):
    console.print(f"[info]i {escape(str(text))}[/]")

def get_progress_bar():
    """Returns a rich progress bar configured for premium Mac look."""
    return Progress(
        SpinnerColumn(spinner_name="dots"),
        TextColumn("[progress.description]{task.description}"),
        BarColumn(bar_width=40, complete_style="green", finished_style="bold green"),
        TextColumn("[progress.percentage]{task.percentage:>3.0f}%"),
        TimeRemainingColumn(),
        console=console
    )

def format_size(size_in_bytes: int) -> str:
    """Formats bytes into human readable format."""
    size_val = float(max(0, size_in_bytes or 0))
    for unit in ['B', 'KB', 'MB', 'GB', 'TB']:
        if size_val < 1024.0:
            return f"{size_val:.2f} {unit}"
        size_val /= 1024.0
    return f"{size_val:.2f} PB"

def get_default_history_file(base_dir: str = "") -> str:
    """Resolves the path for run_history.json.

    Avoids polluting the repo's src/run_history.json during unittest runs and
    avoids writing into ephemeral sys._MEIPASS when frozen by PyInstaller.
    """
    env_override = os.environ.get("DRIVE_ORGANIZER_HISTORY_FILE")
    if env_override:
        return env_override
    if "unittest" in sys.modules:
        return os.path.join(tempfile.gettempdir(), "drive_organizer_test_run_history.json")
    if getattr(sys, "frozen", False):
        return os.path.expanduser("~/.drive_organizer_history.json")
    if base_dir:
        return os.path.join(base_dir, "run_history.json")
    return os.path.join(os.path.dirname(os.path.abspath(__file__)), "run_history.json")

def load_run_history(history_file: str) -> list:
    if not history_file or not os.path.exists(history_file):
        return []
    try:
        with open(history_file, 'r', encoding='utf-8') as f:
            data = json.load(f)
            return data if isinstance(data, list) else []
    except Exception:
        return []

def save_run_to_history(history_file: str, run_data: dict):
    if not history_file or not isinstance(run_data, dict):
        return
    with _HISTORY_LOCK:
        tmp_file = f"{history_file}.{os.getpid()}.{threading.get_ident()}.tmp"
        try:
            history = load_run_history(history_file)
            history.insert(0, run_data)
            history = history[:50]
            parent = os.path.dirname(os.path.abspath(history_file))
            if parent:
                os.makedirs(parent, exist_ok=True)
            with open(tmp_file, 'w', encoding='utf-8') as f:
                json.dump(history, f, indent=2)
            os.replace(tmp_file, history_file)
        except Exception:
            pass
        finally:
            if os.path.exists(tmp_file):
                try:
                    os.remove(tmp_file)
                except OSError:
                    pass

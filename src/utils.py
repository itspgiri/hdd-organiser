import os
from rich.console import Console
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

def print_header(text: str):
    """Prints a beautiful formatted header."""
    console.print(f"\n[bold white on #4f46e5] {text} [/]\n")

def print_success(text: str):
    console.print(f"[success]✓ {text}[/]")

def print_error(text: str):
    console.print(f"[error]✗ {text}[/]")

def print_warning(text: str):
    console.print(f"[warning]! {text}[/]")

def print_info(text: str):
    console.print(f"[info]i {text}[/]")

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
    for unit in ['B', 'KB', 'MB', 'GB', 'TB']:
        if size_in_bytes < 1024.0:
            return f"{size_in_bytes:.2f} {unit}"
        size_in_bytes /= 1024.0
    return f"{size_in_bytes:.2f} PB"

import json

def load_run_history(history_file: str) -> list:
    if not os.path.exists(history_file):
        return []
    try:
        with open(history_file, 'r', encoding='utf-8') as f:
            return json.load(f)
    except Exception:
        return []

def save_run_to_history(history_file: str, run_data: dict):
    history = load_run_history(history_file)
    history.insert(0, run_data)
    history = history[:50]
    try:
        tmp_file = history_file + ".tmp"
        with open(tmp_file, 'w', encoding='utf-8') as f:
            json.dump(history, f, indent=2)
        os.replace(tmp_file, history_file)
    except Exception:
        pass



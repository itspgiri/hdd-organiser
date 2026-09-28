import os
import sys
import socket
import shutil
import secrets
import threading
import subprocess
from flask import Flask, render_template, request, jsonify
from .api_organizer import OrganizerAPI
from .scanner import SKIP_SYSTEM_DIRS, GARBAGE_FILES
from .utils import format_size, load_run_history, get_default_history_file

# The server binds a local port, so without this any web page or local process
# could drive the organizer (moving/trashing files) unauthenticated. A fresh
# token is minted per launch and handed to the UI when the page is rendered.
API_TOKEN = secrets.token_urlsafe(32)

if getattr(sys, 'frozen', False):
    template_folder = os.path.join(sys._MEIPASS, 'templates')
    static_folder = os.path.join(sys._MEIPASS, 'static')
    base_dir = sys._MEIPASS
    app = Flask(__name__, template_folder=template_folder, static_folder=static_folder)
else:
    base_dir = os.path.dirname(os.path.abspath(__file__))
    template_folder = os.path.join(base_dir, 'templates')
    static_folder = os.path.join(base_dir, 'static')
    app = Flask(__name__, template_folder=template_folder, static_folder=static_folder)


class UIState:
    status = "idle"  # idle, running, cancelling, complete, cancelled, error
    progress = 0
    total = 100
    message = "Waiting to start..."
    eta = ""
    logs = []
    preview_summary = {}


state = UIState()
state_lock = threading.Lock()
active_api_instance = None


def reset_state():
    with state_lock:
        state.status = "idle"
        state.progress = 0
        state.total = 0
        state.message = "Ready"
        state.eta = ""
        state.logs = []
        state.preview_summary = {}


def log_cb(msg: str):
    print(msg)
    with state_lock:
        state.logs.append(msg)
        if len(state.logs) > 2000:
            state.logs = state.logs[-2000:]
        state.message = msg


def progress_cb(current: int, total: int, msg: str, eta: str = ""):
    with state_lock:
        state.progress = current
        state.total = total
        state.message = msg
        state.eta = eta if eta else ""


def _sanitize_csv_cell(value):
    """Prevents CSV formula injection (CWE-1236) in Excel / Numbers / Google Sheets."""
    if not isinstance(value, str) or not value:
        return value
    stripped = value.lstrip(" ")
    if stripped and stripped[0] in ("=", "+", "-", "@", "\t", "\r"):
        return "'" + value
    return value


@app.before_request
def require_token():
    # Only /api/ is gated. The page itself must load so it can receive the token.
    if not request.path.startswith("/api/"):
        return None
    supplied = request.headers.get("X-Organizer-Token") or request.args.get("token")
    if supplied and secrets.compare_digest(supplied, API_TOKEN):
        return None
    return jsonify({"success": False, "error": "Unauthorized"}), 403


@app.route("/")
def index():
    return render_template("index.html", api_token=API_TOKEN)


@app.route("/api/select_folder", methods=["GET"])
def select_folder():
    prompt_text = request.args.get("prompt", "Select folder")
    # The prompt MUST NOT be interpolated into the script source. AppleScript
    # would evaluate any embedded `do shell script` while building the dialog's
    # argument. Passing it through argv makes it inert data.
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
    folder = result.stdout.strip()
    return jsonify({"folder": folder})


@app.route("/api/start", methods=["POST"])
def start():
    data = request.get_json(silent=True) or {}
    source = data.get("source")
    dest = data.get("dest")
    is_preview = bool(data.get("is_preview", False))
    dest_mode = data.get("dest_mode", "new")
    excluded_projects = data.get("excluded_projects", [])

    if not source or not dest:
        return jsonify({"success": False, "error": "Source and destination required"}), 400

    path_error = OrganizerAPI.validate_paths(source, dest)
    if path_error:
        return jsonify({"success": False, "error": path_error}), 400

    sources_list = OrganizerAPI._split_sources(source)
    for src_item in sources_list:
        if not os.path.exists(src_item):
            return jsonify({"success": False, "error": f"Source folder does not exist: {src_item}"}), 400
        if not os.path.isdir(src_item):
            return jsonify({"success": False, "error": f"Source path is not a folder: {src_item}"}), 400

    # "cancelling" counts as busy: the worker thread is still copying and still
    # holds the checkpoint database. Allowing a start here spawned a second
    # organizer against the same DB.
    with state_lock:
        if state.status in ("running", "cancelling"):
            return jsonify({"success": False, "error": "Already running"})
        state.status = "running"
        state.progress = 0
        state.total = 0
        state.message = "Initializing..."
        state.eta = ""
        state.logs = []
        state.preview_summary = {}

    thread = threading.Thread(target=run_organizer, args=(source, dest, is_preview, dest_mode, excluded_projects))
    thread.daemon = True
    thread.start()

    return jsonify({"success": True})


@app.route("/api/inspect_folder", methods=["GET"])
def inspect_folder():
    folder_path = request.args.get("path")
    if not folder_path or not os.path.isdir(folder_path):
        return jsonify({"files": []})
    sample_files = []
    try:
        for root, dirs, files in os.walk(folder_path):
            dirs[:] = [d for d in dirs if d not in SKIP_SYSTEM_DIRS and not d.startswith(".unzipped_")]
            for f in files[:20]:
                if f in GARBAGE_FILES or f.startswith(("._", "~$")):
                    continue
                rel_p = os.path.relpath(os.path.join(root, f), folder_path)
                sample_files.append(rel_p)
                if len(sample_files) >= 15:
                    break
            if len(sample_files) >= 15:
                break
    except Exception:
        pass
    return jsonify({"folder_path": folder_path, "name": os.path.basename(folder_path), "files": sample_files})


@app.route("/api/status", methods=["GET"])
def get_status():
    with state_lock:
        return jsonify({
            "status": state.status,
            "progress": state.progress,
            "total": state.total,
            "message": state.message,
            "eta": state.eta,
            "logs": list(state.logs),
            "preview_summary": state.preview_summary
        })


@app.route("/api/projects", methods=["GET"])
def get_projects():
    dest = request.args.get("dest")
    if not dest or not os.path.exists(dest):
        return jsonify({"projects": []})
    config_path = os.path.join(base_dir, "config.json")
    api = OrganizerAPI(config_path, log_cb, progress_cb)
    projects = api.list_code_projects(dest)
    return jsonify({"projects": projects})


@app.route("/api/dissolve_project", methods=["POST"])
def dissolve_project():
    with state_lock:
        if state.status in ("running", "cancelling"):
            return jsonify({"success": False, "error": "Cannot dissolve projects while a transfer is running."}), 409
    data = request.get_json(silent=True) or {}
    dest = data.get("dest")
    project_path = data.get("project_path")
    if not dest or not project_path:
        return jsonify({"success": False, "error": "dest and project_path required"}), 400
    config_path = os.path.join(base_dir, "config.json")
    api = OrganizerAPI(config_path, log_cb, progress_cb)
    ok, msg = api.dissolve_and_resort_project(dest, project_path)
    return jsonify({"success": ok, "message": msg, "error": None if ok else msg})


@app.route("/api/duplicates", methods=["GET"])
def get_duplicates():
    dest = request.args.get("dest")
    if not dest or not os.path.exists(dest):
        return jsonify({"duplicates": [], "trashed": []})
    config_path = os.path.join(base_dir, "config.json")
    api = OrganizerAPI(config_path, log_cb, progress_cb)
    dups = api.get_duplicate_records(dest)
    trashed = api.get_trashed_duplicates(dest)
    return jsonify({"duplicates": dups, "trashed": trashed})


@app.route("/api/trash_duplicates", methods=["POST"])
def trash_duplicates():
    with state_lock:
        if state.status in ("running", "cancelling"):
            return jsonify({"success": False, "error": "Cannot trash duplicates while a transfer is running.", "count": 0, "refused": []}), 409
    data = request.get_json(silent=True) or {}
    source_paths = data.get("source_paths", [])
    # dest is required for the safety re-check: it locates the checkpoint DB
    # that records which destination file each duplicate matched.
    dest = data.get("dest", "")
    if not source_paths or not isinstance(source_paths, list):
        return jsonify({"success": False, "error": "No duplicate source paths were provided.", "count": 0, "refused": []})
    if not dest or not os.path.exists(os.path.join(dest, ".organizer_checkpoint.db")):
        return jsonify({
            "success": False,
            "error": "Destination folder with checkpoint database is required to verify duplicates safely.",
            "count": 0,
            "refused": []
        }), 400
    config_path = os.path.join(base_dir, "config.json")
    api = OrganizerAPI(config_path, log_cb, progress_cb)
    count, refused = api.trash_duplicates(source_paths, dest_abs=dest)
    return jsonify({"success": True, "count": count, "refused": refused})


@app.route("/api/restore_duplicates", methods=["POST"])
def restore_duplicates():
    with state_lock:
        if state.status in ("running", "cancelling"):
            return jsonify({"success": False, "error": "Cannot restore duplicates while a transfer is running.", "count": 0, "refused": []}), 409
    data = request.get_json(silent=True) or {}
    dest = data.get("dest", "")
    source_paths = data.get("source_paths")
    if not dest or not os.path.exists(os.path.join(dest, ".organizer_checkpoint.db")):
        return jsonify({
            "success": False,
            "error": "Destination folder with checkpoint database is required to restore trashed duplicates.",
            "count": 0,
            "refused": []
        }), 400
    config_path = os.path.join(base_dir, "config.json")
    api = OrganizerAPI(config_path, log_cb, progress_cb)
    count, refused = api.restore_duplicates(dest, source_paths=source_paths if isinstance(source_paths, list) else None)
    return jsonify({"success": True, "count": count, "refused": refused})


@app.route("/api/list_volumes", methods=["GET"])
def list_volumes():
    volumes = []
    # 1. Check /Volumes
    if os.path.exists("/Volumes"):
        for v in os.listdir("/Volumes"):
            full_p = os.path.join("/Volumes", v)
            if os.path.isdir(full_p):
                try:
                    usage = shutil.disk_usage(full_p)
                    volumes.append({
                        "name": v,
                        "path": full_p,
                        "free": format_size(usage.free),
                        "total": format_size(usage.total),
                        "mounted": True
                    })
                # Deliberately narrow: a broad `except Exception` here used to
                # swallow a NameError (shutil was never imported) so this
                # endpoint always returned an empty list.
                except OSError:
                    pass
    # 2. Check diskutil for unmounted disks
    unmounted = []
    try:
        res = subprocess.run(["diskutil", "list"], capture_output=True, text=True, timeout=5)
        lines = res.stdout.split('\n')
        for line in lines:
            if "/dev/disk" in line or "external" in line.lower():
                unmounted.append(line.strip())
    except Exception:
        pass

    return jsonify({"volumes": volumes, "diskutil_summary": unmounted[:10]})


@app.route("/api/open_finder", methods=["GET"])
def open_finder():
    folder = request.args.get("path")
    if not folder:
        return jsonify({"success": False, "error": "Path required"}), 400
    folder_real = os.path.realpath(os.path.expanduser(folder.strip()))
    if not folder_real.startswith("/") or not os.path.isdir(folder_real):
        return jsonify({"success": False, "error": "Directory does not exist"}), 400
    subprocess.run(['open', '--', folder_real], check=False)
    return jsonify({"success": True})


@app.route("/api/export_csv", methods=["GET"])
def export_csv():
    dest = request.args.get("dest")
    if not dest:
        return "Destination path required", 400
    db_path = os.path.join(dest, ".organizer_checkpoint.db")
    if not os.path.exists(db_path):
        return "No checkpoint database found", 404
    import sqlite3
    import io
    import csv
    from datetime import datetime

    try:
        conn = sqlite3.connect(db_path, timeout=30.0)
        cursor = conn.execute("SELECT source_path, dest_path, status, size, mtime FROM copies")
        rows = cursor.fetchall()
        conn.close()
    except Exception as db_err:
        return f"Database read error: {str(db_err)}", 500

    output = io.BytesIO()
    # Write UTF-8 BOM so Excel, Apple Numbers, & Google Sheets render non-ASCII characters cleanly
    output.write(b'\xef\xbb\xbf')

    text_buffer = io.StringIO()
    writer = csv.writer(text_buffer)
    writer.writerow(["Source Path", "Destination Path", "Status", "Formatted Size", "Raw Size (Bytes)", "Modification Date"])

    for r in rows:
        src, dst, stat_val, sz, mt = r[0], r[1], r[2], r[3], r[4]
        fmt_size = format_size(sz) if sz else "0 B"
        mt_str = ""
        if mt:
            try:
                mt_str = datetime.fromtimestamp(float(mt)).strftime("%Y-%m-%d %H:%M:%S")
            except (OSError, OverflowError, ValueError, TypeError):
                mt_str = ""
        writer.writerow([
            _sanitize_csv_cell(src),
            _sanitize_csv_cell(dst),
            _sanitize_csv_cell(stat_val),
            fmt_size,
            sz,
            mt_str,
        ])

    output.write(text_buffer.getvalue().encode('utf-8'))

    from flask import Response
    return Response(
        output.getvalue(),
        mimetype="text/csv; charset=utf-8",
        headers={"Content-disposition": "attachment; filename=Drive_Organizer_Audit_Report.csv"}
    )


@app.route("/api/verify_transfer", methods=["GET"])
def verify_transfer():
    dest = request.args.get("dest")
    if not dest or not os.path.exists(dest):
        return jsonify({"success": False, "error": "Destination path required"})
    config_path = os.path.join(base_dir, "config.json")
    api = OrganizerAPI(config_path, log_cb, progress_cb)
    res = api.verify_transfer(dest)
    return jsonify(res)


@app.route("/api/repair_transfer", methods=["POST"])
def repair_transfer():
    with state_lock:
        if state.status in ("running", "cancelling"):
            return jsonify({"success": False, "error": "Cannot repair while a transfer is running."}), 409
    data = request.get_json(silent=True) or {}
    dest = data.get("dest")
    if not dest or not os.path.exists(dest):
        return jsonify({"success": False, "error": "Destination path required"})
    config_path = os.path.join(base_dir, "config.json")
    api = OrganizerAPI(config_path, log_cb, progress_cb)
    res = api.repair_transfer(dest)
    return jsonify(res)


@app.route("/api/history", methods=["GET"])
def get_history():
    history_file = get_default_history_file(base_dir)
    if not os.path.exists(history_file):
        for candidate in (
            os.path.join(base_dir, "run_history.json"),
            os.path.join(os.path.dirname(base_dir), "run_history.json"),
        ):
            if os.path.exists(candidate):
                history_file = candidate
                break
    runs = load_run_history(history_file)
    return jsonify({"history": runs})


@app.route("/api/clear_history", methods=["POST"])
def clear_history():
    candidates = {
        get_default_history_file(base_dir),
        os.path.join(base_dir, "run_history.json"),
        os.path.join(os.path.dirname(base_dir), "run_history.json"),
    }
    for h_file in candidates:
        if h_file and os.path.exists(h_file):
            try:
                os.remove(h_file)
            except Exception:
                pass
    return jsonify({"success": True})


@app.route("/api/cancel", methods=["POST"])
def cancel_operation():
    global active_api_instance
    # Deliberately NOT a terminal state.
    #
    # This used to set status="error" synchronously while the worker thread was
    # still mid-copy. The next poll saw a terminal status and re-enabled Start,
    # and /api/start only refuses when status == "running" -- so a second click
    # spawned a second organizer against the same checkpoint database
    # and overwrote active_api_instance, after which the first thread could no
    # longer be cancelled at all.
    #
    # The worker thread in run_organizer() now owns the transition to a
    # terminal state; this only requests the stop.
    with state_lock:
        if active_api_instance:
            active_api_instance.cancel()
        if state.status == "running":
            state.status = "cancelling"
        state.message = "Cancelling... finishing the file currently in flight."
    log_cb("⛔ Cancellation requested. Finishing the current file, then stopping.")
    return jsonify({"success": True})


def run_organizer(source, dest, is_preview=False, dest_mode="new", excluded_projects=None):
    global active_api_instance
    config_path = os.path.join(base_dir, "config.json")
    api = OrganizerAPI(config_path, log_cb, progress_cb)
    with state_lock:
        if state.status == "cancelling":
            api.cancel()
        active_api_instance = api
    try:
        success = api.run(source, dest, is_preview=is_preview, dest_mode=dest_mode, excluded_projects=excluded_projects)
        with state_lock:
            if hasattr(api, 'last_preview_summary'):
                state.preview_summary = api.last_preview_summary
            if success:
                state.status = "complete"
            else:
                state.status = "cancelled" if api.cancelled else "error"
    except Exception as e:
        with state_lock:
            state.status = "error"
            state.message = str(e)
        log_cb(f"❌ Error: {str(e)}")
    finally:
        # This thread is the sole owner of the terminal transition (/api/cancel
        # only sets the interim "cancelling"). If anything above failed to land
        # on a terminal status, force one -- otherwise the UI polls forever and
        # Start stays disabled with no way back.
        with state_lock:
            if state.status not in ("complete", "error", "cancelled"):
                state.status = "cancelled" if api.cancelled else "error"
            active_api_instance = None


def _find_available_port(host: str = "127.0.0.1", preferred_port: int = 5050) -> int:
    for port in range(preferred_port, preferred_port + 50):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            try:
                s.bind((host, port))
                return port
            except OSError:
                continue
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind((host, 0))
        return s.getsockname()[1]


def start_ui():
    host = "127.0.0.1"
    port = _find_available_port(host, 5050)
    url = f"http://{host}:{port}"

    def run_server():
        import logging
        log = logging.getLogger('werkzeug')
        log.setLevel(logging.ERROR)
        app.run(host=host, port=port, debug=False, use_reloader=False, threaded=True)

    server_thread = threading.Thread(target=run_server)
    server_thread.daemon = True
    server_thread.start()

    import time
    for _ in range(50):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            if s.connect_ex((host, port)) == 0:
                break
        time.sleep(0.05)

    try:
        import webview
        webview.create_window('Drive Organizer', url, width=700, height=550)
        webview.start()
    except ImportError:
        import webbrowser
        webbrowser.open(url)
        while True:
            time.sleep(1)


if __name__ == "__main__":
    start_ui()


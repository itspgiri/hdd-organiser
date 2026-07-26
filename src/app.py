import os
import sys
import threading
import subprocess
from flask import Flask, render_template, request, jsonify
from .api_organizer import OrganizerAPI

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
    status = "idle" # idle, running, complete, error
    progress = 0
    total = 100
    message = "Waiting to start..."
    eta = ""
    logs = []
    preview_summary = {}

state = UIState()

def reset_state():
    state.status = "idle"
    state.progress = 0
    state.total = 0
    state.message = "Ready"
    state.eta = ""
    state.logs = []
    state.preview_summary = {}

def log_cb(msg: str):
    print(msg)
    state.logs.append(msg)
    if len(state.logs) > 2000:
        state.logs = state.logs[-2000:]
    state.message = msg


def progress_cb(current: int, total: int, msg: str, eta: str = ""):
    state.progress = current
    state.total = total
    state.message = msg
    if eta:
        state.eta = eta

@app.route("/")
def index():
    return render_template("index.html")

@app.route("/api/select_folder", methods=["GET"])
def select_folder():
    prompt_text = request.args.get("prompt", "Select folder")
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
    folder = result.stdout.strip()
    return jsonify({"folder": folder})

@app.route("/api/start", methods=["POST"])
def start():
    if state.status == "running":
        return jsonify({"success": False, "error": "Already running"})

    data = request.json
    source = data.get("source")
    dest = data.get("dest")
    is_preview = data.get("is_preview", False)
    dest_mode = data.get("dest_mode", "new")
    excluded_projects = data.get("excluded_projects", [])
    
    if not source or not dest:
        return jsonify({"success": False, "error": "Source and destination required"})
        
    reset_state()
    state.status = "running"
    state.message = "Initializing..."
    
    thread = threading.Thread(target=run_organizer, args=(source, dest, is_preview, dest_mode, excluded_projects))
    thread.daemon = True
    thread.start()
    
    return jsonify({"success": True})

@app.route("/api/inspect_folder", methods=["GET"])
def inspect_folder():
    folder_path = request.args.get("path")
    if not folder_path or not os.path.exists(folder_path):
        return jsonify({"files": []})
    sample_files = []
    try:
        for root, dirs, files in os.walk(folder_path):
            for f in files[:20]:
                sample_files.append(os.path.join(root, f).replace(folder_path, '').lstrip('/\\'))
            if len(sample_files) >= 15:
                break
    except Exception:
        pass
    return jsonify({"folder_path": folder_path, "name": os.path.basename(folder_path), "files": sample_files})

@app.route("/api/status", methods=["GET"])
def get_status():
    return jsonify({
        "status": state.status,
        "progress": state.progress,
        "total": state.total,
        "message": state.message,
        "eta": state.eta,
        "logs": state.logs,
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
    data = request.json
    dest = data.get("dest")
    project_path = data.get("project_path")
    if not dest or not project_path:
        return jsonify({"success": False, "error": "dest and project_path required"})
    config_path = os.path.join(base_dir, "config.json")
    api = OrganizerAPI(config_path, log_cb, progress_cb)
    ok, msg = api.dissolve_and_resort_project(dest, project_path)
    return jsonify({"success": ok, "message": msg})

@app.route("/api/duplicates", methods=["GET"])
def get_duplicates():
    dest = request.args.get("dest")
    if not dest or not os.path.exists(dest):
        return jsonify({"duplicates": []})
    config_path = os.path.join(base_dir, "config.json")
    api = OrganizerAPI(config_path, log_cb, progress_cb)
    dups = api.get_duplicate_records(dest)
    return jsonify({"duplicates": dups})

@app.route("/api/trash_duplicates", methods=["POST"])
def trash_duplicates():
    data = request.json
    source_paths = data.get("source_paths", [])
    if not source_paths:
        return jsonify({"success": False, "count": 0})
    config_path = os.path.join(base_dir, "config.json")
    api = OrganizerAPI(config_path, log_cb, progress_cb)
    count = api.trash_duplicates(source_paths)
    return jsonify({"success": True, "count": count})

@app.route("/api/list_volumes", methods=["GET"])
def list_volumes():
    import subprocess
    volumes = []
    # 1. Check /Volumes
    if os.path.exists("/Volumes"):
        for v in os.listdir("/Volumes"):
            full_p = os.path.join("/Volumes", v)
            if os.path.isdir(full_p):
                try:
                    usage = shutil.disk_usage(full_p)
                    from .utils import format_size
                    volumes.append({
                        "name": v,
                        "path": full_p,
                        "free": format_size(usage.free),
                        "total": format_size(usage.total),
                        "mounted": True
                    })
                except Exception:
                    pass
    # 2. Check diskutil for unmounted disks
    unmounted = []
    try:
        res = subprocess.run(["diskutil", "list"], capture_output=True, text=True)
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
    if folder and os.path.exists(folder):
        subprocess.run(['open', folder])
        return jsonify({"success": True})
    return jsonify({"success": False})

@app.route("/api/export_csv", methods=["GET"])
def export_csv():
    dest = request.args.get("dest")
    if not dest:
        return "Destination path required", 400
    db_path = os.path.join(dest, ".organizer_checkpoint.db")
    if not os.path.exists(db_path):
        return "No checkpoint database found", 404
    import sqlite3, io, csv
    from datetime import datetime
    from .utils import format_size

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
        mt_str = datetime.fromtimestamp(mt).strftime("%Y-%m-%d %H:%M:%S") if mt else ""
        writer.writerow([src, dst, stat_val, fmt_size, sz, mt_str])

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

@app.route("/api/history", methods=["GET"])
def get_history():
    from .utils import load_run_history
    history_file = os.path.join(base_dir, "run_history.json")
    if not os.path.exists(history_file):
        parent_file = os.path.join(os.path.dirname(base_dir), "run_history.json")
        if os.path.exists(parent_file):
            history_file = parent_file
    runs = load_run_history(history_file)
    return jsonify({"history": runs})

@app.route("/api/clear_history", methods=["POST"])
def clear_history():
    for h_file in [os.path.join(base_dir, "run_history.json"), os.path.join(os.path.dirname(base_dir), "run_history.json")]:
        if os.path.exists(h_file):
            try:
                os.remove(h_file)
            except Exception:
                pass
    return jsonify({"success": True})



active_api_instance = None

@app.route("/api/cancel", methods=["POST"])
def cancel_operation():
    global active_api_instance
    if active_api_instance:
        active_api_instance.cancel()
    state.status = "error"
    state.message = "Operation cancelled by user. Progress saved."
    log_cb("⛔ Operation cancelled by user.")
    return jsonify({"success": True})

def run_organizer(source, dest, is_preview=False, dest_mode="new", excluded_projects=None):
    global active_api_instance
    config_path = os.path.join(base_dir, "config.json")
    api = OrganizerAPI(config_path, log_cb, progress_cb)
    active_api_instance = api
    success = api.run(source, dest, is_preview=is_preview, dest_mode=dest_mode, excluded_projects=excluded_projects)
    if hasattr(api, 'last_preview_summary'):
        state.preview_summary = api.last_preview_summary
    if success:
        state.status = "complete"
    else:
        state.status = "error" if not api.cancelled else "cancelled"
    active_api_instance = None


def start_ui():
    port = 5050
    host = "127.0.0.1"
    url = f"http://{host}:{port}"
    
    def run_server():
        import logging
        log = logging.getLogger('werkzeug')
        log.setLevel(logging.ERROR)
        app.run(host=host, port=port, debug=False, use_reloader=False)

    server_thread = threading.Thread(target=run_server)
    server_thread.daemon = True
    server_thread.start()

    import time
    time.sleep(1)

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

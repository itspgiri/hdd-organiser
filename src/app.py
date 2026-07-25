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
    logs = []
    preview_summary = {}

state = UIState()

def reset_state():
    state.status = "idle"
    state.progress = 0
    state.total = 100
    state.message = "Ready"
    state.logs = []
    state.preview_summary = {}

def log_cb(msg: str):
    print(msg)
    state.logs.append(msg)
    state.message = msg

def progress_cb(current: int, total: int, msg: str):
    state.progress = current
    state.total = total
    state.message = msg

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
    
    if not source or not dest:
        return jsonify({"success": False, "error": "Source and destination required"})
        
    reset_state()
    state.status = "running"
    state.message = "Initializing..."
    
    thread = threading.Thread(target=run_organizer, args=(source, dest, is_preview, dest_mode))
    thread.daemon = True
    thread.start()
    
    return jsonify({"success": True})

@app.route("/api/status", methods=["GET"])
def get_status():
    return jsonify({
        "status": state.status,
        "progress": state.progress,
        "total": state.total,
        "message": state.message,
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

def run_organizer(source, dest, is_preview=False, dest_mode="new"):
    config_path = os.path.join(base_dir, "config.json")
    api = OrganizerAPI(config_path, log_cb, progress_cb)
    success = api.run(source, dest, is_preview=is_preview, dest_mode=dest_mode)
    if hasattr(api, 'last_preview_summary'):
        state.preview_summary = api.last_preview_summary
    if success:
        state.status = "complete"
    else:
        state.status = "error"

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

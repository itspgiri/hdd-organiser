"""Shared fixtures for the pass-3 audit tests (web UI).

The browser tests drive the real app: the Flask app in `src/app.py`, served
from a thread in the test process on 127.0.0.1 at a free port, and loaded in
a Playwright browser (Chromium by default; see BROWSER_NAME) at the size of
the app's window (700 x 550). Every folder the UI works on is synthetic and
lives in a private temp directory that is deleted afterwards.

Isolation from the owner's machine:
- `src.app.base_dir` points at a temp folder holding a copy of
  `src/config.json`, and DRIVE_ORGANIZER_HISTORY_FILE at a temp file, so the
  History screen and "Clear History" never read or delete
  `src/run_history.json`.
- `subprocess` in `src.app` and `src.api_organizer` is replaced by a recorder.
  No Finder window, folder picker, `diskutil` run or sound is ever started;
  the calls are recorded so tests can check them. The folder picker returns
  the synthetic folder the test chose.

Browser tests need Playwright (`.venv/bin/pip install -r requirements-dev.txt`,
see AUDIT_REPORT.md, pass 3). Without it they are skipped.
"""

import os
import shutil
import stat
import sys
import tempfile
import threading
import time
import types
import unittest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

from src import api_organizer as api_mod  # noqa: E402
from src import app as app_mod  # noqa: E402

try:
    from playwright.sync_api import sync_playwright
    HAVE_PLAYWRIGHT = True
except ImportError:  # pragma: no cover - depends on the environment
    sync_playwright = None
    HAVE_PLAYWRIGHT = False

# pywebview renders with WebKit (WKWebView) on macOS, so WebKit is the closest
# engine. Playwright's WebKit build is ad-hoc signed and Santa blocks it on the
# owner's Mac, so the default is Chromium (Chrome for Testing, which runs).
# Set DRIVE_ORGANIZER_UI_BROWSER=webkit where WebKit is allowed.
BROWSER_NAME = os.environ.get("DRIVE_ORGANIZER_UI_BROWSER", "chromium")
WINDOW = {"width": 700, "height": 550}  # webview.create_window(..., 700, 550)


# ---------------------------------------------------------------------------
# Server-side isolation
# ---------------------------------------------------------------------------

class SubprocessRecorder:
    """Stands in for the `subprocess` module inside src.app / src.api_organizer.

    Only `df` (a read-only file-system query used to detect APFS) really
    runs. Everything else is recorded and answered with an empty success.
    """

    PIPE = -1
    DEVNULL = -3
    TimeoutExpired = __import__("subprocess").TimeoutExpired
    CalledProcessError = __import__("subprocess").CalledProcessError

    def __init__(self):
        import subprocess as real
        self._real = real
        self.calls = []
        self.picker_result = ""

    def run(self, args, *a, **kw):
        argv = list(args) if isinstance(args, (list, tuple)) else [args]
        if argv and argv[0] == "df":
            return self._real.run(args, *a, **kw)
        self.calls.append(argv)
        stdout = ""
        if argv and argv[0] == "osascript" and "choose folder" in " ".join(argv):
            stdout = self.picker_result
        text = kw.get("text") or kw.get("universal_newlines")
        return self._real.CompletedProcess(argv, 0, stdout if text else stdout.encode(), "" if text else b"")

    def Popen(self, args, *a, **kw):  # noqa: N802 - mirrors subprocess.Popen
        self.calls.append(list(args))
        return types.SimpleNamespace(pid=0, returncode=0, wait=lambda *x, **y: 0,
                                     poll=lambda: 0, kill=lambda: None,
                                     terminate=lambda: None,
                                     communicate=lambda *x, **y: ("", ""))

    def commands(self, name):
        return [c for c in self.calls if c and c[0] == name]


def reset_app_state():
    with app_mod.state_lock:
        s = app_mod.state
        s.status, s.progress, s.total = "idle", 0, 0
        s.message, s.eta, s.logs = "Ready", "", []
        s.preview_summary = {}
        s.run_summary = {}
        s.dup_status, s.dup_progress, s.dup_total = "idle", 0, 0
        s.dup_message, s.dup_results, s.dup_root = "Ready", [], ""
        app_mod.active_api_instance = None
        app_mod.active_dup_scanner_cancelled = False


class AppServer:
    """Serves src.app's Flask app on 127.0.0.1:<free port> from a thread."""

    def __init__(self, sandbox):
        self.sandbox = sandbox
        self.base = os.path.join(sandbox, "app_base")
        os.makedirs(self.base)
        shutil.copy(os.path.join(REPO_ROOT, "src", "config.json"), self.base)
        self.history_file = os.path.join(sandbox, "history", "run_history.json")
        os.makedirs(os.path.dirname(self.history_file))
        self.recorder = SubprocessRecorder()
        self._saved = None
        self._httpd = None
        self._thread = None
        self.url = None

    def start(self):
        import logging
        from werkzeug.serving import make_server
        logging.getLogger("werkzeug").setLevel(logging.ERROR)
        self._saved = (app_mod.base_dir, app_mod.subprocess, api_mod.subprocess,
                       os.environ.get("DRIVE_ORGANIZER_HISTORY_FILE"))
        app_mod.base_dir = self.base
        app_mod.subprocess = self.recorder
        api_mod.subprocess = self.recorder
        os.environ["DRIVE_ORGANIZER_HISTORY_FILE"] = self.history_file
        reset_app_state()
        self._httpd = make_server("127.0.0.1", 0, app_mod.app, threaded=True)
        self.url = f"http://127.0.0.1:{self._httpd.server_port}/"
        self._thread = threading.Thread(target=self._httpd.serve_forever, daemon=True)
        self._thread.start()
        return self

    def wait_idle(self, timeout=60.0):
        """Waits for any transfer or duplicate scan the UI started to finish."""
        deadline = time.time() + timeout
        while time.time() < deadline:
            with app_mod.state_lock:
                busy = (app_mod.state.status in ("running", "cancelling")
                        or app_mod.state.dup_status in ("running", "cancelling"))
            if not busy:
                return True
            time.sleep(0.05)
        return False

    def stop(self):
        if self._httpd is not None:
            self._httpd.shutdown()
            self._httpd.server_close()
            self._thread.join(10)
            self._httpd = None
        self.wait_idle()
        if self._saved is not None:
            base, app_sub, api_sub, hist = self._saved
            app_mod.base_dir = base
            app_mod.subprocess = app_sub
            api_mod.subprocess = api_sub
            if hist is None:
                os.environ.pop("DRIVE_ORGANIZER_HISTORY_FILE", None)
            else:
                os.environ["DRIVE_ORGANIZER_HISTORY_FILE"] = hist
            self._saved = None
        reset_app_state()


# ---------------------------------------------------------------------------
# Synthetic data
# ---------------------------------------------------------------------------

# A project folder name with an apostrophe (common on macOS: "Bob's app") and
# the other characters HTML and JavaScript care about.
TRICKY_PROJECT = "Bob's \"quoted\" <b>app</b>"


def write(path, data=b"x"):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as fh:
        fh.write(data)
    return path


def make_source_drive(root):
    """A small messy folder with everything the preview dashboard reports.

    Several categories, a duplicate, garbage files, a skipped cache folder and
    two code projects, one named TRICKY_PROJECT.
    """
    files = {
        "Camera/IMG_0001.jpg": b"jpeg-one" * 200,
        "Camera/copy/IMG_0001 copy.jpg": b"jpeg-one" * 200,
        "Bob's Photos/beach.jpg": b"beach" * 300,
        "Docs/report.pdf": b"%PDF-1.4" * 100,
        "Docs/notes.txt": b"notes" * 40,
        "Music/song.mp3": b"ID3" * 400,
        "Video/clip.mov": b"moov" * 500,
        "Projects/my-app/package.json": b"{}",
        "Projects/my-app/index.js": b"console.log(1)",
        f"Projects/{TRICKY_PROJECT}/requirements.txt": b"flask",
        f"Projects/{TRICKY_PROJECT}/main.py": b"print(1)",
        ".DS_Store": b"ds",
        "Camera/._IMG_0001.jpg": b"appledouble",
        "Docs/Thumbs.db": b"thumbs",
        "Caches/keep-me.txt": b"a real file in a folder named Caches",
    }
    for rel, data in files.items():
        write(os.path.join(root, rel), data)
    return root


def make_unreadable(path):
    """chmod 000, so copying it fails the way a damaged or locked file does."""
    os.chmod(path, 0)
    return path


def restore_writable(root):
    for dirpath, dirnames, filenames in os.walk(root):
        for name in dirnames + filenames:
            p = os.path.join(dirpath, name)
            try:
                if not os.path.islink(p):
                    os.chmod(p, stat.S_IRWXU)
            except OSError:
                pass


# ---------------------------------------------------------------------------
# Browser test case
# ---------------------------------------------------------------------------

class UITestCase(unittest.TestCase):
    """A fresh app server, sandbox and browser page for every test.

    self.js_errors  - console.error messages and uncaught exceptions
    self.http_errors - the browser's "Failed to load resource" messages
    self.dialogs    - (type, message) of every alert/confirm, in order
    confirm_answer  - what every confirm() is answered with (default: OK)
    """

    confirm_answer = True
    viewport = WINDOW

    @classmethod
    def setUpClass(cls):
        if not HAVE_PLAYWRIGHT:
            raise unittest.SkipTest(
                "Playwright is not installed: .venv/bin/pip install -r requirements-dev.txt")
        cls._pw = sync_playwright().start()
        cls.browser = getattr(cls._pw, BROWSER_NAME).launch()

    @classmethod
    def tearDownClass(cls):
        cls.browser.close()
        cls._pw.stop()

    def setUp(self):
        self.sandbox = tempfile.mkdtemp(prefix="p3-ui-")
        self.addCleanup(self._remove_sandbox)
        self.server = AppServer(self.sandbox).start()
        self.addCleanup(self.server.stop)
        self.context = self.browser.new_context(viewport=self.viewport)
        self.addCleanup(self.context.close)
        self.page = self.context.new_page()
        self.js_errors, self.js_warnings, self.http_errors = [], [], []
        self.dialogs = []
        self.page.on("console", self._on_console)
        self.page.on("pageerror", lambda exc: self.js_errors.append(f"uncaught: {exc}"))
        self.page.on("dialog", self._on_dialog)

    def _remove_sandbox(self):
        restore_writable(self.sandbox)
        shutil.rmtree(self.sandbox, ignore_errors=True)

    def _on_console(self, msg):
        if msg.type == "error":
            if msg.text.startswith("Failed to load resource"):
                self.http_errors.append(msg.text)
            else:
                self.js_errors.append(msg.text)
        elif msg.type == "warning":
            self.js_warnings.append(msg.text)

    def _on_dialog(self, dialog):
        self.dialogs.append((dialog.type, dialog.message))
        if dialog.type == "confirm" and not self.confirm_answer:
            dialog.dismiss()
        else:
            dialog.accept()

    # -- helpers ------------------------------------------------------------

    def path(self, *parts):
        return os.path.join(self.sandbox, *parts)

    def open_app(self, dark=False):
        self.page.goto(self.server.url, wait_until="load")
        self.page.wait_for_function("typeof showView === 'function'")
        if dark:
            self.page.evaluate("document.documentElement.classList.add('dark')")
        return self.page

    def click(self, selector):
        self.page.locator(selector).first.click()

    def last_dialog(self):
        return self.dialogs[-1][1] if self.dialogs else ""

    def wait_for(self, expression, timeout=30000):
        self.page.wait_for_function(expression, timeout=timeout)

    def assertNoJsErrors(self, allowed_http=()):
        """No JS errors; HTTP errors only with a status the test provoked."""
        self.assertEqual(self.js_errors, [], "JavaScript errors in the console")
        unexpected = [m for m in self.http_errors
                      if not any(f"status of {code}" in m for code in allowed_http)]
        self.assertEqual(unexpected, [], "unexpected HTTP errors in the console")

    # -- flows --------------------------------------------------------------

    def fill_setup(self, source, dest, preview=True):
        self.click("#guide-view button:has-text('Get Started')")
        self.page.fill("#source-path", source)
        self.page.dispatch_event("#source-path", "change")
        self.page.fill("#dest-path", dest)
        self.page.set_checked("#preview-mode", preview)

    def run_preview(self, source=None, dest=None, dark=False):
        """Welcome -> Setup -> Start (dry run) -> the preview dashboard."""
        source = source or make_source_drive(self.path("source"))
        dest = dest or self.path("dest", "Sorted")
        self.open_app(dark=dark)
        self.fill_setup(source, dest, preview=True)
        self.click("#start-btn")
        self.wait_for("document.getElementById('preview-view').classList.contains('active')")
        return source, dest

    def modal_state(self, modal_id):
        """Is the modal displayed, is its panel inside the window, is it on top?"""
        return self.page.evaluate("""(id) => {
            const m = document.getElementById(id);
            const panel = m.firstElementChild;
            const r = panel.getBoundingClientRect();
            const hit = document.elementFromPoint(r.left + r.width / 2, r.top + r.height / 2);
            return {
                displayed: m.getClientRects().length > 0 && getComputedStyle(m).display !== 'none',
                panel: [Math.round(r.left), Math.round(r.top), Math.round(r.right), Math.round(r.bottom)],
                window: [innerWidth, innerHeight],
                onTop: !!hit && m.contains(hit),
            };
        }""", modal_id)

    def assertModalVisible(self, modal_id):
        st = self.modal_state(modal_id)
        self.assertTrue(st["displayed"], f"#{modal_id} is not displayed: {st}")
        left, top, right, bottom = st["panel"]
        width, height = st["window"]
        self.assertTrue(left >= 0 and top >= 0 and right <= width and bottom <= height,
                        f"#{modal_id} panel is outside the window: {st}")
        self.assertTrue(st["onTop"], f"#{modal_id} is covered by something else: {st}")

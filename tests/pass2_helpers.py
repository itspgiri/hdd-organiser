"""Shared fixtures for the pass-2 audit tests (standalone duplicates utility).

Every test works on synthetic files inside a private temp directory, or on a
small disk image that the test creates, attaches at a mount point inside its
own temp directory (never under /Volumes), and detaches and deletes
afterwards. Nothing here reads or writes the owner's drives or personal
folders.
"""

import errno
import hashlib
import os
import plistlib
import shutil
import stat
import subprocess
import sys
import tempfile
import time
import unittest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

from src.api_organizer import OrganizerAPI  # noqa: E402
from src.categorizer import Categorizer  # noqa: E402

CONFIG_PATH = os.path.join(REPO_ROOT, "src", "config.json")
MB = 1024 * 1024


def payload(tag, size):
    """Deterministic, tag-specific bytes. Different tags never collide."""
    out = bytearray()
    counter = 0
    seed = tag.encode("utf-8")
    while len(out) < size:
        out += hashlib.sha256(seed + counter.to_bytes(8, "big")).digest()
        counter += 1
    return bytes(out[:size])


def write_file(path, data):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as fh:
        fh.write(data)
    return path


def read_file(path):
    with open(path, "rb") as fh:
        return fh.read()


def free_bytes(path):
    st = os.statvfs(path)
    return st.f_bavail * st.f_frsize


def make_api(logs=None):
    """An OrganizerAPI for the duplicates utility.

    On 92ed5c1 `find_duplicates_inplace` reads `self.categorizer`, which
    nothing sets (P2-01), so any scan of a folder with a subfolder raises
    AttributeError. Setting the attribute here lets the tests for the other
    findings run the baseline's real scan logic instead of stopping at P2-01.
    The P2-01 tests themselves never use this helper.
    """
    sink = logs if logs is not None else []
    api = OrganizerAPI(CONFIG_PATH, sink.append, lambda *a, **k: None)
    api.categorizer = Categorizer(CONFIG_PATH)
    return api


def restore_writable(root):
    """Undo chmod/chflags a test applied, so the sandbox can be deleted."""
    if not os.path.lexists(root):
        return
    for dirpath, dirnames, filenames in os.walk(root):
        for name in dirnames + filenames:
            p = os.path.join(dirpath, name)
            try:
                if not os.path.islink(p):
                    os.chflags(p, 0)
                    os.chmod(p, 0o700 if os.path.isdir(p) else 0o600)
            except OSError:
                pass
    try:
        os.chmod(root, 0o700)
    except OSError:
        pass


class Pass2Case(unittest.TestCase):
    """Private sandbox: self.root is the folder the utility scans."""

    def setUp(self):
        self.sandbox = tempfile.mkdtemp(prefix="p2-dups-")
        self.root = os.path.join(self.sandbox, "scan")
        os.makedirs(self.root)
        self.logs = []
        self.addCleanup(self._remove_sandbox)

    def _remove_sandbox(self):
        restore_writable(self.sandbox)
        shutil.rmtree(self.sandbox, ignore_errors=True)

    def path(self, *parts):
        return os.path.join(self.root, *parts)

    def api(self):
        return make_api(self.logs)

    def scan(self, root=None):
        return self.api().find_duplicates_inplace(root or self.root)


# ---------------------------------------------------------------------------
# Local API (Flask test client)
# ---------------------------------------------------------------------------

def app_module():
    from src import app as app_mod
    return app_mod


def reset_dup_state():
    app_mod = app_module()
    with app_mod.state_lock:
        app_mod.state.dup_status = "idle"
        app_mod.state.dup_progress = 0
        app_mod.state.dup_total = 0
        app_mod.state.dup_message = "Ready"
        app_mod.state.dup_results = []
        app_mod.state.dup_root = ""
        app_mod.active_dup_scanner_cancelled = False


def api_client():
    """Returns (client, headers) with a clean duplicates-scanner state."""
    app_mod = app_module()
    reset_dup_state()
    client = app_mod.app.test_client()
    headers = {"X-Organizer-Token": app_mod.API_TOKEN}
    return client, headers


def wait_for_dup_scan(client, headers, timeout=30.0):
    """Polls /api/dup_scan_status until the scan has stopped."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        status = client.get("/api/dup_scan_status", headers=headers).get_json()
        if status["status"] not in ("running", "cancelling"):
            return status
        time.sleep(0.02)
    raise AssertionError("duplicate scan did not finish in time")


# ---------------------------------------------------------------------------
# Real disk images (exFAT and APFS)
# ---------------------------------------------------------------------------

def _run(cmd, timeout=120):
    return subprocess.run(cmd, capture_output=True, timeout=timeout)


class DiskImage:
    """A small disk image, attached at a private mount point.

    The image file and the mount point both live in a fresh temp directory.
    `close()` detaches the image (forcing if needed) and deletes the
    directory only once the volume is no longer mounted.
    """

    def __init__(self, fs, size_mb, volname):
        self.fs = fs
        self.size_mb = size_mb
        self.volname = volname
        self.workdir = None
        self.mountpoint = None
        self.devices = []

    def open(self):
        if sys.platform != "darwin" or not shutil.which("hdiutil"):
            raise unittest.SkipTest("hdiutil is needed to create disk images")
        self.workdir = tempfile.mkdtemp(prefix="p2-img-")
        image = os.path.join(self.workdir, "volume.dmg")
        self.mountpoint = os.path.join(self.workdir, "mnt")
        os.makedirs(self.mountpoint)
        create = ["hdiutil", "create", "-size", f"{self.size_mb}m",
                  "-fs", self.fs, "-volname", self.volname]
        if self.fs == "ExFAT":
            # What an external drive formatted exFAT on a Mac usually has.
            create += ["-layout", "MBRSPUD"]
        res = _run(create + [image])
        if res.returncode != 0:
            self.close()
            raise unittest.SkipTest(
                f"hdiutil create failed: {res.stderr.decode(errors='replace')}")
        res = _run(["hdiutil", "attach", "-nobrowse", "-noautoopen",
                    "-mountpoint", self.mountpoint, "-plist", image])
        if res.returncode != 0:
            self.close()
            raise unittest.SkipTest(
                f"hdiutil attach failed: {res.stderr.decode(errors='replace')}")
        info = plistlib.loads(res.stdout)
        self.devices = [e["dev-entry"] for e in info.get("system-entities", [])
                        if "dev-entry" in e]
        if not os.path.ismount(self.mountpoint):
            self.close()
            raise unittest.SkipTest("disk image did not mount")
        return self

    def close(self):
        if self.mountpoint and os.path.ismount(self.mountpoint):
            # Detach the whole image (the first entity is the image's disk).
            target = self.devices[0] if self.devices else self.mountpoint
            for extra in ([], ["-force"], ["-force"]):
                _run(["hdiutil", "detach", target] + extra)
                if not os.path.ismount(self.mountpoint):
                    break
                time.sleep(1)
        if self.mountpoint and os.path.ismount(self.mountpoint):
            raise RuntimeError(f"could not detach test image at {self.mountpoint}")
        if self.workdir:
            shutil.rmtree(self.workdir, ignore_errors=True)
            self.workdir = None

    def __enter__(self):
        return self.open()

    def __exit__(self, *exc):
        self.close()
        return False


def _create_and_write(path, size):
    """Creates path with `size` bytes. Returns False once the volume is full."""
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644)
    except OSError as e:
        if e.errno in (errno.ENOSPC, errno.EINVAL):
            return False
        raise
    ok = True
    try:
        os.write(fd, b"\xcd" * size)
        os.fsync(fd)
    except OSError as e:
        # exFAT (FSKit) sometimes reports EINVAL instead of ENOSPC here.
        if e.errno not in (errno.ENOSPC, errno.EINVAL):
            raise
        ok = False
    finally:
        try:
            os.close(fd)
        except OSError:
            ok = False
    return ok


def fill_volume(mountpoint):
    """Fills the volume until not even a 1-byte file can be written.

    Returns the filler folder. Raises AssertionError if the volume is not
    completely full afterwards.
    """
    assert os.path.ismount(mountpoint), mountpoint
    filler = os.path.join(mountpoint, "filler")
    os.makedirs(filler, exist_ok=True)
    prefix = f"{time.time_ns():x}"
    n = 0
    for size in (4 * MB, MB, 65536, 4096, 512, 1):
        while True:
            n += 1
            if not _create_and_write(os.path.join(filler, f"{prefix}-{n:06d}"), size):
                break
    probe = os.path.join(filler, f"{prefix}-probe")
    wrote = _create_and_write(probe, 1)
    if wrote:
        raise AssertionError("volume still accepts writes after filling")
    return filler


def volume_is_full(mountpoint):
    """True if a new 1-byte file cannot be written (leaves nothing behind)."""
    probe = os.path.join(mountpoint, f".p2-probe-{os.getpid()}-{time.time_ns()}")
    try:
        return not _create_and_write(probe, 1)
    finally:
        try:
            if os.path.lexists(probe):
                os.unlink(probe)
        except OSError:
            pass


def tree_snapshot(root, skip=(".fseventsd", ".Spotlight-V100", ".Trashes")):
    """Everything a write could change: names, types, sizes, mtimes, contents."""
    snap = {}
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = sorted(d for d in dirnames
                             if not (dirpath == root and d in skip))
        for name in dirnames + sorted(filenames):
            p = os.path.join(dirpath, name)
            st = os.lstat(p)
            entry = (stat.S_IFMT(st.st_mode), st.st_size, st.st_mtime_ns)
            if stat.S_ISREG(st.st_mode):
                h = hashlib.sha256()
                with open(p, "rb") as fh:
                    for chunk in iter(lambda: fh.read(MB), b""):
                        h.update(chunk)
                entry += (h.hexdigest(),)
            snap[os.path.relpath(p, root)] = entry
    return snap

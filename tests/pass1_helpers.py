"""Shared fixtures for the pass-1 audit tests (organize-flow data safety).

Every test works on synthetic files inside a private temp directory that is
deleted afterwards. Nothing here reads or writes real drives or personal
folders: run history is redirected into the temp directory, and the
`caffeinate` helper is never started (a simulated crash would otherwise leave
it running and keep the Mac awake).
"""

import hashlib
import os
import shutil
import sqlite3
import stat
import subprocess
import sys
import tempfile
import textwrap
import unittest
from unittest import mock

import xattr

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

from src.api_organizer import OrganizerAPI  # noqa: E402
from src.file_ops import FileEngine  # noqa: E402

CONFIG_PATH = os.path.join(REPO_ROOT, "src", "config.json")

# What the organizer itself writes at the top of a destination.
BOOKKEEPING_FILES = {
    ".organizer_checkpoint.db",
    ".organizer_checkpoint.db-wal",
    ".organizer_checkpoint.db-shm",
    ".metadata_never_index",
}
BOOKKEEPING_DIRS = {".organizer_staging"}

# 2020-01-01 12:00 UTC. Source files get an old mtime, like a real archive,
# so "the copy was edited later" is distinguishable from "the copy is fresh".
OLD_MTIME = 1577880000


def write_file(path, data, mtime=OLD_MTIME):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as fh:
        fh.write(data)
    if mtime is not None:
        os.utime(path, (mtime, mtime))
    return path


def read_file(path):
    with open(path, "rb") as fh:
        return fh.read()


def unique_payload(tag, size):
    """Deterministic bytes that differ for every tag (no shared prefixes)."""
    out = bytearray()
    counter = 0
    while len(out) < size:
        out += hashlib.sha256(f"{tag}:{counter}".encode()).digest()
        counter += 1
    return bytes(out[:size])


def sha256_file(path):
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _entry(path):
    st = os.lstat(path)
    info = {
        "mode": st.st_mode,
        "mtime_ns": st.st_mtime_ns,
        "flags": getattr(st, "st_flags", 0),
    }
    if stat.S_ISREG(st.st_mode):
        info["size"] = st.st_size
        info["sha256"] = sha256_file(path)
    elif stat.S_ISLNK(st.st_mode):
        info["link"] = os.readlink(path)
    try:
        names = sorted(xattr.listxattr(path, symlink=True))
        info["xattrs"] = [(n, xattr.getxattr(path, n, symlink=True)) for n in names]
    except OSError:
        info["xattrs"] = None
    return info


def snapshot_tree(root):
    """Everything a write could change under root: entries, types, sizes,
    content hashes, mtimes (directory mtimes catch files that were created
    and removed again), permission bits, flags, and extended attributes.
    Returns None if root does not exist."""
    if not os.path.lexists(root):
        return None
    snap = {".": _entry(root)}
    for dirpath, dirnames, filenames in os.walk(root):
        for name in dirnames + filenames:
            full = os.path.join(dirpath, name)
            snap[os.path.relpath(full, root)] = _entry(full)
    return snap


def dest_data_files(dest):
    """Relative paths of every file in a destination except the organizer's
    own bookkeeping (checkpoint DB, Spotlight marker, archive staging)."""
    out = []
    for dirpath, dirnames, filenames in os.walk(dest):
        dirnames[:] = [d for d in dirnames if d not in BOOKKEEPING_DIRS]
        for name in filenames:
            if dirpath == dest and name in BOOKKEEPING_FILES:
                continue
            out.append(os.path.relpath(os.path.join(dirpath, name), dest))
    return sorted(out)


# Runs a real organize in a child process and kills it, os._exit-style, while
# the (crash_after + 1)-th file is half written. No finally blocks, atexit
# handlers or database commits run: the closest a test can get to a power cut
# or `kill -9`.
_CRASH_DRIVER = textwrap.dedent(
    """
    import os, sys
    repo, config, src, dest, crash_after, mode = sys.argv[1:7]
    sys.path.insert(0, repo)
    from src import file_ops
    from src.api_organizer import OrganizerAPI

    # Never leave an orphaned `caffeinate -dims` behind after os._exit.
    file_ops.FileEngine.start_caffeinate = lambda self: None

    real_native_copy = file_ops.native_copy
    calls = [0]

    def crashing_native_copy(source_path, target_path):
        calls[0] += 1
        if calls[0] > int(crash_after):
            with open(source_path, "rb") as fh:
                data = fh.read()
            with open(target_path, "wb") as fh:
                fh.write(data[: max(1, len(data) // 2)])
            os._exit(17)
        return real_native_copy(source_path, target_path)

    file_ops.native_copy = crashing_native_copy
    OrganizerAPI(config, lambda m: None, lambda *a, **k: None).run(
        src, dest, is_preview=False, dest_mode=mode
    )
    os._exit(0)
    """
)

CRASH_EXIT_CODE = 17


class Pass1Case(unittest.TestCase):
    """Private sandbox per test: self.src, self.dest under self.tmp."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="organizer_pass1_")
        self.addCleanup(self._remove_sandbox)
        self.src = os.path.join(self.tmp, "source")
        self.dest = os.path.join(self.tmp, "dest")
        os.makedirs(self.src)

        env = mock.patch.dict(
            os.environ,
            {"DRIVE_ORGANIZER_HISTORY_FILE": os.path.join(self.tmp, "run_history.json")},
        )
        env.start()
        self.addCleanup(env.stop)

        caffeinate = mock.patch.object(FileEngine, "start_caffeinate", lambda _self: None)
        caffeinate.start()
        self.addCleanup(caffeinate.stop)

        self.logs = []

    def _remove_sandbox(self):
        # Undo locked / read-only bits a test may have set, then delete.
        for dirpath, dirnames, filenames in os.walk(self.tmp):
            for name in dirnames + filenames:
                p = os.path.join(dirpath, name)
                try:
                    os.chflags(p, 0, follow_symlinks=False)
                except (OSError, AttributeError, NotImplementedError):
                    pass
                if not os.path.islink(p):
                    try:
                        os.chmod(p, 0o700)
                    except OSError:
                        pass
        shutil.rmtree(self.tmp, ignore_errors=True)

    # -- organizer helpers -------------------------------------------------

    def make_api(self):
        return OrganizerAPI(CONFIG_PATH, self.logs.append, lambda *a, **k: None)

    def organize(self, src=None, dest=None, mode="new", expect=True):
        ok = self.make_api().run(src or self.src, dest or self.dest,
                                 is_preview=False, dest_mode=mode)
        if expect is not None:
            self.assertEqual(ok, expect, "unexpected organize result. Last log lines:\n"
                             + "\n".join(self.logs[-25:]))
        return ok

    def db_rows(self, dest=None):
        conn = sqlite3.connect(os.path.join(dest or self.dest, ".organizer_checkpoint.db"))
        try:
            return conn.execute(
                "SELECT source_path, dest_path, status, size, mtime FROM copies"
            ).fetchall()
        finally:
            conn.close()

    def dest_of(self, source_file, dest=None):
        """Where the checkpoint DB says source_file was copied to."""
        want = os.path.realpath(source_file)
        for source_path, dest_path, _status, _size, _mtime in self.db_rows(dest):
            if os.path.realpath(source_path) == want:
                return dest_path
        self.fail(f"no checkpoint record for {source_file}")

    def run_until_crash(self, crash_after, src=None, dest=None, mode="new"):
        proc = subprocess.run(
            [sys.executable, "-c", _CRASH_DRIVER, REPO_ROOT, CONFIG_PATH,
             src or self.src, dest or self.dest, str(crash_after), mode],
            env=dict(os.environ),
            capture_output=True,
            text=True,
            timeout=120,
        )
        self.assertEqual(proc.returncode, CRASH_EXIT_CODE,
                         f"the simulated crash did not happen:\n{proc.stdout}\n{proc.stderr}")

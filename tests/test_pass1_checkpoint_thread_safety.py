"""Pass-1 audit, finding P1-07: parallel transfer workers crashed the app.

FileEngine shares one SQLite connection between the four transfer worker
threads and guards it with `self.lock`. macOS's system SQLite is built with
THREADSAFE=2, so a connection must never be used by two threads at the same
time. Several lookups left their cursor alive after `with self.lock:` ended.
When Python freed that cursor, it called sqlite3_reset() on the statement it
still held, outside the lock and at the same time as another worker's query.
Resumed transfers (where most lookups find a row) crashed with a segmentation
fault, which also made `make test` crash now and then.

Run with:  .venv/bin/python -m unittest tests.test_pass1_checkpoint_thread_safety -v
"""

import gc
import os
import sqlite3
import subprocess
import sys
import textwrap

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from pass1_helpers import REPO_ROOT, Pass1Case, write_file  # noqa: E402
from src.file_ops import FileEngine  # noqa: E402

# Four threads doing checkpoint lookups against rows that exist, like the
# workers of a resumed transfer. Before the fix this segfaulted in well under
# a second on every attempt.
_HAMMER = textwrap.dedent(
    """
    import os, sys, threading, time
    repo, root, seconds = sys.argv[1], sys.argv[2], float(sys.argv[3])
    sys.path.insert(0, repo)
    from src import file_ops
    file_ops.FileEngine.start_caffeinate = lambda self: None

    src_dir = os.path.join(root, "src")
    dest = os.path.join(root, "dest")
    os.makedirs(src_dir)
    os.makedirs(dest)
    paths = []
    for i in range(100):
        p = os.path.join(src_dir, "f%03d.bin" % i)
        with open(p, "wb") as fh:
            fh.write(b"x" * (64 + i))
        paths.append(p)

    engine = file_ops.FileEngine(dest)
    for p in paths:
        engine.record_copy(p, p, os.path.getsize(p), os.path.getmtime(p), "h")
    engine.record_project(src_dir, src_dir)

    deadline = time.time() + seconds

    def lookups(k):
        i = k
        while time.time() < deadline:
            engine.is_already_copied(paths[i % len(paths)])
            i += 7

    def project_lookups():
        while time.time() < deadline:
            engine.get_project_copy(src_dir)
            engine.is_project_dissolved(src_dir)
            engine.has_completed_owner(paths[0])

    threads = [threading.Thread(target=lookups, args=(k,)) for k in range(3)]
    threads.append(threading.Thread(target=project_lookups))
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    engine.close()
    print("ok")
    """
)


class _CursorAudit:
    lock = None
    freed_outside_lock = []


class _AuditedCursor(sqlite3.Cursor):
    """Records a cursor that is freed without being closed while the engine's
    lock is not held: exactly when sqlite3.Cursor's own deallocation would
    reset a live statement outside the lock."""

    def execute(self, sql, *args):
        self._audit_sql = " ".join(sql.split())
        return super().execute(sql, *args)

    def close(self):
        self._audit_closed = True
        super().close()

    def __del__(self):
        if not getattr(self, "_audit_closed", False) and not _CursorAudit.lock.locked():
            _CursorAudit.freed_outside_lock.append(getattr(self, "_audit_sql", "?"))


class _AuditedConnection(sqlite3.Connection):
    def cursor(self, factory=_AuditedCursor):
        return super().cursor(factory)


class CheckpointThreadSafetyTests(Pass1Case):

    def test_parallel_checkpoint_lookups_do_not_crash(self):
        proc = subprocess.run(
            [sys.executable, "-X", "faulthandler", "-c", _HAMMER, REPO_ROOT,
             os.path.join(self.tmp, "hammer"), "2.0"],
            capture_output=True, text=True, timeout=120,
        )
        self.assertEqual(proc.returncode, 0,
                         "parallel checkpoint lookups crashed the process "
                         f"(exit code {proc.returncode}):\n{proc.stderr[-3000:]}")
        self.assertIn("ok", proc.stdout)

    def test_every_cursor_is_closed_before_the_lock_is_released(self):
        os.makedirs(self.dest)
        src_file = write_file(os.path.join(self.src, "a.txt"), b"same size 1")
        twin_src = write_file(os.path.join(self.src, "b.txt"), b"same size 2")
        dest_file = write_file(os.path.join(self.dest, "a.txt"), b"same size 1")
        project = os.path.join(self.src, "proj")
        os.makedirs(project)

        engine = FileEngine(self.dest)
        self.addCleanup(engine.close)
        engine.conn.close()
        engine.conn = sqlite3.connect(os.path.join(self.dest, ".organizer_checkpoint.db"),
                                      factory=_AuditedConnection, check_same_thread=False)
        _CursorAudit.lock = engine.lock
        _CursorAudit.freed_outside_lock = []

        engine.record_copy(src_file, dest_file, os.path.getsize(src_file),
                           os.path.getmtime(src_file))
        engine.record_project(project, os.path.join(self.dest, "Code", "proj"))

        # Every read the transfer workers do, each finding a matching row.
        engine.is_already_copied(src_file)
        engine.has_completed_owner(dest_file)
        engine.is_content_duplicate(twin_src, os.path.getsize(twin_src))
        engine.get_project_copy(project)
        engine.is_project_dissolved(project)
        engine.mark_project_dissolved(project)
        gc.collect()

        self.assertEqual(_CursorAudit.freed_outside_lock, [],
                         "these queries left a live statement to be reset outside self.lock")

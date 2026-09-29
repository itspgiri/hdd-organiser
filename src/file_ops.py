import os
import atexit
import plistlib
import shutil
import sqlite3
import hashlib
import stat
import subprocess
import threading
import time
import unicodedata
from typing import Optional, Tuple, Dict
import xattr  # Used for macOS Finder tags
from .categorizer import split_filename_ext

try:
    import ctypes
    _libsystem = ctypes.CDLL('/usr/lib/libSystem.dylib')
    _libsystem.copyfile.argtypes = [ctypes.c_char_p, ctypes.c_char_p, ctypes.c_void_p, ctypes.c_uint32]
    _libsystem.copyfile.restype = ctypes.c_int
    # Values are from /usr/include/copyfile.h. COPYFILE_ALL is
    # COPYFILE_METADATA|COPYFILE_DATA == (ACL|STAT|XATTR|DATA) == 15.
    #
    # This used to be 32767 (0x7FFF), which also set bits 4-14 -- eleven bits
    # Apple has never defined. The kernel happens to ignore them today, but
    # passing undefined flags to the syscall that moves every byte the user
    # owns is not something to rely on, and the neighbouring bits in that
    # register are COPYFILE_MOVE (unlink source) and COPYFILE_UNLINK.
    _COPYFILE_ACL = 1 << 0
    _COPYFILE_STAT = 1 << 1
    _COPYFILE_XATTR = 1 << 2
    _COPYFILE_DATA = 1 << 3
    _COPYFILE_ALL = _COPYFILE_ACL | _COPYFILE_STAT | _COPYFILE_XATTR | _COPYFILE_DATA
    _COPYFILE_CLONE = 1 << 24
    _HAS_NATIVE_COPYFILE = True
except Exception:
    _HAS_NATIVE_COPYFILE = False


def _is_regular_file(path: str) -> bool:
    """Returns True only if path resolves to a regular file (not a FIFO, socket, or device)."""
    try:
        return stat.S_ISREG(os.stat(path).st_mode)
    except OSError:
        return False


def _clear_immutable(path: str):
    """Clears macOS UF_IMMUTABLE (Finder Locked) flag on path if present so it can be renamed/removed."""
    try:
        st = os.lstat(path)
        flags = getattr(st, "st_flags", 0)
        uf_immutable = getattr(stat, "UF_IMMUTABLE", 0x00000002)
        if flags & uf_immutable:
            os.chflags(path, flags & ~uf_immutable)
    except (OSError, AttributeError):
        pass


def _force_remove(path: str) -> bool:
    """Removes a file or symlink even if it is Finder-locked (UF_IMMUTABLE) or read-only (0o444)."""
    try:
        if not os.path.lexists(path):
            return False
        _clear_immutable(path)
        if not os.path.islink(path):
            try:
                os.chmod(path, stat.S_IRUSR | stat.S_IWUSR)
            except OSError:
                pass
        os.unlink(path)
        return True
    except OSError:
        return False


def _truncate_filename_utf8(stem: str, suffix: str, max_bytes: int = 251) -> str:
    """Ensures `stem + suffix` does not exceed max_bytes in UTF-8 (leaving 4 bytes for `.tmp` under 255-byte NAME_MAX)."""
    suffix_bytes = len(suffix.encode("utf-8"))
    allowed_stem_bytes = max(1, max_bytes - suffix_bytes)
    encoded_stem = stem.encode("utf-8")
    if len(encoded_stem) <= allowed_stem_bytes:
        return stem + suffix
    truncated_stem = encoded_stem[:allowed_stem_bytes].decode("utf-8", errors="ignore")
    if not truncated_stem:
        truncated_stem = "file"
    return truncated_stem + suffix


def restore_timestamps(source_path: str, target_path: str):
    """Best-effort copy of creation/access/modification times from source to target.

    exFAT / FAT32 volumes reject extended attributes and ACLs (raising Errno 22),
    which is why the data-only shutil.copyfile is used as the transfer fallback.
    Those same volumes *do* accept utime, so timestamps are restored separately
    here instead of being silently reset to the time of the copy.

    On macOS/Darwin, calling utime with a timestamp earlier than the current
    inode birthtime pulls st_birthtime back to that timestamp; a second utime
    call then restores the true (atime, mtime) with nanosecond precision while
    preserving the historical creation date.
    """
    try:
        st = os.stat(source_path)
        btime = getattr(st, "st_birthtime", None)
        if btime is not None and btime < st.st_mtime:
            try:
                os.utime(target_path, (btime, btime))
            except (OSError, ValueError):
                pass
        os.utime(target_path, ns=(st.st_atime_ns, st.st_mtime_ns))
    except (OSError, ValueError):
        try:
            st = os.stat(source_path)
            os.utime(target_path, (st.st_atime, st.st_mtime))
        except (OSError, ValueError):
            pass


def restore_mode(source_path: str, target_path: str):
    """Best-effort copy of the permission bits from source to target.

    Matters most for code projects: a repository whose build.sh, configure or
    git hooks arrive without the executable bit is not "preserved intact".
    """
    try:
        shutil.copymode(source_path, target_path)
    except OSError:
        pass


def restore_xattrs(source_path: str, target_path: str):
    """Best-effort copy of extended attributes (Finder tags, comments, etc.).

    shutil.copy2 only carries xattrs on Linux; on macOS it silently drops them,
    so they are re-applied here. If target_path was already chmod'ed read-only
    (e.g. 0o444), temporarily grant owner-write so setxattr does not fail with EACCES.
    """
    try:
        keys = xattr.listxattr(source_path)
        if not keys:
            return
        st_mode = None
        try:
            st_mode = stat.S_IMODE(os.stat(target_path).st_mode)
            if not (st_mode & stat.S_IWUSR):
                os.chmod(target_path, st_mode | stat.S_IWUSR)
        except OSError:
            st_mode = None
        try:
            for key in keys:
                try:
                    xattr.setxattr(target_path, key, xattr.getxattr(source_path, key))
                except (OSError, IOError):
                    pass
        finally:
            if st_mode is not None and not (st_mode & stat.S_IWUSR):
                try:
                    os.chmod(target_path, st_mode)
                except OSError:
                    pass
    except (OSError, IOError):
        pass


def native_copy(source_path: str, target_path: str) -> bool:
    """Copy via the macOS copyfile syscall, preserving all metadata.

    Returns True on success. On APFS this is a copy-on-write clone: instant
    and consuming no additional space.
    """
    if not _HAS_NATIVE_COPYFILE:
        return False
    # Never pass a symlink or special file (FIFO/socket) to copyfile(CLONE):
    # CLONE implies NOFOLLOW_SRC and creates a symlink at target_path, which a
    # retry or fallback would then open and write through to the symlink target.
    if os.path.islink(source_path) or not _is_regular_file(source_path):
        return False
    try:
        src_size = os.path.getsize(source_path)
        # COPYFILE_CLONE implies COPYFILE_EXCL, so an existing target makes the
        # syscall fail. Retry without CLONE rather than giving up on metadata.
        for flags in (_COPYFILE_ALL | _COPYFILE_CLONE, _COPYFILE_ALL):
            if os.path.lexists(target_path):
                _force_remove(target_path)
            ret = _libsystem.copyfile(
                source_path.encode('utf-8'), target_path.encode('utf-8'), None, flags
            )
            if ret == 0 and os.path.exists(target_path) \
                    and not os.path.islink(target_path) \
                    and os.path.getsize(target_path) == src_size:
                # Strip UF_IMMUTABLE on target_path so copy_file can os.replace(tmp_path, target)
                _clear_immutable(target_path)
                return True
        if os.path.lexists(target_path):
            _force_remove(target_path)
    except Exception:
        if os.path.lexists(target_path):
            _force_remove(target_path)
    return False


def safe_copy(source_path: str, target_path: str):
    """exFAT-safe copy that preserves as much metadata as the volume allows.

    Drop-in replacement for shutil.copyfile / shutil.copy2 usable as a
    shutil.copytree copy_function.

    Order of preference:
      1. the native copyfile syscall  - data + permissions + timestamps + xattrs
      2. shutil.copy2 + xattr restore - portable equivalent
      3. shutil.copyfile + manual restore of times and mode - exFAT/FAT32,
         which reject xattrs and ACLs with Errno 22
    """
    if os.path.islink(source_path):
        _force_remove(target_path)
        try:
            os.symlink(os.readlink(source_path), target_path)
            return target_path
        except OSError:
            # On exFAT/FAT32 (which reject symlinks), fall back to copying the
            # target regular file if the symlink resolves to one.
            try:
                st = os.stat(source_path)
                if not stat.S_ISREG(st.st_mode):
                    return target_path
                shutil.copyfile(source_path, target_path)
                restore_timestamps(source_path, target_path)
                restore_mode(source_path, target_path)
            except OSError:
                pass
            return target_path

    if not _is_regular_file(source_path):
        # Skip FIFOs, sockets, and device nodes inside code projects so copytree does not fail.
        return target_path

    if os.path.lexists(target_path):
        _force_remove(target_path)

    if native_copy(source_path, target_path):
        return target_path
    try:
        shutil.copy2(source_path, target_path)
        _clear_immutable(target_path)
        restore_xattrs(source_path, target_path)
        restore_timestamps(source_path, target_path)
    except OSError:
        src_size = -1
        try:
            src_size = os.path.getsize(source_path)
        except OSError:
            pass
        # If copy2 failed only at the final chflags/copystat step after writing all bytes,
        # avoid rewriting multi-GB files a second time; otherwise clean up and copy bytes.
        if not (os.path.exists(target_path) and not os.path.islink(target_path) and src_size >= 0 and os.path.getsize(target_path) == src_size):
            _force_remove(target_path)
            shutil.copyfile(source_path, target_path)
        _clear_immutable(target_path)
        restore_xattrs(source_path, target_path)
        restore_timestamps(source_path, target_path)
        restore_mode(source_path, target_path)
    return target_path


def files_are_identical(path_a: str, path_b: str) -> bool:
    """Byte-for-byte comparison of two files without using filecmp's global stat cache.

    The part-hash below only samples the first and last 1MB of a file, so it
    cannot prove two files are the same. Any two files that share a size and
    those two sampled megabytes collide - re-encoded videos, disk images,
    padded archives and same-camera clips all do this in practice. Because a
    positive duplicate verdict means the file is *never copied* (and is then
    offered up for trashing), the sampled hash is only ever used to shortlist
    candidates; this function makes the final call by reading the actual bytes.
    """
    try:
        if not (_is_regular_file(path_a) and _is_regular_file(path_b)):
            return False
        size_a = os.path.getsize(path_a)
        size_b = os.path.getsize(path_b)
        if size_a != size_b:
            return False
        if size_a == 0:
            return True
        bufsize = 65536
        with open(path_a, 'rb') as fa, open(path_b, 'rb') as fb:
            while True:
                ba = fa.read(bufsize)
                bb = fb.read(bufsize)
                if ba != bb:
                    return False
                if not ba:
                    return True
    except OSError:
        return False


def get_part_hash(filepath: str, size: int = 0) -> str:
    """Fast hash: reads first and last 1MB of large files to prevent HDD thrashing.
    Stateless and 100% read-only (zero disk writes), safe on completely full drives.
    """
    if not _is_regular_file(filepath):
        return ""
    h = hashlib.sha256()
    chunk_size = 1024 * 1024  # 1MB
    try:
        with open(filepath, 'rb') as f:
            actual_size = os.fstat(f.fileno()).st_size
            if actual_size <= chunk_size * 2:
                h.update(f.read(chunk_size * 2))
            else:
                h.update(f.read(chunk_size))
                f.seek(-chunk_size, os.SEEK_END)
                h.update(f.read(chunk_size))
    except Exception:
        return ""
    return h.hexdigest()


# Regenerable build output and dependency caches - not worth the transfer time
# and usually far larger than the project itself.
#
# NOTE: .git is deliberately NOT in this list. It used to be, which meant a
# repository advertised as "preserved 100% intact" arrived with its entire
# version history stripped out - normally the most valuable part of an
# archived project.
PROJECT_IGNORE_PATTERNS = (
    "node_modules", ".next", ".firebase", ".venv", "venv", "__pycache__",
    ".turbo", "dist", "build", ".cache", "target", ".gradle", ".cargo",
    ".DS_Store", ".Duplicates_Trash", ".organizer_staging",
)


def copy_project_intact(source_dir: str, dest_dir: str) -> Tuple[bool, str]:
    """Copies a detected code project, preserving its structure, symlinks, and history.

    Tries the timestamp-preserving exFAT-safe copy first and falls back if needed.

    Returns (ok, error_message).
    """
    ignore = shutil.ignore_patterns(*PROJECT_IGNORE_PATTERNS)
    try:
        shutil.copytree(
            source_dir, dest_dir,
            symlinks=True,
            dirs_exist_ok=True,
            copy_function=safe_copy,
            ignore_dangling_symlinks=True,
            ignore=ignore
        )
        return True, ""
    except shutil.Error as err:
        # On exFAT/FAT32, directory copystat(chflags) can raise Errno 22 (EINVAL)
        # at the very end of copytree after all files have been copied intact.
        if project_already_copied(source_dir, dest_dir):
            return True, ""
        return False, str(err)
    except Exception as err:
        return False, str(err)


def project_already_copied(source_dir: str, dest_dir: str) -> bool:
    """True if dest_dir already looks like a faithful copy of source_dir.

    Used as a fallback when the checkpoint database has no record of the
    project (it was deleted, or the destination was organized by an older
    version). Without this, re-running the same job clones every repository
    again as project_1, project_2, ...

    Compares the relative path set (in both directions), file sizes, and
    samples non-.git file content so two unrelated projects that happen to share
    filenames and byte sizes (e.g. a 10-byte main.py) are not collapsed together.
    """
    if not os.path.isdir(dest_dir):
        return False

    ignored = set(PROJECT_IGNORE_PATTERNS)
    src_rel_files = set()
    bytes_verified = 0
    max_verify_bytes = 4 * 1024 * 1024  # Cap content verification at 4MB for speed

    for root, dirs, files in os.walk(source_dir):
        dirs[:] = [
            d for d in dirs
            if d not in ignored and not os.path.islink(os.path.join(root, d))
        ]
        for name in files:
            if name in ignored:
                continue
            src_file = os.path.join(root, name)
            if os.path.islink(src_file):
                if not os.path.exists(src_file):
                    # Dangling symlink skipped by ignore_dangling_symlinks=True
                    continue
                rel = os.path.relpath(src_file, source_dir)
                dst_file = os.path.join(dest_dir, rel)
                if not os.path.lexists(dst_file):
                    return False
                src_rel_files.add(rel)
                continue
            if not _is_regular_file(src_file):
                continue
            rel = os.path.relpath(src_file, source_dir)
            src_rel_files.add(rel)
            dst_file = os.path.join(dest_dir, rel)
            try:
                src_sz = os.path.getsize(src_file)
                if src_sz != os.path.getsize(dst_file):
                    return False
                if ".git" not in rel.split(os.sep) and bytes_verified < max_verify_bytes and src_sz <= 1024 * 1024:
                    if not files_are_identical(src_file, dst_file):
                        return False
                    bytes_verified += src_sz
            except OSError:
                return False

    dst_rel_files = set()
    for root, dirs, files in os.walk(dest_dir):
        dirs[:] = [
            d for d in dirs
            if d not in ignored and not os.path.islink(os.path.join(root, d))
        ]
        for name in files:
            if name in ignored:
                continue
            dst_file = os.path.join(root, name)
            if not os.path.islink(dst_file) and not _is_regular_file(dst_file):
                continue
            dst_rel_files.add(os.path.relpath(dst_file, dest_dir))

    return src_rel_files == dst_rel_files


class FileEngine:
    def __init__(self, dest_root: str):
        self.dest_root = dest_root
        os.makedirs(dest_root, exist_ok=True)
        self.db_path = os.path.join(dest_root, ".organizer_checkpoint.db")
        self.lock = threading.Lock()
        self.dirs_lock = threading.Lock()
        self.reserved_lock = threading.Lock()
        self.created_dirs = set()
        self.reserved_paths = set()
        self.reserved_path_sizes: Dict[str, int] = {}
        self.in_flight_sizes: Dict[int, int] = {}
        self._uncommitted = 0
        self._init_db()
        self.caffeinate_process = None

    @staticmethod
    def _norm_key(p: str) -> str:
        """Case- and Unicode-normalized path key for macOS APFS/HFS+/exFAT collision checks."""
        return unicodedata.normalize("NFD", os.path.normcase(os.path.abspath(p))).lower()

    def _init_db(self):
        """Initializes SQLite database for ACID guarantees and content deduplication during transfer."""
        with self.lock:
            self.conn = sqlite3.connect(self.db_path, check_same_thread=False, timeout=60.0)
            self.conn.execute('PRAGMA synchronous = NORMAL;')

            self.conn.execute('PRAGMA journal_mode = WAL;')
            self.conn.execute('PRAGMA cache_size = -64000;')  # 64MB RAM index cache
            self.conn.execute('PRAGMA temp_store = MEMORY;')
            self.conn.execute('''
                CREATE TABLE IF NOT EXISTS copies (
                    source_path TEXT PRIMARY KEY,
                    dest_path TEXT,
                    status TEXT,
                    size INTEGER,
                    mtime REAL,
                    part_hash TEXT,
                    duplicate_of TEXT,
                    trashed_path TEXT DEFAULT ''
                )
            ''')
            # Migrations for databases written by earlier versions.
            cursor = self.conn.execute("PRAGMA table_info(copies)")
            columns = [row[1] for row in cursor.fetchall()]
            if 'part_hash' not in columns:
                self.conn.execute("ALTER TABLE copies ADD COLUMN part_hash TEXT")
            # duplicate_of records WHICH destination file a skipped duplicate
            # matched. Without it a DUPLICATE_SKIPPED row is a dead end: the
            # "Move Duplicates to Trash" button had no way to confirm the
            # surviving copy still existed before trashing the user's original.
            if 'duplicate_of' not in columns:
                self.conn.execute("ALTER TABLE copies ADD COLUMN duplicate_of TEXT")
            if 'trashed_path' not in columns:
                self.conn.execute("ALTER TABLE copies ADD COLUMN trashed_path TEXT DEFAULT ''")

            self.conn.execute("CREATE INDEX IF NOT EXISTS idx_size_hash ON copies(size, part_hash)")

            # Code projects are copied wholesale rather than file-by-file, so
            # they need their own checkpoint. Kept out of `copies` because
            # verify_transfer size-checks every row there, and a directory row
            # would always look like a mismatch.
            self.conn.execute('''
                CREATE TABLE IF NOT EXISTS projects (
                    source_path TEXT PRIMARY KEY,
                    dest_path TEXT,
                    status TEXT
                )
            ''')
            self.conn.commit()

    def ensure_dir(self, dir_path: str):
        """Fast path directory creation: avoids repeated syscalls if directory was already created."""
        key = self._norm_key(dir_path)
        if key not in self.created_dirs:
            with self.dirs_lock:
                if key not in self.created_dirs:
                    os.makedirs(dir_path, exist_ok=True)
                    self.created_dirs.add(key)

    def start_caffeinate(self):
        """Prevents macOS from sleeping during long HDD transfers."""
        try:
            self.caffeinate_process = subprocess.Popen(["caffeinate", "-dims"])
            # Safety net: if the app crashes or is killed before stop_caffeinate
            # runs, the orphaned caffeinate would otherwise keep the Mac awake
            # indefinitely.
            atexit.register(self.stop_caffeinate)
        except Exception:
            pass

    def stop_caffeinate(self):
        proc = self.caffeinate_process
        if not proc:
            return
        self.caffeinate_process = None
        try:
            if proc.poll() is None:
                proc.terminate()
            # Reap the child so it does not linger as a zombie.
            proc.wait(timeout=5)
        except Exception:
            try:
                proc.kill()
            except Exception:
                pass

    def _select_one(self, sql: str, params: tuple = ()):
        """First row of a query, or None. The caller must hold self.lock.

        The checkpoint connection is shared by the transfer's worker threads,
        and macOS's system SQLite is built with THREADSAFE=2, which means a
        connection must never be used by two threads at the same time. A
        cursor that still holds an unfinished statement calls sqlite3_reset()
        when Python frees it. If the cursor outlives `with self.lock:` (for
        example a local variable freed when the method returns), that reset
        runs outside the lock, at the same time as another worker's query.
        That race crashed resumed transfers with a segmentation fault. So the
        cursor is always closed here, while the lock is still held.
        """
        cursor = self.conn.execute(sql, params)
        try:
            return cursor.fetchone()
        finally:
            cursor.close()

    def is_already_copied(self, source_path: str) -> bool:
        """Check if file was already successfully copied in a previous run and remains intact."""
        with self.lock:
            row = self._select_one(
                'SELECT dest_path, status, size, mtime, COALESCE(duplicate_of, "") '
                'FROM copies WHERE source_path = ?',
                (source_path,)
            )
        if not row or row[1] != 'completed':
            return False
        dest_path, _status, rec_size, rec_mtime, duplicate_of = row
        try:
            st = os.stat(source_path)
            if rec_size is not None and st.st_size != rec_size:
                return False
            if rec_mtime and rec_mtime > 0 and abs(st.st_mtime - rec_mtime) > 2.0:
                return False
            check_target = duplicate_of if dest_path == "DUPLICATE_SKIPPED" else dest_path
            if not check_target or not os.path.exists(check_target):
                return False
            if rec_size is not None and os.path.getsize(check_target) != rec_size:
                return False
        except OSError:
            return False
        return True

    def has_completed_owner(self, dest_path: str, exclude_source: str = "") -> bool:
        """Returns True if another completed source_path in the DB already owns dest_path."""
        if not dest_path:
            return False
        with self.lock:
            cursor = self.conn.execute(
                "SELECT source_path FROM copies WHERE dest_path = ? AND status = 'completed'",
                (dest_path,)
            )
            try:
                rows = cursor.fetchall()
            finally:
                cursor.close()  # inside the lock: see _select_one
        for (src,) in rows:
            if src != exclude_source:
                return True
        return False

    def is_project_dissolved(self, source_path: str) -> bool:
        """Returns True if a code project was previously dissolved by the user."""
        with self.lock:
            row = self._select_one(
                'SELECT status FROM projects WHERE source_path = ?', (source_path,)
            )
        return bool(row and row[0] == 'dissolved')

    def mark_project_dissolved(self, project_folder_path: str):
        """Marks a project in the checkpoint DB as dissolved so re-runs do not re-copy it intact."""
        with self.lock:
            cur = self.conn.execute(
                "UPDATE projects SET status = 'dissolved' WHERE dest_path = ? OR source_path = ?",
                (project_folder_path, project_folder_path)
            )
            updated = cur.rowcount
            cur.close()  # inside the lock: see _select_one
            if updated == 0:
                self.conn.execute(
                    "INSERT OR REPLACE INTO projects (source_path, dest_path, status) VALUES (?, ?, 'dissolved')",
                    (project_folder_path, project_folder_path)
                )
            self.conn.commit()

    def get_project_copy(self, source_path: str) -> Optional[str]:
        """Destination of a previously copied project, or None."""
        with self.lock:
            row = self._select_one(
                'SELECT dest_path, status FROM projects WHERE source_path = ?', (source_path,)
            )
        if row and row[1] == 'completed' and row[0] and os.path.isdir(row[0]):
            return row[0]
        return None

    def record_project(self, source_path: str, dest_path: str, status: str = "completed"):
        """Checkpoint a copied code project so a re-run does not clone it again."""
        with self.lock:
            self.conn.execute('''
                INSERT OR REPLACE INTO projects (source_path, dest_path, status)
                VALUES (?, ?, ?)
            ''', (source_path, dest_path, status))
            self.conn.commit()

    def record_copy(self, source_path: str, dest_path: str, size: int, mtime: float,
                    part_hash: str = "", status: str = "completed", duplicate_of: str = ""):
        """Atomic write to checkpoint DB with size, status, and hash indexing.

        duplicate_of is the destination file a DUPLICATE_SKIPPED row matched.
        It is what makes trashing the source safe later: without it there is no
        way to confirm the surviving copy is still present.
        """
        if not part_hash and dest_path != "DUPLICATE_SKIPPED" and status == "completed" and os.path.exists(dest_path):
            part_hash = self._get_part_hash(dest_path, size)

        with self.lock:
            # Never overwrite a source file's own primary completed copy record
            # with DUPLICATE_SKIPPED pointing to itself (e.g. if a source file's
            # mtime was touched and re-evaluated against its own destination copy).
            if dest_path == "DUPLICATE_SKIPPED" and duplicate_of:
                existing = self._select_one(
                    "SELECT dest_path, status FROM copies WHERE source_path = ?",
                    (source_path,)
                )
                if existing and existing[1] == "completed" and existing[0] == duplicate_of:
                    dest_path = duplicate_of
                    duplicate_of = ""

            self.conn.execute('''
                INSERT OR REPLACE INTO copies (source_path, dest_path, status, size, mtime, part_hash, duplicate_of)
                VALUES (?, ?, ?, ?, ?, ?, ?)
            ''', (source_path, dest_path, status, size, mtime, part_hash, duplicate_of))
            self._uncommitted += 1
            if self._uncommitted >= 50:
                self.conn.commit()
                self._uncommitted = 0

    def set_finder_tag(self, filepath: str, color_num: str, tag_name: str):
        """Applies a native macOS Finder tag to a file using binary plist format while preserving existing tags."""
        try:
            tag_entry = f"{tag_name}\n{color_num}"
            attr_name = 'com.apple.metadata:_kMDItemUserTags'
            existing_tags = []
            try:
                raw_existing = xattr.getxattr(filepath, attr_name)
                parsed = plistlib.loads(raw_existing)
                if isinstance(parsed, list):
                    existing_tags = [str(t) for t in parsed]
            except Exception:
                existing_tags = []

            if tag_entry not in existing_tags:
                existing_tags.append(tag_entry)

            plist_data = plistlib.dumps(existing_tags, fmt=plistlib.FMT_BINARY)
            _clear_immutable(filepath)
            st = os.stat(filepath)
            st_mode = stat.S_IMODE(st.st_mode)
            need_chmod = not (st_mode & stat.S_IWUSR)
            if need_chmod:
                os.chmod(filepath, st_mode | stat.S_IWUSR)
            try:
                xattr.setxattr(filepath, attr_name, plist_data)
            finally:
                if need_chmod:
                    try:
                        os.chmod(filepath, st_mode)
                    except OSError:
                        pass
                try:
                    os.utime(filepath, ns=(st.st_atime_ns, st.st_mtime_ns))
                except OSError:
                    pass
        except Exception:
            pass

    def _get_part_hash(self, filepath: str, size: int) -> str:
        """Fast hash: reads first and last 1MB of large files to prevent HDD thrashing."""
        if not _is_regular_file(filepath):
            return ""
        h = hashlib.sha256()
        chunk_size = 1024 * 1024  # 1MB
        try:
            with open(filepath, 'rb') as f:
                actual_size = os.fstat(f.fileno()).st_size
                if actual_size <= chunk_size * 2:
                    # File is small enough, hash the whole thing
                    h.update(f.read(chunk_size * 2))
                else:
                    # Part-hash large videos
                    h.update(f.read(chunk_size))
                    f.seek(-chunk_size, os.SEEK_END)
                    h.update(f.read(chunk_size))
        except Exception:
            return ""
        return h.hexdigest()

    def is_content_duplicate(self, source_path: str, size: int) -> Tuple[bool, str, str]:
        """
        Checks if an identical file has already been copied anywhere in dest.
        Returns (is_dup, part_hash, twin_dest_path).
        """
        # 0-byte empty files (e.g. __init__.py, .gitkeep, empty placeholders) have
        # no content payload and must not be treated as global duplicates of one another.
        if size <= 0:
            return False, "", ""

        # Cheap existence probe first, so files with a brand-new size skip the
        # hash entirely.
        with self.lock:
            probe = self._select_one(
                "SELECT 1 FROM copies WHERE size = ? AND status = 'completed' "
                "AND dest_path NOT IN ('DUPLICATE_SKIPPED', '') LIMIT 1",
                (size,)
            )
        if probe is None:
            return False, "", ""

        src_hash = self._get_part_hash(source_path, size)
        if not src_hash:
            return False, "", ""

        with self.lock:
            rows = self.conn.execute(
                "SELECT dest_path FROM copies "
                "WHERE size = ? AND part_hash = ? AND status = 'completed' "
                "AND dest_path NOT IN ('DUPLICATE_SKIPPED', '')",
                (size, src_hash)
            ).fetchall()
            # Rows written before part_hash existed still have to be checked,
            # but they are a bounded legacy set rather than every same-size row.
            legacy_rows = self.conn.execute(
                "SELECT source_path, dest_path FROM copies "
                "WHERE size = ? AND (part_hash IS NULL OR part_hash = '') "
                "AND status = 'completed' "
                "AND dest_path NOT IN ('DUPLICATE_SKIPPED', '')",
                (size,)
            ).fetchall()

        for (dest_path,) in rows:
            if not dest_path or not os.path.exists(dest_path):
                continue
            if files_are_identical(source_path, dest_path):
                return True, src_hash, dest_path

        for leg_src, dest_path in legacy_rows:
            if not dest_path or not os.path.exists(dest_path):
                continue
            cand_hash = self._get_part_hash(dest_path, size)
            if cand_hash:
                with self.lock:
                    self.conn.execute(
                        "UPDATE copies SET part_hash = ? WHERE source_path = ?",
                        (cand_hash, leg_src)
                    )
            if cand_hash != src_hash:
                continue
            if files_are_identical(source_path, dest_path):
                return True, src_hash, dest_path

        return False, src_hash, ""

    def _release_size_slot_unlocked(self, size: int):
        if size > 0 and size in self.in_flight_sizes:
            if self.in_flight_sizes[size] <= 1:
                del self.in_flight_sizes[size]
            else:
                self.in_flight_sizes[size] -= 1

    def _reserve_path(self, path: str, size: int, already_counted_size: bool = False):
        norm = self._norm_key(path)
        self.reserved_paths.add(norm)
        self.reserved_path_sizes[norm] = size
        if size > 0 and not already_counted_size:
            self.in_flight_sizes[size] = self.in_flight_sizes.get(size, 0) + 1

    def release_reservation(self, target_path: Optional[str]):
        """Releases reservation lock on target path after copy attempt."""
        if target_path:
            norm = self._norm_key(target_path)
            with self.reserved_lock:
                self.reserved_paths.discard(norm)
                sz = self.reserved_path_sizes.pop(norm, 0)
                self._release_size_slot_unlocked(sz)

    def resolve_destination(self, base_dest: str, filename: str, source_size: int, source_path: str) -> Tuple[Optional[str], str, str]:
        """
        Calculates destination path. Handles global content duplicates, parallel thread reservations, and filename collisions.
        Returns (None, part_hash, twin_path) if file is an exact duplicate (should be skipped).
        Returns (new_path, part_hash, "") if file should be copied.
        """
        deadline = time.time() + 300

        # Atomically wait for and claim the in-flight size slot under reserved_lock
        # so two threads with identical files of the same size (even with different
        # names) are serialized with zero TOCTOU race window.
        claimed_size_slot = False
        if source_size > 0:
            while time.time() < deadline:
                with self.reserved_lock:
                    if self.in_flight_sizes.get(source_size, 0) == 0:
                        self.in_flight_sizes[source_size] = 1
                        claimed_size_slot = True
                        break
                time.sleep(0.02)
            if not claimed_size_slot:
                with self.reserved_lock:
                    self.in_flight_sizes[source_size] = self.in_flight_sizes.get(source_size, 0) + 1
                    claimed_size_slot = True

        def _drop_size_slot():
            nonlocal claimed_size_slot
            if claimed_size_slot:
                with self.reserved_lock:
                    self._release_size_slot_unlocked(source_size)
                claimed_size_slot = False

        try:
            # 1. Global content deduplication check
            is_dup, src_hash, twin = self.is_content_duplicate(source_path, source_size)
            if is_dup:
                _drop_size_slot()
                return None, src_hash, twin

            dest_dir = os.path.dirname(base_dest)
            self.ensure_dir(dest_dir)

            # Ensure the base filename + ".tmp" fits within the 255-byte macOS NAME_MAX limit
            stem, ext = split_filename_ext(filename)
            safe_filename = _truncate_filename_utf8(stem, ext, max_bytes=251)
            base_dest = os.path.join(dest_dir, safe_filename)

            def _cleanup_orphan_tmp(p: str):
                norm_p = self._norm_key(p)
                tmp_p = p + ".tmp"
                if norm_p not in self.reserved_paths and not os.path.lexists(p) and os.path.lexists(tmp_p):
                    _force_remove(tmp_p)

            def is_path_busy(p: str) -> bool:
                _cleanup_orphan_tmp(p)
                return os.path.lexists(p) or os.path.lexists(p + ".tmp") or (self._norm_key(p) in self.reserved_paths)

            # If the target path is free, reserve it immediately for this thread.
            # If another worker currently holds the reservation (including while it is
            # writing base_dest + ".tmp"), wait for it to finish and release the
            # reservation rather than falling through to a "name_1" collision name.
            while True:
                with self.reserved_lock:
                    if not is_path_busy(base_dest):
                        self._reserve_path(base_dest, source_size, already_counted_size=claimed_size_slot)
                        claimed_size_slot = False
                        return base_dest, src_hash, ""
                    held_by_other_thread = self._norm_key(base_dest) in self.reserved_paths
                if held_by_other_thread and time.time() < deadline:
                    time.sleep(0.02)
                    continue
                break

            # Base dest exists! Check if the destination file is genuinely the same
            # file. Confirmed byte-for-byte.
            if source_size > 0 and os.path.exists(base_dest):
                try:
                    if os.path.getsize(base_dest) == source_size:
                        if not src_hash:
                            src_hash = self._get_part_hash(source_path, source_size)
                        if files_are_identical(source_path, base_dest):
                            _drop_size_slot()
                            return None, src_hash, base_dest
                except OSError:
                    pass

            # Sizes differ or contents differ. Find a free collision counter path.
            counter = 1
            while True:
                suffix = f"_{counter}{ext}"
                cand_name = _truncate_filename_utf8(stem, suffix, max_bytes=251)
                candidate = os.path.join(dest_dir, cand_name)
                with self.reserved_lock:
                    if not is_path_busy(candidate):
                        self._reserve_path(candidate, source_size, already_counted_size=claimed_size_slot)
                        claimed_size_slot = False
                        return candidate, src_hash, ""
                    cand_held = self._norm_key(candidate) in self.reserved_paths
                if cand_held and time.time() < deadline:
                    time.sleep(0.02)
                    continue
                if source_size > 0 and os.path.exists(candidate):
                    try:
                        if os.path.getsize(candidate) == source_size and files_are_identical(source_path, candidate):
                            if not src_hash:
                                src_hash = self._get_part_hash(source_path, source_size)
                            _drop_size_slot()
                            return None, src_hash, candidate
                    except OSError:
                        pass
                counter += 1
        except Exception:
            _drop_size_slot()
            raise

    def copy_file(self, source_path: str, target_path: str):
        """Native macOS Darwin Kernel Copy Engine via copyfile syscall with safe temp file handling."""
        if os.path.islink(source_path):
            safe_copy(source_path, target_path)
            return
        if not _is_regular_file(source_path):
            raise IOError(f"Unsupported special file (not a regular file): {source_path}")
        tmp_path = target_path + ".tmp"
        if os.path.lexists(tmp_path):
            _force_remove(tmp_path)
        src_size = os.path.getsize(source_path)
        try:
            copied = native_copy(source_path, tmp_path)

            if not copied:
                # copy2 before copyfile: it carries permissions, timestamps and
                # flags.
                try:
                    shutil.copy2(source_path, tmp_path)
                    _clear_immutable(tmp_path)
                    restore_xattrs(source_path, tmp_path)
                    restore_timestamps(source_path, tmp_path)
                except OSError:
                    # exFAT / FAT32 reject xattrs, ACLs or chflags (Errno 22).
                    # Avoid rewriting bytes if copy2 already wrote the full file
                    # before failing on chflags/copystat.
                    if not (os.path.exists(tmp_path) and not os.path.islink(tmp_path) and os.path.getsize(tmp_path) == src_size):
                        _force_remove(tmp_path)
                        shutil.copyfile(source_path, tmp_path)
                    _clear_immutable(tmp_path)
                    restore_xattrs(source_path, tmp_path)
                    restore_timestamps(source_path, tmp_path)
                    restore_mode(source_path, tmp_path)

            if os.path.exists(tmp_path) and not os.path.islink(tmp_path):
                tmp_size = os.path.getsize(tmp_path)
                if tmp_size != src_size:
                    raise IOError(f"Incomplete file copy: source is {src_size} bytes, but copy is {tmp_size} bytes.")
                _clear_immutable(tmp_path)
                if os.path.lexists(target_path):
                    _force_remove(target_path)
                os.replace(tmp_path, target_path)
            else:
                raise IOError("Copy failed: temp target file was not created.")
        finally:
            if os.path.lexists(tmp_path):
                _force_remove(tmp_path)

    def close(self):
        with self.lock:
            try:
                self.conn.commit()
            except Exception:
                pass
            self.conn.close()
        self.stop_caffeinate()

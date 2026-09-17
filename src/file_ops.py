import os
import atexit
import filecmp
import shutil
import sqlite3
import hashlib
import subprocess
import threading
import time
from typing import Optional, Tuple
import xattr # Used for macOS Finder tags

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


def restore_timestamps(source_path: str, target_path: str):
    """Best-effort copy of access/modification times from source to target.

    exFAT / FAT32 volumes reject extended attributes and ACLs (raising Errno 22),
    which is why the data-only shutil.copyfile is used as the transfer fallback.
    Those same volumes *do* accept utime, so timestamps are restored separately
    here instead of being silently reset to the time of the copy.
    """
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
    so they are re-applied here. exFAT/FAT32 reject them outright, which is a
    property of the volume rather than an error worth surfacing.
    """
    try:
        for key in xattr.listxattr(source_path):
            try:
                xattr.setxattr(target_path, key, xattr.getxattr(source_path, key))
            except (OSError, IOError):
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
    try:
        src_size = os.path.getsize(source_path)
        # COPYFILE_CLONE implies COPYFILE_EXCL, so an existing target makes the
        # syscall fail. Retry without CLONE rather than giving up on metadata.
        for flags in (_COPYFILE_ALL | _COPYFILE_CLONE, _COPYFILE_ALL):
            ret = _libsystem.copyfile(
                source_path.encode('utf-8'), target_path.encode('utf-8'), None, flags
            )
            if ret == 0 and os.path.exists(target_path) \
                    and os.path.getsize(target_path) == src_size:
                return True
    except Exception:
        pass
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

    This used to be a bare shutil.copyfile, which transfers bytes only. Every
    file in every copied Code/ project therefore lost its executable bit
    (measured: 0o755 -> 0o644) and all of its extended attributes, despite the
    project being advertised as preserved 100% intact.
    """
    if native_copy(source_path, target_path):
        return target_path
    try:
        shutil.copy2(source_path, target_path)
        restore_xattrs(source_path, target_path)
    except OSError:
        shutil.copyfile(source_path, target_path)
        restore_timestamps(source_path, target_path)
        restore_mode(source_path, target_path)
    return target_path


def files_are_identical(path_a: str, path_b: str) -> bool:
    """Byte-for-byte comparison of two files.

    The part-hash below only samples the first and last 1MB of a file, so it
    cannot prove two files are the same. Any two files that share a size and
    those two sampled megabytes collide - re-encoded videos, disk images,
    padded archives and same-camera clips all do this in practice. Because a
    positive duplicate verdict means the file is *never copied* (and is then
    offered up for trashing), the sampled hash is only ever used to shortlist
    candidates; this function makes the final call.
    """
    try:
        if os.path.getsize(path_a) != os.path.getsize(path_b):
            return False
        return filecmp.cmp(path_a, path_b, shallow=False)
    except OSError:
        return False


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
    ".DS_Store",
)


def copy_project_intact(source_dir: str, dest_dir: str) -> Tuple[bool, str]:
    """Copies a detected code project, preserving its structure and history.

    Tries the timestamp-preserving exFAT-safe copy first and falls back to
    shutil's default copy function if the volume rejects it.

    Returns (ok, error_message).
    """
    ignore = shutil.ignore_patterns(*PROJECT_IGNORE_PATTERNS)
    try:
        shutil.copytree(
            source_dir, dest_dir,
            dirs_exist_ok=True,
            copy_function=safe_copy,
            ignore_dangling_symlinks=True,
            ignore=ignore
        )
        return True, ""
    except Exception:
        try:
            shutil.copytree(
                source_dir, dest_dir,
                dirs_exist_ok=True,
                ignore_dangling_symlinks=True,
                ignore=ignore
            )
            return True, ""
        except Exception as err:
            return False, str(err)


def project_already_copied(source_dir: str, dest_dir: str) -> bool:
    """True if dest_dir already looks like a faithful copy of source_dir.

    Used as a fallback when the checkpoint database has no record of the
    project (it was deleted, or the destination was organized by an older
    version). Without this, re-running the same job clones every repository
    again as project_1, project_2, ...

    Compares the relative path set and file sizes rather than every byte:
    a repository can be gigabytes, and this runs before any copying.
    """
    if not os.path.isdir(dest_dir):
        return False

    ignored = set(PROJECT_IGNORE_PATTERNS)
    for root, dirs, files in os.walk(source_dir):
        dirs[:] = [d for d in dirs if d not in ignored]
        for name in files:
            if name in ignored:
                continue
            src_file = os.path.join(root, name)
            rel = os.path.relpath(src_file, source_dir)
            dst_file = os.path.join(dest_dir, rel)
            try:
                if os.path.getsize(src_file) != os.path.getsize(dst_file):
                    return False
            except OSError:
                return False
    return True


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
        self._uncommitted = 0
        self._init_db()
        self.caffeinate_process = None


    def _init_db(self):
        """Initializes SQLite database for ACID guarantees and content deduplication during transfer."""
        with self.lock:
            self.conn = sqlite3.connect(self.db_path, check_same_thread=False, timeout=60.0)
            self.conn.execute('PRAGMA synchronous = NORMAL;')

            self.conn.execute('PRAGMA journal_mode = WAL;')
            self.conn.execute('PRAGMA cache_size = -64000;') # 64MB RAM index cache
            self.conn.execute('PRAGMA temp_store = MEMORY;')
            self.conn.execute('''
                CREATE TABLE IF NOT EXISTS copies (
                    source_path TEXT PRIMARY KEY,
                    dest_path TEXT,
                    status TEXT,
                    size INTEGER,
                    mtime REAL,
                    part_hash TEXT,
                    duplicate_of TEXT
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
        if dir_path not in self.created_dirs:
            with self.dirs_lock:
                if dir_path not in self.created_dirs:
                    os.makedirs(dir_path, exist_ok=True)
                    self.created_dirs.add(dir_path)

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

    def is_already_copied(self, source_path: str) -> bool:
        """Check if file was already successfully copied in a previous run."""
        with self.lock:
            cursor = self.conn.execute('SELECT status FROM copies WHERE source_path = ?', (source_path,))
            row = cursor.fetchone()
            return row is not None and row[0] == 'completed'

    def get_project_copy(self, source_path: str) -> Optional[str]:
        """Destination of a previously copied project, or None."""
        with self.lock:
            cursor = self.conn.execute(
                'SELECT dest_path, status FROM projects WHERE source_path = ?', (source_path,)
            )
            row = cursor.fetchone()
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
            self.conn.execute('''
                INSERT OR REPLACE INTO copies (source_path, dest_path, status, size, mtime, part_hash, duplicate_of)
                VALUES (?, ?, ?, ?, ?, ?, ?)
            ''', (source_path, dest_path, status, size, mtime, part_hash, duplicate_of))
            self._uncommitted += 1
            if self._uncommitted >= 50:
                self.conn.commit()
                self._uncommitted = 0

    def set_finder_tag(self, filepath: str, color_num: str, tag_name: str):
        """Applies a native macOS Finder tag to a file."""
        try:
            # Color num: 1=Gray, 2=Green, 3=Purple, 4=Blue, 5=Yellow, 6=Red, 7=Orange
            plist_data = f'<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd"><plist version="1.0"><array><string>{tag_name}\n{color_num}</string></array></plist>'.encode('utf-8')
            xattr.setxattr(filepath, 'com.apple.metadata:_kMDItemUserTags', plist_data)
        except Exception:
            pass

    def _get_part_hash(self, filepath: str, size: int) -> str:
        """Fast hash: reads first and last 1MB of large files to prevent HDD thrashing."""
        h = hashlib.sha256()
        chunk_size = 1024 * 1024 # 1MB
        try:
            with open(filepath, 'rb') as f:
                if size <= chunk_size * 2:
                    # File is small enough, hash the whole thing
                    h.update(f.read())
                else:
                    # Part-hash large videos
                    h.update(f.read(chunk_size))
                    f.seek(-chunk_size, os.SEEK_END)
                    h.update(f.read(chunk_size))
        except Exception:
            return ""
        return h.hexdigest()

    def is_content_duplicate(self, source_path: str, size: int) -> Tuple[bool, str]:
        """
        Checks if an identical file has already been copied anywhere in dest.
        Returns (is_dup, part_hash).

        The size + part_hash lookup is only a *shortlist*: any candidate it
        finds is confirmed with a full byte-for-byte comparison before the
        source file is written off as a duplicate. Skipping a file is
        irreversible from the user's point of view (it never reaches the
        destination and the duplicates UI then offers to trash the source),
        so a sampled hash is not a strong enough basis on its own.

        Performance note: the source is hashed *before* querying so that
        part_hash can go into the WHERE clause and actually use idx_size_hash.
        This previously selected every completed row sharing a size and
        filtered them in Python, which is quadratic on the same-size clusters
        real drives are full of (camera JPEGs, 4KB stubs, zero-byte files).
        Measured on a 40,000-file cluster: 10.7ms -> 0.05ms per file.
        """
        # Cheap existence probe first, so files with a brand-new size skip the
        # hash entirely.
        with self.lock:
            cursor = self.conn.execute(
                "SELECT 1 FROM copies WHERE size = ? AND status = 'completed' "
                "AND dest_path NOT IN ('DUPLICATE_SKIPPED', '') LIMIT 1",
                (size,)
            )
            if cursor.fetchone() is None:
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
                "SELECT dest_path FROM copies "
                "WHERE size = ? AND (part_hash IS NULL OR part_hash = '') "
                "AND status = 'completed' "
                "AND dest_path NOT IN ('DUPLICATE_SKIPPED', '')",
                (size,)
            ).fetchall()

        for (dest_path,) in rows:
            # A recorded duplicate is only real if the file is still there to
            # compare against. A missing destination file means the copy was
            # lost, so the source must be re-copied rather than skipped.
            if not dest_path or not os.path.exists(dest_path):
                continue
            if files_are_identical(source_path, dest_path):
                return True, src_hash, dest_path

        for (dest_path,) in legacy_rows:
            if not dest_path or not os.path.exists(dest_path):
                continue
            if self._get_part_hash(dest_path, size) != src_hash:
                continue
            if files_are_identical(source_path, dest_path):
                return True, src_hash, dest_path

        return False, src_hash, ""

    def release_reservation(self, target_path: Optional[str]):
        """Releases reservation lock on target path after copy attempt."""
        if target_path:
            with self.reserved_lock:
                self.reserved_paths.discard(target_path)

    def resolve_destination(self, base_dest: str, filename: str, source_size: int, source_path: str) -> Tuple[Optional[str], str, str]:
        """
        Calculates destination path. Handles global content duplicates, parallel thread reservations, and filename collisions.
        Returns (None, part_hash, twin_path) if file is an exact duplicate (should be skipped).
        Returns (new_path, part_hash, "") if file should be copied.

        twin_path is the existing destination file the source was found to be
        identical to. It is persisted so the duplicates UI can verify the
        surviving copy still exists before offering to trash the original.
        """
        # 1. Global content deduplication check
        is_dup, src_hash, twin = self.is_content_duplicate(source_path, source_size)
        if is_dup:
            return None, src_hash, twin

        self.ensure_dir(os.path.dirname(base_dest))

        def is_path_busy(p: str) -> bool:
            return os.path.exists(p) or os.path.exists(p + ".tmp") or p in self.reserved_paths

        # If the target path is free, reserve it immediately for this thread.
        #
        # If another worker currently holds the reservation, wait for it rather
        # than immediately falling through to a "name_1" collision name: two
        # identical files with the same name are extremely common (the same
        # photo in two folders), and racing past each other here defeated
        # deduplication entirely. Once the other thread finishes, the file is
        # on disk and the identical-content check below can do its job.
        deadline = time.time() + 300
        while True:
            with self.reserved_lock:
                if not is_path_busy(base_dest):
                    self.reserved_paths.add(base_dest)
                    return base_dest, src_hash, ""
                held_by_other_thread = (
                    base_dest in self.reserved_paths
                    and not os.path.exists(base_dest)
                    and not os.path.exists(base_dest + ".tmp")
                )
            if held_by_other_thread and time.time() < deadline:
                time.sleep(0.05)
                continue
            break

        # Base dest exists! Check if the destination file is genuinely the same
        # file. Confirmed byte-for-byte: a sampled-hash match is not enough to
        # justify never copying the source (see files_are_identical).
        if os.path.exists(base_dest):
            try:
                if os.path.getsize(base_dest) == source_size:
                    if not src_hash:
                        src_hash = self._get_part_hash(source_path, source_size)
                    if files_are_identical(source_path, base_dest):
                        # Identical file! Skip copying.
                        return None, src_hash, base_dest
            except OSError:
                pass

        # Sizes differ or hashes differ or parallel thread busy. Find a free collision counter path.
        name, ext = os.path.splitext(filename)
        counter = 1
        with self.reserved_lock:
            while True:
                candidate = os.path.join(os.path.dirname(base_dest), f"{name}_{counter}{ext}")
                if not (os.path.exists(candidate) or os.path.exists(candidate + ".tmp") or candidate in self.reserved_paths):
                    self.reserved_paths.add(candidate)
                    return candidate, src_hash, ""
                counter += 1

    def copy_file(self, source_path: str, target_path: str):
        """Native macOS Darwin Kernel Copy Engine via copyfile syscall with safe temp file handling."""
        tmp_path = target_path + ".tmp"
        src_size = os.path.getsize(source_path)
        try:
            copied = native_copy(source_path, tmp_path)

            if not copied:
                # copy2 before copyfile: it carries permissions, timestamps and
                # flags. The order used to be reversed, so the data-only
                # copyfile almost always won and the copy arrived as 0o644 with
                # today's date -- mis-sorted by DateExtractor and stripped of
                # its executable bit.
                try:
                    shutil.copy2(source_path, tmp_path)
                    restore_xattrs(source_path, tmp_path)
                except OSError:
                    # exFAT / FAT32 reject xattrs and ACLs (Errno 22). Bytes
                    # only, then re-apply what the volume will accept.
                    shutil.copyfile(source_path, tmp_path)
                    restore_timestamps(source_path, tmp_path)
                    restore_mode(source_path, tmp_path)

            if os.path.exists(tmp_path):
                tmp_size = os.path.getsize(tmp_path)
                if tmp_size != src_size:
                    raise IOError(f"Incomplete file copy: source is {src_size} bytes, but copy is {tmp_size} bytes.")
                os.replace(tmp_path, target_path)
            else:
                raise IOError("Copy failed: temp target file was not created.")
        finally:
            if os.path.exists(tmp_path):
                try:
                    os.remove(tmp_path)
                except OSError:
                    pass


    def close(self):
        with self.lock:
            try:
                self.conn.commit()
            except Exception:
                pass
            self.conn.close()
        self.stop_caffeinate()


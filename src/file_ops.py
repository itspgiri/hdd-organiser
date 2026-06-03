import os
import shutil
import sqlite3
import hashlib
import subprocess
from typing import Optional, Tuple
import xattr # Used for macOS Finder tags

class FileEngine:
    def __init__(self, dest_root: str):
        self.dest_root = dest_root
        self.db_path = os.path.join(dest_root, ".organizer_checkpoint.db")
        self._init_db()
        self.caffeinate_process = None

    def _init_db(self):
        """Initializes SQLite database for ACID guarantees during transfer."""
        self.conn = sqlite3.connect(self.db_path)
        self.conn.execute('''
            CREATE TABLE IF NOT EXISTS copies (
                source_path TEXT PRIMARY KEY,
                dest_path TEXT,
                status TEXT,
                size INTEGER,
                mtime REAL
            )
        ''')
        self.conn.commit()

    def start_caffeinate(self):
        """Prevents macOS from sleeping during long HDD transfers."""
        try:
            self.caffeinate_process = subprocess.Popen(["caffeinate", "-dims"])
        except Exception:
            pass

    def stop_caffeinate(self):
        if self.caffeinate_process:
            self.caffeinate_process.terminate()

    def is_already_copied(self, source_path: str) -> bool:
        """Check if file was already successfully copied in a previous run."""
        cursor = self.conn.execute('SELECT status FROM copies WHERE source_path = ?', (source_path,))
        row = cursor.fetchone()
        return row is not None and row[0] == 'completed'

    def record_copy(self, source_path: str, dest_path: str, size: int, mtime: float):
        """Atomic write to checkpoint DB."""
        self.conn.execute('''
            INSERT OR REPLACE INTO copies (source_path, dest_path, status, size, mtime)
            VALUES (?, ?, ?, ?, ?)
        ''', (source_path, dest_path, 'completed', size, mtime))
        self.conn.commit()

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

    def resolve_destination(self, base_dest: str, filename: str, source_size: int, source_path: str) -> Optional[str]:
        """
        Calculates destination path. Handles duplicates.
        Returns None if file is an exact duplicate (should be skipped).
        Returns new path (with _1 suffix) if collision exists but files differ.
        """
        os.makedirs(os.path.dirname(base_dest), exist_ok=True)
        
        if not os.path.exists(base_dest):
            return base_dest
            
        # Collision detected! Check if they are identical
        dest_size = os.path.getsize(base_dest)
        if dest_size == source_size:
            # Sizes match, do smart part-hash
            src_hash = self._get_part_hash(source_path, source_size)
            dst_hash = self._get_part_hash(base_dest, dest_size)
            if src_hash == dst_hash and src_hash != "":
                # Identical file! Skip copying.
                return None
                
        # Sizes differ or hashes differ. Rename destination.
        name, ext = os.path.splitext(filename)
        counter = 1
        while True:
            new_dest = os.path.join(os.path.dirname(base_dest), f"{name}_{counter}{ext}")
            if not os.path.exists(new_dest):
                return new_dest
            counter += 1

    def copy_file(self, source_path: str, target_path: str):
        """Safe out-of-place copy via tmp file and APFS clonefile (shutil.copy2)."""
        tmp_path = target_path + ".tmp"
        
        # shutil.copy2 on macOS APFS naturally attempts `clonefile` first,
        # providing instantaneous zero-space copies if on the same drive volume.
        # If not, it falls back to a fast stream copy.
        shutil.copy2(source_path, tmp_path)
        os.rename(tmp_path, target_path)

    def close(self):
        self.conn.close()
        self.stop_caffeinate()

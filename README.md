# Drive Organizer 🚀

**Drive Organizer** is a high-performance, ultra-lightweight macOS utility designed to safely reorganize massive, messy storage drives (500GB to 2TB+) into a clean, chronological, structured directory layout. Built with a strict read-only source policy, native APFS zero-copy cloning, byte-exact content deduplication, 1-click duplicate isolation and restore, multi-threaded parallel execution, and intact code repository protection.

Backed by a comprehensive **122-test automated regression suite** and a full-stack security, concurrency, and data-integrity audit ([`BUG_AUDIT_REPORT.md`](BUG_AUDIT_REPORT.md)).

---

## 🌟 Key Features & Performance Stack

| Feature | Functionality | Safety & Performance Mechanism |
| :--- | :--- | :--- |
| **Parallel Worker Pool** | Processes multiple files concurrently across CPU cores. | Mechanical-drive-safe worker cap (4–8 threads), consuming only **15–20 MB RAM total** by streaming 1MB buffers with atomic in-flight size synchronization (`in_flight_sizes`) and NFD/NFC normalized path reservations. |
| **APFS Zero-Copy Fast Path & 3-Tier Copy Engine** | Instantaneous file copying on macOS APFS volumes with universal filesystem fallback. | Uses Darwin kernel `copyfile` (`COPYFILE_ALL \| COPYFILE_CLONE`) for **0.001s Copy-on-Write clones** with 0 bytes of extra storage on APFS, falling back to `shutil.copy2` + explicit `xattr` restoration, and raw-bytes + timestamp/mode restoration on exFAT/FAT32/NTFS. |
| **SQLite WAL + 64MB Cache** | ACID progress & state tracking (`.organizer_checkpoint.db`). | SQLite Write-Ahead Logging (WAL) with 64MB RAM cache and buffered batch commits every 50 files. Tracks both individual files (`copies`) and repositories (`projects`) so re-running a completed job is an instant no-op. |
| **8-Layer Metadata & Date Pipeline** | Chronological media and document date extraction. | 1. EXIF `DateTimeOriginal` ➔ 2. EXIF `DateTimeDigitized` ➔ 3. `Image DateTime` ➔ 4. `GPSDate` (across **JPEG, TIFF, HEIC/HEIF, AVIF, PNG, WebP, and Camera RAWs `.cr2/.cr3/.nef/.arw/.dng/.orf/.raf/.rw2`**, including embedded TIFF header scanning and per-tag validation) ➔ 5. **MP4/MOV/M4V/3GP QuickTime `moov`/`mvhd` Atom** (top-level ISO-BMFF box walker + 2MB head/tail scan for v0 32-bit & v1 64-bit timestamps) ➔ 6. **PDF `/CreationDate` & XMP `CreateDate`** (64KB head/tail scan + UTF-16BE decoding) ➔ 7. **Filename Date Regex** (with lookaround guards against 13-digit epoch/snowflake IDs) ➔ 8. **macOS Earliest Filesystem Timestamp** (`min(st_mtime, st_birthtime)`). |
| **Chronological Screenshot Sorting** | Isolates screenshots into `Media/Screenshots/YYYY/MonthName/`. | Multi-OS & multi-locale regex (`Screenshot`, `Screen Shot`, `Screen_Shot`, `Captura de pantalla`, `Bildschirmfoto`, `Capture d'écran` with NFD/NFC normalization), guarded by camera EXIF checks (`Make`/`Model`/`LensModel`) and video/RAW exclusions so real camera photos and screen recordings are never misclassified. |
| **Apple Live Photo Pairing** | Pairs `.heic`/`.heif`/`.jpg`/`.jpeg` stills and `.mov` video clips together. | Pre-indexes still photo capture dates using NFD/NFC normalized, case-insensitive keys. The photo's capture date is **authoritative for the pair** regardless of scan order, ensuring companion `.mov` clips with reset modification times always land beside their photo. |
| **Intact Code Repository Protection** | Detects development repositories (`.git`, `package.json`, `requirements.txt`, `Cargo.toml`, `go.mod`, `pom.xml`, `build.gradle`). | Preserves repositories 100% intact under `Code/<project_name>/`, **including `.git` history, internal symlinks, executable permissions, and extended attributes**. Heavy build/cache folders (`node_modules`, `.venv`, `target`, `dist`, `.next`, `.gradle`, `__pycache__`) are skipped during copy for speed, while being **counted and itemized in the preview** so nothing disappears silently. |
| **Google Takeout & Drive Zip Auto-Extraction** | Automatically unpacks split or monolithic Google Takeout / Drive `.zip` exports. | Stages extraction off-source onto the destination (or system temp dir) only after user confirmation, with stall-aware timeout scaling, **Zip-Slip (`../`) & symlink traversal protection**, `__MACOSX` filtering, and automatic staging cleanup. |
| **Multi-Source Folder Selection** | Organize multiple messy folders or drives in a single run. | Supports selecting multiple folders or comma-separated paths (`/Volumes/HDD1, /Volumes/HDD2`) while preserving single folder names that contain commas and deduplicating overlapping source trees. |
| **Real-Time Progress, ETA & Run History** | Live speed-sampled progress bar, searchable activity logs, and audit history. | Displays live transfer rate and ETA (`45% • 1,912/4,250 • ⏳ ~02m 14s remaining`), plays a completion chime (`afplay`), and saves every run to **📜 Past Runs & Audit Reports** for historical inspection and CSV export. |
| **System & Drive Protection** | Ultra-gentle on external mechanical HDDs and macOS. | Filters OS junk (`.DS_Store`, `._*` AppleDouble files, `Thumbs.db`, `Desktop.ini`, `.Spotlight-V100`, `.Trashes`), suppresses Spotlight indexing (`.metadata_never_index`), and blocks system sleep during transfers (`caffeinate`). |

---

## 📁 Destination Layout Structure

```text
Destination_Drive/
│
├── 📸 Media/                     <-- Photos, Videos & Camera RAWs (by Capture Date)
│   ├── 2021/
│   │   └── August/
│   │       ├── vacation_01.heic
│   │       ├── vacation_01.mov   <-- Live Photo video paired automatically
│   │       └── portrait.cr3      <-- Camera RAW dated via embedded TIFF EXIF
│   │
│   └── 📸 Screenshots/           <-- Master Chronological Screenshots Directory
│       ├── 2022/
│       │   └── January/
│       │       └── Screen Shot 2022-01-15.png
│       └── 2023/
│           └── May/
│               └── Screenshot_20230512.png
│
├── 📄 Documents/                 <-- Workplace & Personal Documents (Dated via PDF/File Metadata)
│   ├── PDF/                      <-- .pdf (organized by YYYY/MonthName/)
│   ├── Word/                     <-- .docx, .doc, .pages
│   ├── Spreadsheets/             <-- .csv, .xlsx, .xls, .numbers
│   ├── Presentations/            <-- .pptx, .ppt, .key
│   └── Text/                     <-- .txt, .rtf, .md
│
├── 💻 Code/                      <-- Development Repositories & Loose Scripts
│   ├── my-react-app/             <-- Preserved 100% INTACT (with .git, modes & xattrs)
│   ├── python-automation/        <-- Preserved 100% INTACT
│   └── Snippets/                 <-- Loose single script files (.py, .js, .html, .css, .cpp, .sh)
│
├── 🎨 Creative/                  <-- Design Projects & Audio Files
│   ├── Projects/                 <-- .psd, .ai, .xd, .fig, .sketch
│   └── Audio/                    <-- .mp3, .wav, .aac, .flac
│
├── 📦 Archives/                  <-- .zip, .tar.gz, .tar.bz2, .tar.xz, .tar.zst, .tgz, .rar, .7z
│
├── 📚 E-Books/                   <-- .epub, .mobi
│
└── ❓ Unsorted/                  <-- Unknown formats or files with undetectable dates
                                      (Tagged with Yellow macOS Finder Tag "To Review")
```

---

## 🚀 Quick Start & How to Run

### Prerequisites & One-Command Setup

Requires **macOS** and **Python 3.9+**. Create a virtual environment and install dependencies using `make`:

```bash
make setup
```

*(Or manually: `python3 -m venv .venv && .venv/bin/pip install -r requirements.txt`)*

---

### Option 1: Desktop Web Interface (GUI Mode)

Double-click **`Launch App.command`**, run `make run`, or execute in Terminal:

```bash
python3 main.py
```

#### 4-Step GUI Workflow:
1. **Select Messy Source Folder(s)**: Choose one or multiple folders via the native macOS folder picker, or enter comma-separated paths.
2. **Choose Destination Strategy**:
   - **✨ Option A: Brand New Sorted Folder** — Organize into a fresh destination folder on your drive.
   - **📂 Option B: Merge into Already Sorted Folder** — Incrementally append files into an existing organized drive with fast checkpoint resume and zero duplicates.
3. **Select Destination Folder** & keep **`[✓] Run in Dry-Run / Preview Mode first`** enabled.
4. **Interactive Dry-Run Preview & Execution**:
   - Inspect total file counts, data volume, required vs. available disk space, category breakdowns, ignored system junk (`._*`, `.DS_Store`), skipped build directories (`node_modules`, `.venv`), and detected code repositories.
   - Click any category card to preview sample files, or click any detected code project to open the **Folder Inspector** and optionally toggle **`⚡ Sort This as Regular Files (Not a Code Project)`**.
   - Click **`🚀 YES, Execute Full Transfer!`** to start the parallel copy engine.

---

### Option 2: Command Line Interface (CLI Mode)

Double-click **`Launch CLI.command`**, run `make cli`, or execute in Terminal:

#### 1. Interactive CLI Mode (Opens Native macOS Folder Dialogs)
```bash
python3 main.py --cli
```

#### 2. Dry-Run / Preview Mode (100% Read-Only, No Files Written)
```bash
python3 main.py --cli /Volumes/SourceDrive /Volumes/DestinationDrive --preview
```

#### 3. Automated Full Copy Execution
```bash
python3 main.py --cli /Volumes/SourceDrive /Volumes/DestinationDrive --copy
```

---

### Makefile Shortcuts

| Command | Description |
| :--- | :--- |
| `make setup` | Create `.venv` virtual environment and install dependencies from `requirements.txt` |
| `make run` | Launch the native Desktop GUI application (`pywebview` + Flask) |
| `make cli` | Launch Interactive Terminal CLI mode (`rich`) |
| `make preview SOURCE=/src DEST=/dst` | Run a non-destructive dry-run preview in Terminal |
| `make copy SOURCE=/src DEST=/dst` | Execute a full transfer in Terminal without interactive prompts |
| `make test` | Run the full 122-test automated regression suite |
| `make clean` | Remove `__pycache__`, `.pyc`, and `.DS_Store` files |

---

## 🛠️ Post-Organization Review, Verification & Recovery Tools

After a transfer completes (or from the setup/history screens), Drive Organizer provides built-in management utilities:

- **📂 Open Destination Folder in Finder**: 1-click button opens your organized destination directory directly in macOS Finder.
- **📊 Export CSV Audit Log & Past Runs History**: Download a comprehensive spreadsheet report detailing every file's original source path, destination path, byte size, timestamp, transfer status, and duplicate reference (sanitized against spreadsheet formula injection). Past runs can be browsed and exported at any time via **📜 Past Runs & Audit Reports**.
- **🔍 Review Code Projects & Dissolve**: Browse all repositories preserved under `Code/`. Click **`Dissolve & Re-Sort`** on any folder mistakenly identified as a code project to break it open, sort its inner files into `Media/`, `Documents/`, etc., remove duplicate remnants, and record `status = 'dissolved'` in `.organizer_checkpoint.db` so future incremental merges never re-copy it into `Code/`.
- **🗑️ Clean Source Duplicates & ♻️ 1-Click Restore**:
  - **Isolate Duplicates**: View every source file skipped as an exact duplicate (`DUPLICATE_SKIPPED`). Clicking **`Move Duplicates to Trash`** re-verifies in real time that the surviving destination twin still exists and is byte-for-byte identical before moving the redundant source file into `<Source>/.Duplicates_Trash/`.
  - **1-Click Undo / Restore**: At any time, click **`♻️ Restore Files to Original Folder`** in the Duplicate Cleaner panel to move isolated files from `.Duplicates_Trash` back to their exact original source folders (refusing to overwrite if a new file was created at that path) and automatically clean up empty trash directories.
- **✅ Integrity Verification Checker & Auto-Repair**:
  - Audits every checkpointed file and surviving duplicate target against the destination drive, reporting `100% Verified` or pinpointing missing/corrupted files.
  - Clicking **`🔧 Repair & Re-Sync Missing Files`** safely removes only unambiguous failed artifacts (never deleting a healthy file owned by another record), clears stale checkpoint entries, and re-runs the transfer to restore any missing files.

---

## 🏗️ Architecture & Project Structure

```text
hard-drive-organizer/
├── main.py                        # Unified entrypoint (routes to Desktop GUI or CLI)
├── Launch App.command             # Double-clickable macOS launcher for Desktop GUI
├── Launch CLI.command             # Double-clickable macOS launcher for Terminal CLI
├── Makefile                       # Build, run, preview, copy, test, and clean targets
├── requirements.txt               # Python dependencies (Flask, pywebview, rich, exifread, xattr)
├── BUG_AUDIT_REPORT.md            # Full-stack 63-bug audit & remediation documentation
├── src/
│   ├── app.py                     # Flask local server, session token auth, thread-safe UIState
│   ├── api_organizer.py           # Core orchestrator, preview, parallel copy, dissolve, verify/repair, duplicate trash/restore
│   ├── cli.py                     # Rich interactive terminal interface & progress dashboard
│   ├── scanner.py                 # Multi-source directory walker, project detector, safe Google Drive zip extractor
│   ├── dates.py                   # 8-layer metadata date extractor (EXIF, embedded TIFF, QuickTime mvhd, PDF, regex, stat)
│   ├── categorizer.py             # Extension mapping, compound archive splitter, NFD-safe screenshot & date regexes
│   ├── file_ops.py                # 3-tier copy engine (APFS clonefile/copy2/raw), SQLite WAL checkpoint DB, byte-exact dedup
│   ├── utils.py                   # Rich logging helpers, size formatters, thread-safe run history persistence
│   ├── config.json                # Category extension definitions & repository project markers
│   ├── templates/
│   │   └── index.html             # Single-page Desktop UI template with automatic API token header injection
│   └── static/
│       ├── script.js              # Frontend state machine, live log filter, preview modals, post-run tools
│       └── style.css              # macOS dark-mode native styling
└── tests/
    ├── test_metadata_preservation.py  # Timestamp, permission, xattr & historical sorting tests
    ├── test_data_safety.py            # Deduplication, repair ownership, auth, overlap & idempotency tests
    ├── test_audit_regressions.py      # Deep audit regression tests (HEIC/MOV dates, scale, XSS safety)
    └── test_full_audit_fixes.py       # 63-bug full-stack audit & 1-click duplicate restore regression suite
```

---

## 🧪 Automated Testing & Verification

Drive Organizer includes **122 deterministic unit and integration tests** using Python's standard `unittest` framework:

```bash
make test
# or directly:
.venv/bin/python3 -m unittest discover -s tests -v
```

The test suite verifies:
- **Metadata & Timestamps**: APFS native `copyfile`, `copy2`, and exFAT raw-bytes fallback preservation of `mtime`, `birthtime`, POSIX mode bits (`0o755`, `0o444`), macOS `UF_IMMUTABLE` locked files, binary plist Finder tags, and extended attributes (`xattr`).
- **Date Extraction Accuracy**: Real `exifread` extraction, corrupt/zeroed EXIF tag fallthrough, embedded TIFF parsing for `.heic`/`.cr3`/`.avif`, QuickTime `moov`/`mvhd` v0 & v1 box walking (including false-positive stream rejection), PDF UTF-16BE & incremental trailer `/CreationDate` parsing, and filename regex boundary safety.
- **Data Safety & Concurrency**: Streamed byte-for-byte duplicate verification (bypassing `filecmp` stat caches), 0-byte file preservation, concurrent worker same-size serialization (`in_flight_sizes`), NFD/NFC collision handling, 255-byte UTF-8 filename truncation, symlink preservation vs. loose symlink filtering, and Zip-Slip path traversal defense.
- **Recovery & UI Security**: Re-verified duplicate isolation into `.Duplicates_Trash`, 1-click restore to original folders, code project dissolution persistence across incremental runs, ownership-aware transfer repair, per-launch API token authentication, CSV formula sanitization, and XSS-safe DOM rendering.

See [`BUG_AUDIT_REPORT.md`](BUG_AUDIT_REPORT.md) for the complete root-cause breakdown of all 63 audited fixes.

---

## ❓ Frequently Asked Questions (FAQ)

### Q1: Will running parallel threads slow down or crash my Mac or run out of RAM?
**No, 100% safe.** The app streams small 1MB hash buffer chunks, consuming only **~15 to 20 MB of RAM total** (< 0.2% of system memory). Worker thread count is capped at 4 workers for external HDDs to prevent mechanical drive head thrashing while keeping your Mac fast and responsive.

### Q2: Is it possible two completely different files have the exact same byte size? Will one get deleted?
**No data is ever lost.** If two files have the exact same byte size, the engine shortlists candidates by fast partial hash + full SHA-256 hash and **always performs a streamed byte-for-byte comparison (`files_are_identical`)** before classifying a file as a duplicate. Furthermore, `0`-byte files (such as empty `__init__.py` or `.gitkeep` placeholders) are never deduplicated against one another. If two files share a filename in the same destination folder but have different contents, the second file is safely renamed (`filename_1.ext`).

### Q3: How are screenshots detected, and where do they go?
Screenshots are detected using NFD/NFC normalized multi-locale filename patterns (`Screenshot...`, `Screen Shot...`, `Screen_Shot...`, `Captura de pantalla...`, `Bildschirmfoto...`, `Capture d'écran...`), excluding video/RAW extensions and verifying that the image does not contain camera hardware EXIF (`Make`, `Model`, `LensModel`). Confirmed screenshots are sorted chronologically into `Media/Screenshots/YYYY/MonthName/`.

### Q4: What if a folder is mistakenly detected as an intact code project?
In the **Dry-Run Preview Dashboard**, click on the project folder card to open the **Folder Inspector** and click **`⚡ Sort This as Regular Files (Not a Code Project)`**. If you already completed the transfer, open **`🔍 Review Code Projects & Dissolve`** in the Post-Transfer Review Panel and click **`Dissolve & Re-Sort`** — its files will be sorted into their regular categories and the checkpoint database will remember not to re-copy that folder as a project on future runs.

### Q5: Can I organize multiple messy source folders at once?
**Yes.** Click **Browse** multiple times or enter comma-separated paths in Step 1 (e.g. `/Volumes/HDD1, /Volumes/HDD2, /Users/me/Desktop/MessyFolder`). Even single folders whose names legitimately contain a comma (e.g. `/Users/me/Taxes, 2024`) are automatically recognized on disk and kept intact.

### Q6: What happens if I accidentally move duplicates to `.Duplicates_Trash` and want them back?
Open **`🗑️ Clean Source Duplicates`** in the Post-Transfer Review Panel and click **`♻️ Restore Files to Original Folder`**. Every isolated file in `.Duplicates_Trash` is moved back to its exact original source path in one click.

### Q7: How does the tool make file transfers so fast on macOS?
It invokes the native macOS Darwin Kernel `copyfile` syscall via `ctypes` (`COPYFILE_ALL | COPYFILE_CLONE`). On APFS volumes, same-volume copies perform instant **Copy-on-Write cloning in ~0.001s** with zero physical data blocks duplicated.

---

## 🔍 Troubleshooting HDD Detection on macOS

If macOS does not automatically show your external HDD:

1. **Check `/Volumes` in Terminal**:
   ```bash
   ls -la /Volumes/
   ```
2. **Check Unmounted Physical Disks**:
   ```bash
   diskutil list
   ```
   Mount manually if unmounted:
   ```bash
   diskutil mount /dev/disk2s1
   ```
   Or force read-only mount if filesystem is dirty:
   ```bash
   diskutil mount readOnly /dev/disk2s1
   ```
3. **Run macOS Disk Utility First Aid**:
   - Open **Disk Utility** (`Cmd + Space` ➔ search `Disk Utility`).
   - Click **View** ➔ **Show All Devices**.
   - Select your physical external drive and click **First Aid**.
4. **Grant Full Disk Access**:
   - Go to **System Settings ➔ Privacy & Security ➔ Full Disk Access**.
   - Ensure **Terminal** and **Python** have Full Disk Access toggled ON.

---

## 🛡️ Safety & Resource Guarantees

* **Strict Read-Only Source Policy**: Your source drive is never modified or written to during organization (even in CLI mode before confirmation, or when unpacking Google Takeout archives).
* **Zero RAM Pressure**: Uses only ~15–20 MB RAM total by streaming small 64KB–1MB buffers.
* **HDD Physical Protection**: Safe worker thread limits + SQLite WAL batching keep mechanical HDD read heads cool and quiet.
* **System Sleep Prevention**: Uses `caffeinate` to keep your Mac awake during long transfers and cleanly reaps child processes when finished.
* **Spotlight Thrash Protection**: Writes `.metadata_never_index` to prevent macOS Spotlight indexers from thrashing your external drive during and after transfers.
* **Complete Metadata, Permission & Symlink Preservation**: Every copy path — native macOS `copyfile`, `shutil.copy2` fallback, and raw-bytes fallback on exFAT/FAT32 — preserves modification time, creation time, POSIX permissions, and extended attributes (Finder tags, comments, and quarantine/origin URLs). Locked (`UF_IMMUTABLE`) and read-only (`0o444`) files are handled cleanly, and internal project symlinks are preserved (or dereferenced safely on exFAT).
* **Byte-Exact Duplicate Detection & 0-Byte Safety**: A file is only ever skipped as a duplicate after a streamed byte-for-byte comparison (`files_are_identical`). Sampled hashes are used only to shortlist candidates, and `0`-byte files are never deduplicated against each other.
* **Re-Verified Duplicate Isolation & 1-Click Undo**: "Clean Source Duplicates" will not move a source file into `.Duplicates_Trash` unless the surviving twin still exists at the destination **and** is confirmed byte-identical at that moment. Anything that fails the re-check is reported back and left untouched, and any isolated file can be restored to its original path with one click (`♻️ Restore Files to Original Folder`).
* **Ownership-Aware Repair**: Repair only deletes a destination file when a single failed record claims it and no completed copy owns that path, ensuring a healthy file can never be removed on behalf of a failed one.
* **Authenticated Local API & Hardened UI**: The local Flask server mints a fresh cryptographic token (`secrets.token_hex`) per launch required on every `/api/` route, guards state transitions with thread locks, escapes all dynamic filenames against XSS, and sanitizes exported CSV cells against formula injection.
* **Off-Source Archive Staging with Zip-Slip Defense**: Google Takeout/Drive archives are extracted onto the destination (or system temp dir), never onto the source drive, inspected for path traversal (`../`) and symlink escapes before extraction, and cleaned up automatically after a successful run.
* **Free Space Pre-Flight**: A fresh run that cannot fit on the destination is refused before a single file is copied. Resumed runs and same-volume APFS transfers (where Copy-on-Write clones consume near-zero additional disk space) warn instead of blocking.
* **Nothing Skipped Silently**: Build and cache folders (`node_modules`, `.venv`, `build`, `target`, `.next`, `.gradle`, …) are excluded from copying to keep transfers fast, but are counted and listed in the preview so you can see every folder left behind.
* **Fast Idempotent Resume**: Re-running against an existing destination performs a lightweight consistency check rather than re-hashing the entire drive, and checkpoints both loose files and code repositories so completed items are skipped immediately.

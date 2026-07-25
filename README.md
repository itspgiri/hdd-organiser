# Drive Organizer 🚀

**Drive Organizer** is a high-performance, ultra-lightweight macOS utility designed to safely reorganize massive storage drives (500GB to 2TB+) into a clean, structured directory layout. Built with strict read-only source policies, content deduplication, multi-threaded parallel execution, and intact code project protection.

---

## 🌟 Key Features & Performance Stack

| Feature | Functionality | Safety & Performance Mechanism |
| :--- | :--- | :--- |
| **8-Worker Parallel ThreadPool** | Processes multiple files concurrently across CPU cores. | Safe 4–8 worker limit, consuming only **15–20 MB RAM total** with zero risk of RAM pressure or HDD thrashing. |
| **APFS Zero-Copy Fast Path** | Instantaneous file copying on macOS APFS volumes. | Bypasses stream reads using kernel `os.clonefile`, copying files in **0.001s with 0 bytes extra storage**. |
| **SQLite WAL + 64MB Cache** | ACID progress tracking (`.organizer_checkpoint.db`). | SQLite Write-Ahead Logging (WAL) with 64MB RAM index cache & buffered batch commits every 50 files. |
| **8-Layer Metadata Pipeline** | Chronological media and document date extraction. | EXIF DateTimeOriginal ➔ EXIF Digitized ➔ Image DateTime ➔ GPS UTC ➔ **MP4/MOV QuickTime `mvhd` Atom** ➔ **PDF `CreationDate` Metadata** ➔ Filename Regex ➔ macOS Birthtime. |
| **Chronological Screenshot Sorting** | Isolates screenshots into `Media/Screenshots/YYYY/MonthName/`. | Multi-OS screenshot regex matching (macOS, iOS, Windows, Android). |
| **Apple Live Photo Pairing** | Pairs `.heic` photos and `.mov` video clips together. | Pre-indexes HEIC dates using fast memory tuple keys `(dir_name, name_only)`. |
| **Intact Code Repository Protection** | Detects development repositories (Git, Node, Python, Rust). | Preserves code folders 100% intact under `Code/project_name/`. Includes Dry-Run Preview Inspector drawer. |
| **Multi-Source Folder Selection** | Select multiple messy source folders at once. | Aggregates files across multiple folders or comma-separated paths (`/Volumes/HDD1, /Volumes/HDD2`). |
| **Real-Time Progress & ETA** | Live progress bar with estimated time remaining. | Speed-sampled calculation (`45% • 1,912/4,250 • ⏳ ~02m 14s remaining`) with audio completion chime (`afplay`). |
| **System & Drive Protection** | Ultra-gentle on external HDDs and Mac system. | Filters OS junk (`.DS_Store`, `._*`), suppresses Spotlight indexing (`.metadata_never_index`), and blocks system sleep (`caffeinate`). |

---

## 📁 Destination Layout Structure

```text
Destination_Drive/
│
├── 📸 Media/                     <-- Photos, Videos & Camera RAWs (by Date)
│   ├── 2021/
│   │   └── August/
│   │       ├── vacation_01.jpg
│   │       ├── vacation_01.mov   <-- Live Photo video paired automatically
│   │       └── beach.heic
│   │
│   └── 📸 Screenshots/           <-- Master Chronological Screenshots Directory
│       ├── 2022/
│       │   └── January/
│       │       └── Screen Shot 2022-01-15.png
│       └── 2023/
│           └── May/
│               └── Screenshot_20230512.png
│
├── 📄 Documents/                 <-- Workplace & Personal Documents (Dated via PDF Metadata)
│   ├── PDF/                      <-- .pdf files
│   ├── Word/                     <-- .docx, .doc, .pages
│   ├── Spreadsheets/             <-- .csv, .xlsx, .numbers
│   ├── Presentations/            <-- .pptx, .ppt, .key
│   └── Text/                     <-- .txt, .rtf, .md
│
├── 💻 Code/                      <-- Development Repositories & Snippets
│   ├── my-react-app/             <-- Preserved 100% INTACT
│   ├── python-automation/        <-- Preserved 100% INTACT
│   └── Snippets/                 <-- Loose single script files (.py, .js)
│
├── 🎨 Creative/                  <-- Design Files & Audio
│   ├── Projects/                 <-- .psd, .ai, .fig, .sketch
│   └── Audio/                    <-- .mp3, .wav, .flac
│
├── 📦 Archives/                  <-- .zip, .rar, .tar.gz, .7z
│
└── ❓ Unsorted/                  <-- Unknown formats or missing dates
                                      (Tagged with Yellow macOS Finder Tag "To Review")
```

---

## 🚀 Quick Start & How to Run

### Option 1: Desktop Web Interface (GUI Mode)

Double-click **`Launch App.command`** or run in terminal:
```bash
python3 main.py
```

#### 4-Step GUI Workflow:
1. **Select Messy Source Folder(s)**: Choose one or multiple folders (separate multiple paths with commas).
2. **Choose Destination Strategy**:
   - **✨ Option A: Brand New Sorted Folder** (Organize into a fresh folder).
   - **📂 Option B: Merge into Already Sorted Folder** (Append without duplicates).
3. **Select Destination Folder** & Toggle **`[✓] Run in Dry-Run / Preview Mode first`**.
4. **Preview Confirmation & Transfer**: Review file counts, total size, free space, and inspect detected code projects. Click **`🚀 YES, Execute Full Transfer!`**.

---

### Option 2: Command Line Interface (CLI Mode)

Double-click **`Launch CLI.command`** or run in terminal:

#### 1. Dry Run / Preview Mode (100% Safe, No Files Moved)
```bash
python3 main.py --cli /Volumes/SourceDrive /Volumes/DestinationDrive --preview
```

#### 2. Full Copy Execution
```bash
python3 main.py --cli /Volumes/SourceDrive /Volumes/DestinationDrive --copy
```

---

## 🛠️ Post-Organization Review & Utilities

After organization completes:
- **📂 Open Destination Folder in Finder**: 1-click button opens your organized HDD directory directly in macOS Finder.
- **📊 Export CSV Audit Log**: Download a full spreadsheet report detailing every file's original path, new path, file size, timestamp, and duplicate status.
- **🔍 Review Code Projects & Dissolve**: View all project folders under `Code/`. Click **`Dissolve & Re-Sort`** to break open any folder mistakenly identified as a code project and categorize its inner files.
- **🗑️ Clean Source Duplicates**: View skipped duplicate files on your source drive and click **`Move Duplicates to Trash`** to reclaim space on your original HDD.

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

* **Strict Read-Only Source Policy**: Your source drive is never modified or written to during organization.
* **Zero RAM Pressure**: Uses only ~15–20 MB RAM total by streaming small 1MB hash buffers.
* **HDD Physical Protection**: Safe worker thread limits + SQLite WAL batching keep mechanical HDD read heads cool and quiet.
* **System Sleep Prevention**: Uses `caffeinate` to keep your Mac awake during long transfers.
* **Spotlight Thrash Protection**: Writes `.metadata_never_index` to prevent Spotlight indexers from thrashing your HDD.

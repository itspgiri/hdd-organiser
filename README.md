# Drive Organizer 🚀

**Drive Organizer** is a high-performance macOS utility designed to safely reorganize massive storage drives (500GB to 2TB+) into a clean, structured directory layout. Built with strict read-only source policies, content deduplication, and project protection.

---

## 🌟 Key Features

| Feature | Functionality | Safety & Performance Mechanism |
| :--- | :--- | :--- |
| **Smart Content Deduplication** | Identifies exact duplicate files across your drive regardless of filename. | Calculates head/tail SHA-256 part-hashes stored in an ACID SQLite index (`.organizer_checkpoint.db`), skipping duplicate copies and saving GBs/TBs of storage. |
| **Media Chronological Sorting** | Sorts photos, videos, and RAW camera files into `Media/YYYY/MonthName/`. | 6-layer priority fallback pipeline (EXIF DateTimeOriginal ➔ EXIF Digitized ➔ Filename Regex ➔ Live Photo HEIC Glue ➔ macOS Creation Birthtime ➔ Modification Time). |
| **Apple Live Photo Pairing** | Pairs `.heic` photos and `.mov` video clips together. | Pre-indexes HEIC dates so paired Live Photo MOV clips land in the exact same Year/Month folder. |
| **Intact Code Protection** | Detects development repositories (Git, Node.js, Python, Rust, Go). | Moves the entire code project directory intact into `Code/project_name/` to preserve dependencies, git history, and imports. |
| **Post-Organization Review & Dissolve** | Interactive 1-click folder dissolve tool. | Dissolves any mistakenly identified code project and automatically categorizes its inner files into Media/Documents. |
| **Source Duplicate Cleaner** | Safely cleans duplicate files from your source drive. | Moves skipped duplicate source files directly to macOS Trash (`~/.Trash`) with 1 click. |
| **Same-Volume Speed** | Instant zero-space copies when reorganizing on the same disk. | Utilizes macOS APFS `clonefile` wrappers via `shutil.copy2`. |
| **System Protection** | Keeps destination drives 100% clean of junk files. | Filters OS hidden files (`.DS_Store`, `._*` AppleDouble, `Thumbs.db`, `.Spotlight-V100`, `.fseventsd`). Suppresses Spotlight indexing (`.metadata_never_index`) and blocks system sleep (`caffeinate`). |

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
│   └── 2023/
│       └── December/
│           ├── holiday.png
│           └── camera_raw.cr2
│
├── 📄 Documents/                 <-- Workplace & Personal Documents
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

### Option 1: Web Interface (GUI Mode)

Double-click **`Launch App.command`** or run in terminal:
```bash
python3 main.py
```
This launches the native macOS desktop app.

#### 4-Step GUI Workflow:
1. **Select Source Folder**: Choose the drive or messy directory to sort.
2. **Choose Destination Strategy**:
   - **✨ Option A: Brand New Folder** (Organize into a fresh folder).
   - **📂 Option B: Merge into Already Sorted Folder** (Append without duplicates).
3. **Select Destination Folder** & Toggle **`[✓] Run in Dry-Run / Preview Mode first`**.
4. **Preview Confirmation & Transfer**: Review file counts, total size, and free space. Click **`🚀 YES, Execute Full Transfer!`**.

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

## 🛠️ Post-Organization Review Tools

After organization completes, open **Post-Organization Review & Tools**:
- **🔍 Review Code Projects & Dissolve**: View all project folders under `Code/`. Click **`Dissolve & Re-Sort`** to break open any folder that was mistakenly detected as a code project and categorize its inner files.
- **🗑️ Clean Source Duplicates**: View all skipped duplicate source files and click **`Move Duplicates to Trash`** to reclaim space on your original HDD.

---

## 🛡️ Safety Guarantees

* **Read-Only Source**: Your source HDD is never modified or written to during organization.
* **ACID SQLite Checkpointing**: Commits progress to `.organizer_checkpoint.db` so transfers can safely resume if interrupted.
* **System Sleep Prevention**: Uses `caffeinate` to prevent Mac sleep mid-transfer.
* **Spotlight Indexing Suppression**: Writes `.metadata_never_index` to prevent Spotlight indexer thrashing.

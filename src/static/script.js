let currentDestMode = 'new';
let excludedProjects = [];
let currentInspectedPath = "";
let viewHistory = ['guide-view'];
let pollInterval = null;
let lastWasPreview = false;
let lastPreviewSummary = null;
let allLogs = [];

function startPolling() {
    if (pollInterval) clearInterval(pollInterval);

    pollInterval = setInterval(async () => {
        try {
            const res = await fetch('/api/status');
            const data = await res.json();

            updateProgressUI(data);

            if (data.status === 'complete' || data.status === 'error' || data.status === 'cancelled') {
                clearInterval(pollInterval);
                pollInterval = null;

                const heading = document.getElementById('status-heading');
                const doneBtn = document.getElementById('done-btn');
                const startBtn = document.getElementById('start-btn');
                const cancelBtn = document.getElementById('cancel-btn');
                if (cancelBtn) {
                    cancelBtn.disabled = false;
                    cancelBtn.innerText = "⛔ Cancel Operation";
                }
                if (startBtn) {
                    startBtn.disabled = false;
                    startBtn.innerText = "Start Organizing";
                }
                if (doneBtn) doneBtn.disabled = false;

                if (data.status === 'complete') {
                    if (heading) heading.innerText = lastWasPreview ? "Preview Complete!" : "Organization Complete!";

                    if (lastWasPreview) {
                        lastPreviewSummary = data.preview_summary;
                        document.getElementById('confirm-box').classList.remove('hidden');
                        renderPreviewDashboard(data.preview_summary);
                    } else {
                        document.getElementById('review-panel').classList.remove('hidden');
                    }
                    try { loadHistoryView(false); } catch (e) {}
                } else {
                    if (heading) heading.innerText = "Operation Cancelled / Error";
                }
            }

        } catch (e) {
            console.error("Error polling status", e);
        }
    }, 500);
}

function selectDestMode(mode) {
    currentDestMode = mode;
    document.querySelectorAll('.option-card').forEach(card => card.classList.remove('active'));
    document.getElementById(`card-${mode}`).classList.add('active');
    
    const radio = document.querySelector(`input[name="dest-mode"][value="${mode}"]`);
    if (radio) radio.checked = true;
}


function showView(viewId) {
    if (viewHistory[viewHistory.length - 1] !== viewId) {
        viewHistory.push(viewId);
    }
    document.querySelectorAll('.view').forEach(v => v.classList.remove('active'));
    document.getElementById(viewId).classList.add('active');

    // Update back button & breadcrumbs
    const backBtn = document.getElementById('nav-back-btn');
    backBtn.style.display = viewHistory.length > 1 ? 'inline-block' : 'none';

    document.querySelectorAll('.crumb').forEach(c => c.classList.remove('active'));
    if (viewId === 'guide-view') document.getElementById('crumb-guide').classList.add('active');
    if (viewId === 'setup-view') document.getElementById('crumb-setup').classList.add('active');
    if (viewId === 'progress-view') document.getElementById('crumb-progress').classList.add('active');
    if (viewId === 'history-view') {
        const crumbHist = document.getElementById('crumb-history');
        if (crumbHist) crumbHist.classList.add('active');
    }
}

function navigateBack() {
    if (viewHistory.length > 1) {
        viewHistory.pop();
        const prevView = viewHistory[viewHistory.length - 1];
        document.querySelectorAll('.view').forEach(v => v.classList.remove('active'));
        document.getElementById(prevView).classList.add('active');

        const backBtn = document.getElementById('nav-back-btn');
        backBtn.style.display = viewHistory.length > 1 ? 'inline-block' : 'none';

        document.querySelectorAll('.crumb').forEach(c => c.classList.remove('active'));
        if (prevView === 'guide-view') document.getElementById('crumb-guide').classList.add('active');
        if (prevView === 'setup-view') document.getElementById('crumb-setup').classList.add('active');
        if (prevView === 'progress-view') document.getElementById('crumb-progress').classList.add('active');
        if (prevView === 'history-view') {
            const crumbHist = document.getElementById('crumb-history');
            if (crumbHist) crumbHist.classList.add('active');
        }
    }
}


async function selectFolder(type) {
    const promptText = type === 'source' 
        ? "Select a messy SOURCE folder to organize:" 
        : "Select your DESTINATION folder (where organized files will go):";
        
    try {
        const response = await fetch(`/api/select_folder?prompt=${encodeURIComponent(promptText)}`);
        const data = await response.json();
        if (data.folder) {
            const input = document.getElementById(`${type}-path`);
            if (type === 'source' && input.value.trim().length > 0) {
                if (!input.value.includes(data.folder)) {
                    input.value = input.value + ", " + data.folder;
                }
            } else {
                input.value = data.folder;
            }
        }
    } catch (e) {
        console.error("Error selecting folder", e);
    }
}

async function startOrganizing() {
    const source = document.getElementById('source-path').value;
    const dest = document.getElementById('dest-path').value;
    const isPreview = document.getElementById('preview-mode').checked;
    
    if (!source || !dest) {
        alert("Please select both source and destination folders.");
        return;
    }
    
    if (source === dest || dest.startsWith(source)) {
        alert("Safety Error: Destination folder cannot be inside the Source folder!");
        return;
    }

    const btn = document.getElementById('start-btn');
    btn.disabled = true;
    btn.innerText = "Starting...";

    try {
        const response = await fetch('/api/start', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ 
                source: source, 
                dest: dest,
                is_preview: isPreview,
                dest_mode: currentDestMode,
                excluded_projects: excludedProjects
            })
        });
        const data = await response.json();
        
        if (data.success) {
            lastWasPreview = isPreview;
            document.getElementById('confirm-box').classList.add('hidden');
            document.getElementById('review-panel').classList.add('hidden');
            
            const fill = document.getElementById('progress-fill');
            const percentTxt = document.getElementById('progress-percentage');
            const countTxt = document.getElementById('progress-count');
            const heading = document.getElementById('status-heading');
            if (fill) fill.style.width = "0%";
            if (percentTxt) percentTxt.innerText = "0%";
            if (countTxt) countTxt.innerText = "0 / 0";
            if (heading) {
                heading.innerText = isPreview ? "🔍 Scanning Files (Preview)..." : "⚡ Organizing & Transferring Files...";
            }

            showView('progress-view');
            startPolling();
        } else {

            alert("Error starting: " + data.error);
            btn.disabled = false;
            btn.innerText = "Start Organizing";
        }
    } catch (e) {
        console.error("Error starting", e);
        btn.disabled = false;
        btn.innerText = "Start Organizing";
    }
}

function renderPreviewDashboard(summary) {
    const dash = document.getElementById('preview-dashboard');
    if (!summary || !summary.total_files) {
        dash.innerHTML = "<p class='confirm-desc'>Preview Complete! Ready to transfer files.</p>";
        return;
    }
    
    let catsHtml = "";
    if (summary.categories) {
        for (const [cat, count] of Object.entries(summary.categories)) {
            catsHtml += `<button class="category-pill-btn" onclick="inspectCategoryFiles('${cat}')">📂 <strong>${cat}</strong>: ${count} <span style="opacity:0.7; font-size:9px;">(Click to preview)</span></button>`;
        }
    }

    let projsHtml = "";
    if (summary.project_details && summary.project_details.length > 0) {
        projsHtml = `
        <div style="margin-top: 12px; border-top: 1px solid var(--border-color); padding-top: 10px;">
            <div style="display: flex; justify-content: space-between; align-items: center; margin-bottom: 6px; flex-wrap: wrap; gap: 4px;">
                <strong style="font-size: 12px;">💻 Intact Code Repositories (${summary.total_projects}):</strong>
                <div style="display: flex; gap: 4px;">
                    <button class="btn secondary" style="font-size: 10px; padding: 2px 6px;" onclick="selectAllProjects(true)">☑ Select All</button>
                    <button class="btn secondary" style="font-size: 10px; padding: 2px 6px;" onclick="selectAllProjects(false)">☐ Deselect All</button>
                </div>
            </div>
            <input type="text" id="project-search-filter" placeholder="🔍 Search code projects..." onkeyup="filterProjectsList()" style="font-size: 11px; padding: 4px 8px; margin-bottom: 6px; width: 100%; border-radius: 4px;">
            <div id="projects-checkbox-container" style="max-height: 130px; overflow-y: auto; background: var(--secondary-bg); padding: 8px; border-radius: 6px; border: 1px solid var(--border-color);">`;

        summary.project_details.forEach(p => {
            const isExcluded = excludedProjects.includes(p.path);
            const checkedAttr = isExcluded ? '' : 'checked';
            const textStyle = isExcluded ? 'text-decoration: line-through; opacity: 0.6;' : '';
            const statusLabel = isExcluded 
                ? '<span style="color: #e74c3c; font-size: 10px;">⚡ (Sort as Regular Files)</span>' 
                : '<span style="color: #2ecc71; font-size: 10px;">✓ (Keep Intact under Code/)</span>';
            projsHtml += `
            <div class="project-row-item" style="margin-bottom: 4px; font-size: 11px;">
                <label style="cursor: pointer; display: flex; align-items: center; gap: 6px;">
                    <input type="checkbox" ${checkedAttr} onchange="onProjectCheckboxChange('${p.path.replace(/\\/g, '\\\\')}', this.checked)">
                    <span style="${textStyle}">📂 <strong>${p.name}</strong> ${statusLabel}</span>
                    <button class="btn secondary" style="font-size: 9px; padding: 1px 4px; margin-left: auto;" onclick="inspectProject('${p.path.replace(/\\/g, '\\\\')}', '${p.name}')">🔍 Inspect</button>
                </label>
            </div>`;
        });
        projsHtml += `</div></div>`;
    }

    let gdriveHtml = "";
    if (summary.gdrive_zips_extracted && summary.gdrive_zips_extracted > 0) {
        const namesList = summary.gdrive_zip_names ? summary.gdrive_zip_names.join(', ') : '';
        gdriveHtml = `
        <div style="font-size: 11px; margin-top: 8px; padding: 6px 10px; background: rgba(10, 132, 255, 0.1); border-radius: 6px; border: 1px solid var(--primary-color);">
            📦 <strong>Auto-Unzipped ${summary.gdrive_zips_extracted} Google Drive Archive(s):</strong> ${namesList}
            <div style="opacity: 0.85; font-size: 10px; margin-top: 2px;">✓ All inner photos, videos, documents & files extracted & sorted into destination categories!</div>
        </div>`;
    }

    let garbageHtml = "";
    if (summary.ignored_garbage && summary.ignored_garbage > 0) {
        garbageHtml = `
        <div style="font-size: 11px; margin-top: 8px; padding: 6px 10px; background: rgba(255,255,255,0.05); border-radius: 6px; display: flex; justify-content: space-between; align-items: center; border: 1px solid var(--border-color);">
            <span>🛡️ Safely Ignored <strong>${summary.ignored_garbage.toLocaleString()}</strong> System/Garbage Files</span>
            <button class="btn secondary" style="font-size: 10px; padding: 2px 8px;" onclick="inspectGarbageFiles()">
                🔍 View Breakdown Report
            </button>
        </div>`;
    }

    dash.innerHTML = `
        <div class="stat-grid">
            <div class="stat-card">
                <div class="stat-val">${summary.total_files}</div>
                <div class="stat-lbl">Files Found</div>
            </div>
            <div class="stat-card">
                <div class="stat-val">${summary.total_size}</div>
                <div class="stat-lbl">Total Size</div>
            </div>
            <div class="stat-card">
                <div class="stat-val">${summary.free_space}</div>
                <div class="stat-lbl">Free HDD Space</div>
            </div>
        </div>
        ${gdriveHtml}
        <div style="font-size: 11px; font-weight: bold; margin-top: 8px; margin-bottom: 4px;">📊 File Categories (Click any category to preview files):</div>
        <div class="category-pills">
            ${catsHtml}
        </div>
        ${projsHtml}
        ${garbageHtml}
        <div style="font-size: 11px; opacity: 0.7; margin-top: 10px; color: var(--success-color);">
            ✓ <strong>Safe Read-Only Preview:</strong> 0 files moved. Ready to organize.
        </div>
    `;
}


function inspectCategoryFiles(category) {
    const modal = document.getElementById('category-modal');
    const title = document.getElementById('category-modal-title');
    const sub = document.getElementById('category-modal-subtitle');
    const list = document.getElementById('category-files-list');

    if (!lastPreviewSummary) return;

    title.innerText = `📂 ${category} Category File Preview`;
    const totalCount = (lastPreviewSummary.categories && lastPreviewSummary.categories[category]) || 0;
    sub.innerText = `Sample files that will be organized into ${category} (Total: ${totalCount}):`;

    const samples = (lastPreviewSummary.category_samples && lastPreviewSummary.category_samples[category]) || [];
    if (samples.length === 0) {
        list.innerHTML = "<div style='opacity:0.7;'>No sample paths available for this category.</div>";
    } else {
        list.innerHTML = samples.map(f => `<div>📄 ${f}</div>`).join('');
    }

    modal.classList.remove('hidden');
}

function closeCategoryModal() {
    document.getElementById('category-modal').classList.add('hidden');
}

function onProjectCheckboxChange(path, isChecked) {
    const idx = excludedProjects.indexOf(path);
    if (isChecked && idx >= 0) {
        excludedProjects.splice(idx, 1); // Keep intact
    } else if (!isChecked && idx < 0) {
        excludedProjects.push(path); // Exclude -> sort as regular files
    }
    if (lastPreviewSummary) renderPreviewDashboard(lastPreviewSummary);
}

function selectAllProjects(keepIntact) {
    if (!lastPreviewSummary || !lastPreviewSummary.project_details) return;
    if (keepIntact) {
        excludedProjects = [];
    } else {
        excludedProjects = lastPreviewSummary.project_details.map(p => p.path);
    }
    renderPreviewDashboard(lastPreviewSummary);
}

function filterProjectsList() {
    const q = (document.getElementById('project-search-filter') ? document.getElementById('project-search-filter').value : "").toLowerCase();
    document.querySelectorAll('.project-row-item').forEach(row => {
        const txt = row.innerText.toLowerCase();
        row.style.display = txt.includes(q) ? "block" : "none";
    });
}


function inspectGarbageFiles() {
    const modal = document.getElementById('garbage-modal');
    const bList = document.getElementById('garbage-breakdown-list');
    const sList = document.getElementById('garbage-samples-list');

    if (!lastPreviewSummary) return;

    let bHtml = "<strong>System Garbage Breakdown:</strong><br>";
    if (lastPreviewSummary.garbage_breakdown) {
        for (const [gType, gCount] of Object.entries(lastPreviewSummary.garbage_breakdown)) {
            bHtml += `<div style="padding: 2px 0;">• <strong>${gType}:</strong> ${gCount.toLocaleString()} files</div>`;
        }
    }
    bList.innerHTML = bHtml;

    let sHtml = "";
    if (lastPreviewSummary.garbage_samples && lastPreviewSummary.garbage_samples.length > 0) {
        sHtml = lastPreviewSummary.garbage_samples.map(p => `<div>📄 ${p}</div>`).join('');
    } else {
        sHtml = "<div>No sample paths available.</div>";
    }
    sList.innerHTML = sHtml;

    modal.classList.remove('hidden');
}

function closeGarbageModal() {
    document.getElementById('garbage-modal').classList.add('hidden');
}


function updateProgressUI(data) {
    const fill = document.getElementById('progress-fill');
    const percentTxt = document.getElementById('progress-percentage');
    const countTxt = document.getElementById('progress-count');
    const msgTxt = document.getElementById('current-message');
    const heading = document.getElementById('status-heading');
    
    if (data.status === 'running' && heading) {
        if (data.message && data.message.includes("Scanning")) {
            heading.innerText = lastWasPreview ? "🔍 Scanning Files (Preview)..." : "🔍 Scanning Files...";
        } else {
            heading.innerText = lastWasPreview ? "🔍 Analyzing Preview..." : "⚡ Copying & Organizing Files...";
        }
    }

    const percent = data.total > 0 ? Math.min(100, Math.round((data.progress / data.total) * 100)) : 0;
    
    if (fill) fill.style.width = `${percent}%`;
    if (percentTxt) percentTxt.innerText = `${percent}%`;
    const etaText = data.eta ? ` • ${data.eta}` : '';
    if (countTxt) countTxt.innerText = `${data.progress.toLocaleString()} / ${data.total.toLocaleString()}${etaText}`;
    if (msgTxt && data.message) msgTxt.innerText = data.message;
    
    allLogs = data.logs || [];
    filterLogs();
}


function filterLogs() {
    const query = (document.getElementById('log-filter') ? document.getElementById('log-filter').value : "").toLowerCase();
    const logContent = document.getElementById('log-content');
    if (!logContent) return;
    const filtered = query ? allLogs.filter(l => l.toLowerCase().includes(query)) : allLogs;
    logContent.innerHTML = filtered.map(log => `<div>${log}</div>`).join('');
    const logContainer = document.getElementById('log-container');
    if (logContainer) logContainer.scrollTop = logContainer.scrollHeight;
}

async function inspectProject(path, name) {
    currentInspectedPath = path;
    const modal = document.getElementById('inspect-modal');
    const title = document.getElementById('inspect-folder-title');
    const list = document.getElementById('inspect-files-list');
    const toggleBtn = document.getElementById('toggle-exclude-btn');

    title.innerText = `Inspect Folder: ${name}`;
    list.innerHTML = "<div>Loading files...</div>";
    modal.classList.remove('hidden');

    const isExcluded = excludedProjects.includes(path);
    toggleBtn.innerText = isExcluded 
        ? "✓ Re-Enable Code Project Protection" 
        : "⚡ Sort This as Regular Files (Not a Code Project)";

    try {
        const response = await fetch(`/api/inspect_folder?path=${encodeURIComponent(path)}`);
        const data = await response.json();
        
        if (!data.files || data.files.length === 0) {
            list.innerHTML = "<div style='opacity: 0.7;'>No sample files found.</div>";
            return;
        }

        list.innerHTML = data.files.map(f => `<div>📄 ${f}</div>`).join('');
    } catch (e) {
        list.innerHTML = "<div>Error loading folder files</div>";
    }
}

function closeInspectModal() {
    document.getElementById('inspect-modal').classList.add('hidden');
}

function toggleExcludeCurrentProject() {
    if (!currentInspectedPath) return;
    const idx = excludedProjects.indexOf(currentInspectedPath);
    if (idx >= 0) {
        excludedProjects.splice(idx, 1);
    } else {
        excludedProjects.push(currentInspectedPath);
    }
    closeInspectModal();
    if (lastPreviewSummary) {
        renderPreviewDashboard(lastPreviewSummary);
    }
}

async function executeFullCopy() {
    lastWasPreview = false;
    document.getElementById('preview-mode').checked = false;
    document.getElementById('confirm-box').classList.add('hidden');
    document.getElementById('default-actions').style.display = "block";
    const heading = document.getElementById('status-heading');
    if (heading) heading.innerText = "🔍 Scanning Files...";
    await startOrganizing();
}


function resetToSetup() {
    document.getElementById('start-btn').disabled = false;
    document.getElementById('start-btn').innerText = "Start Organizing";
    document.getElementById('progress-fill').style.width = "0%";
    document.getElementById('progress-percentage').innerText = "0%";
    document.getElementById('progress-count').innerText = "0 / 0";
    document.getElementById('log-content').innerHTML = "";
    document.getElementById('confirm-box').classList.add('hidden');
    document.getElementById('review-panel').classList.add('hidden');
    document.getElementById('default-actions').style.display = "block";
    
    showView('setup-view');
}

async function loadProjectReview() {
    const dest = document.getElementById('dest-path').value;
    const area = document.getElementById('review-content-area');
    area.classList.remove('hidden');
    area.innerHTML = "<div>Loading Code Projects...</div>";

    try {
        const response = await fetch(`/api/projects?dest=${encodeURIComponent(dest)}`);
        const data = await response.json();
        
        if (!data.projects || data.projects.length === 0) {
            area.innerHTML = "<div style='font-size: 12px; opacity: 0.7;'>No intact code project folders found in Code/.</div>";
            return;
        }

        let html = "<strong>Discovered Code Project Folders:</strong><br><br>";
        data.projects.forEach(p => {
            html += `
            <div class="project-item">
                <div>
                    <strong>📂 ${p.name}</strong> (${p.file_count} files)
                </div>
                <button class="btn secondary" style="font-size: 11px; padding: 4px 10px;" onclick="dissolveProject('${p.path.replace(/\\/g, '\\\\')}')">
                    Dissolve & Re-Sort
                </button>
            </div>`;
        });
        area.innerHTML = html;
    } catch (e) {
        area.innerHTML = "<div>Error loading projects</div>";
    }
}

async function dissolveProject(projectPath) {
    if (!confirm("Are you sure you want to dissolve this code folder and categorize its inner files (photos -> Media, docs -> Documents)?")) {
        return;
    }
    const dest = document.getElementById('dest-path').value;
    try {
        const response = await fetch('/api/dissolve_project', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ dest: dest, project_path: projectPath })
        });
        const data = await response.json();
        alert(data.message);
        loadProjectReview();
    } catch (e) {
        alert("Error dissolving project: " + e);
    }
}

let duplicateSourcePaths = [];

async function loadDuplicateCleaner() {
    const dest = document.getElementById('dest-path').value;
    const area = document.getElementById('review-content-area');
    area.classList.remove('hidden');
    area.innerHTML = "<div>Checking for duplicate files...</div>";

    try {
        const response = await fetch(`/api/duplicates?dest=${encodeURIComponent(dest)}`);
        const data = await response.json();
        
        if (!data.duplicates || data.duplicates.length === 0) {
            area.innerHTML = "<div style='font-size: 12px; opacity: 0.7;'>✓ No duplicate source files were skipped. Source drive is clean.</div>";
            return;
        }

        duplicateSourcePaths = data.duplicates.map(d => d.source_path);
        let totalSize = data.duplicates.reduce((acc, curr) => acc + (curr.size || 0), 0);
        let sizeMB = (totalSize / (1024 * 1024)).toFixed(2);

        let html = `
        <div style="margin-bottom: 10px;">
            <strong>Found ${data.duplicates.length} Duplicate Files (${sizeMB} MB) on Source Drive</strong>
            <button class="btn primary" style="font-size: 11px; padding: 6px 12px; float: right;" onclick="trashAllDuplicates()">
                🗑️ Move ${data.duplicates.length} Duplicates to Trash
            </button>
        </div>
        <div style="font-size: 11px; max-height: 120px; overflow-y: auto;">`;

        data.duplicates.forEach(d => {
            html += `<div style="white-space: nowrap; overflow: hidden; text-overflow: ellipsis; padding: 2px 0;">• ${d.source_path}</div>`;
        });
        html += `</div>`;
        area.innerHTML = html;
    } catch (e) {
        area.innerHTML = "<div>Error loading duplicates</div>";
    }
}

async function trashAllDuplicates() {
    if (!confirm(`Are you sure you want to isolate ${duplicateSourcePaths.length} duplicate files on your SOURCE drive into a .Duplicates_Trash folder?`)) {
        return;
    }
    try {
        const response = await fetch('/api/trash_duplicates', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ source_paths: duplicateSourcePaths })
        });
        const data = await response.json();
        alert(`Successfully moved ${data.count} duplicate files into .Duplicates_Trash with 0 Touch ID prompts!`);
        loadDuplicateCleaner();
    } catch (e) {
        alert("Error moving duplicate files: " + e);
    }
}


async function openDestinationInFinder() {
    const dest = document.getElementById('dest-path').value;
    if (!dest) return;
    try {
        await fetch(`/api/open_finder?path=${encodeURIComponent(dest)}`);
    } catch (e) {
        console.error("Error opening Finder", e);
    }
}

function downloadAuditReport() {
    const dest = document.getElementById('dest-path').value;
    if (!dest) return;
    window.location.href = `/api/export_csv?dest=${encodeURIComponent(dest)}`;
}

async function runVerificationChecker() {
    const dest = document.getElementById('dest-path').value;
    const area = document.getElementById('review-content-area');
    if (!dest) return;
    area.classList.remove('hidden');
    area.innerHTML = "<div>Running 100% SHA-256 Hash & File Size Integrity Verification Check...</div>";

    try {
        const response = await fetch(`/api/verify_transfer?dest=${encodeURIComponent(dest)}`);
        const data = await response.json();
        
        if (!data.success) {
            area.innerHTML = `<div style="color: var(--danger-color); font-size: 12px;">⚠️ ${data.error}</div>`;
            return;
        }

        if (data.is_perfect) {
            area.innerHTML = `
            <div style="background: rgba(46, 204, 113, 0.15); border: 1px solid #2ecc71; padding: 12px; border-radius: 8px;">
                <div style="color: #2ecc71; font-weight: bold; font-size: 14px;">🟢 100% Integrity Verified & Safe to Delete!</div>
                <div style="font-size: 12px; margin-top: 6px;">
                    • Total Files Checked: <strong>${data.total_files}</strong><br>
                    • Successfully Verified Copied Files: <strong>${data.verified_count}</strong><br>
                    • Skipped Duplicates (Intact at destination): <strong>${data.skipped_duplicates}</strong><br>
                    • Missing Files: <strong>0</strong><br>
                    • Corrupted / Mismatched Files: <strong>0</strong>
                </div>
                <div style="font-size: 11px; margin-top: 8px; opacity: 0.8;">
                    ✓ All files exist at destination with exact byte size & hash match. It is now 100% safe to delete your original source folder!
                </div>
            </div>`;
        } else {
            let errorHtml = "";
            if (data.missing_count > 0) {
                errorHtml += `<div><strong>Missing Files (${data.missing_count}):</strong> ${data.missing_list.join(', ')}</div>`;
            }
            if (data.mismatched_count > 0) {
                errorHtml += `<div><strong>Mismatched Files (${data.mismatched_count}):</strong> ${data.mismatched_list.join(', ')}</div>`;
            }
            area.innerHTML = `
            <div style="background: rgba(231, 76, 60, 0.15); border: 1px solid #e74c3c; padding: 12px; border-radius: 8px;">
                <div style="color: #e74c3c; font-weight: bold; font-size: 14px;">⚠️ Verification Warning: Mismatches Found</div>
                <div style="font-size: 12px; margin-top: 6px;">
                    • Total Files Checked: <strong>${data.total_files}</strong><br>
                    • Verified Files: <strong>${data.verified_count}</strong><br>
                    • Missing Files: <strong>${data.missing_count}</strong><br>
                    • Corrupted / Mismatched Files: <strong>${data.mismatched_count}</strong>
                </div>
                <div style="font-size: 11px; margin-top: 8px;">
                    ${errorHtml}
                </div>
            </div>`;
        }
    } catch (e) {
        area.innerHTML = "<div>Error running verification check</div>";
    }
}

async function loadHistoryView(shouldNavigate = true) {
    if (shouldNavigate) showView('history-view');
    const container = document.getElementById('history-list-container');
    if (!container) return;
    if (shouldNavigate) container.innerHTML = "<div>Loading past runs...</div>";


    try {
        const response = await fetch('/api/history');
        const data = await response.json();
        const runs = data.history || [];

        if (runs.length === 0) {
            container.innerHTML = "<div style='font-size: 13px; opacity: 0.7; padding: 20px; text-align: center;'>No past organization runs recorded yet. Run your first organization to generate audit history!</div>";
            return;
        }

        let html = "";
        runs.forEach(run => {
            const isCompleted = run.status === 'Completed';
            const badgeColor = isCompleted ? '#2ecc71' : '#f39c12';
            html += `
            <div style="background: rgba(255, 255, 255, 0.05); border: 1px solid rgba(255, 255, 255, 0.1); border-radius: 8px; padding: 12px; margin-bottom: 12px;">
                <div style="display: flex; justify-content: space-between; align-items: center; margin-bottom: 6px;">
                    <strong style="font-size: 13px;">🕒 ${run.timestamp}</strong>
                    <span style="font-size: 10px; background: ${badgeColor}; color: #fff; padding: 2px 8px; border-radius: 10px; font-weight: bold;">${run.status}</span>
                </div>
                <div style="font-size: 11px; opacity: 0.9; margin-bottom: 4px;">
                    📂 <strong>Source:</strong> ${run.source}<br>
                    🎯 <strong>Destination:</strong> ${run.dest}
                </div>
                <div style="font-size: 11px; opacity: 0.7; margin-bottom: 8px;">
                    📊 <strong>Files:</strong> ${run.total_files} files (${run.total_size}) • <strong>Code Projects:</strong> ${run.projects_count || 0}
                </div>
                <div style="display: flex; gap: 8px; flex-wrap: wrap; margin-top: 8px;">
                    <button class="btn secondary" style="font-size: 10px; padding: 4px 8px;" onclick="downloadHistoryCSV('${run.dest.replace(/\\/g, '\\\\')}')">
                        📊 Download CSV Audit Log
                    </button>
                    <button class="btn secondary" style="font-size: 10px; padding: 4px 8px;" onclick="verifyHistoryRun('${run.dest.replace(/\\/g, '\\\\')}')">
                        ✅ Run Integrity Check
                    </button>
                    <button class="btn secondary" style="font-size: 10px; padding: 4px 8px;" onclick="openHistoryFinder('${run.dest.replace(/\\/g, '\\\\')}')">
                        📂 Open Destination in Finder
                    </button>
                </div>
            </div>`;
        });
        container.innerHTML = html;
    } catch (e) {
        container.innerHTML = "<div>Error loading past runs history</div>";
    }
}

function downloadHistoryCSV(destPath) {
    if (!destPath) return;
    window.location.href = `/api/export_csv?dest=${encodeURIComponent(destPath)}`;
}

async function verifyHistoryRun(destPath) {
    if (!destPath) return;
    document.getElementById('dest-path').value = destPath;
    showView('progress-view');
    document.getElementById('review-panel').classList.remove('hidden');
    await runVerificationChecker();
}

async function openHistoryFinder(destPath) {
    if (!destPath) return;
    try {
        await fetch(`/api/open_finder?path=${encodeURIComponent(destPath)}`);
    } catch (e) {}
}

async function clearHistoryLog() {
    if (!confirm("Are you sure you want to clear all past run history?")) return;
    try {
        await fetch('/api/clear_history', { method: 'POST' });
        loadHistoryView();
    } catch (e) {
        alert("Error clearing history");
    }
}

async function cancelCurrentOperation() {
    if (!confirm("Are you sure you want to cancel the organization process midway? Progress completed so far is saved safely in the checkpoint database.")) {
        return;
    }
    const cancelBtn = document.getElementById('cancel-btn');
    if (cancelBtn) {
        cancelBtn.disabled = true;
        cancelBtn.innerText = "Cancelling...";
    }
    try {
        await fetch('/api/cancel', { method: 'POST' });
        const heading = document.getElementById('status-heading');
        if (heading) heading.innerText = "Operation Cancelled";
        const doneBtn = document.getElementById('done-btn');
        if (doneBtn) doneBtn.disabled = false;
        const startBtn = document.getElementById('start-btn');
        if (startBtn) {
            startBtn.disabled = false;
            startBtn.innerText = "Start Organizing";
        }
    } catch (e) {
        alert("Error cancelling operation: " + e);
    }
}




let currentDestMode = 'new';
let excludedProjects = [];
let currentInspectedPath = "";
let viewHistory = ['guide-view'];
let pollInterval = null;
let lastWasPreview = false;
let lastPreviewSummary = null;
let allLogs = [];

// Anything that comes off the user's disk -- file names, folder names, full
// paths, log lines -- is untrusted markup. macOS only forbids "/" and NUL in a
// file name, so < > & " ' are all legal and arrive verbatim from the scanner.
// Every interpolation of disk-derived data into innerHTML must go through this.
// Do NOT wrap app-generated markup in it; only the data being embedded.
function escapeHtml(value) {
    return String(value === null || value === undefined ? '' : value)
        .replace(/&/g, '&amp;')
        .replace(/</g, '&lt;')
        .replace(/>/g, '&gt;')
        .replace(/"/g, '&quot;')
        .replace(/'/g, '&#39;');
}

function startPolling() {
    if (pollInterval) clearInterval(pollInterval);

    pollInterval = setInterval(async () => {
        try {
            const res = await fetch('/api/status');
            if (!res.ok) return;
            const data = await res.json();

            // "cancelling" is deliberately NOT terminal. The worker thread is
            // still finishing the file in flight and still owns the checkpoint
            // database; app.py's run_organizer() is the only thing allowed to
            // settle on complete/error/cancelled. Keep polling until it does.
            const isTerminal = data.status === 'complete'
                || data.status === 'error'
                || data.status === 'cancelled';

            // Stop the timer BEFORE rendering. updateProgressUI() used to run
            // first, and a single bad field in the payload threw past the
            // clearInterval below, leaving the app polling forever with every
            // button stuck disabled and no way out but force-quitting.
            if (isTerminal) {
                clearInterval(pollInterval);
                pollInterval = null;
            }

            try {
                updateProgressUI(data);
            } catch (uiErr) {
                console.error("Error rendering progress", uiErr);
            }

            if (isTerminal) {
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
                } else if (data.status === 'cancelled') {
                    if (heading) heading.innerText = "Operation Cancelled";
                } else {
                    // The reason lives in state.message, which updateProgressUI
                    // has already painted into #current-message above.
                    if (heading) heading.innerText = "Operation Failed";
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
    
    // Fast pre-flight only. The authoritative check is validate_paths() in
    // api_organizer.py, which resolves symlinks with realpath/commonpath.
    //
    // The old test was `source === dest || dest.startsWith(source)` on the raw
    // strings, which was wrong in both directions: it blocked the perfectly
    // safe /Volumes/Photos -> /Volumes/Photos_Backup pair, and it missed real
    // nesting whenever the source box held the comma-separated list of folders
    // the UI explicitly invites. Split the list and compare whole path
    // segments so a sibling can never look like a child.
    const stripTrailingSlash = p => p.trim().replace(/\/+$/, '');
    const sources = source.split(',').map(stripTrailingSlash).filter(Boolean);
    const destNorm = stripTrailingSlash(dest);
    const clash = sources.find(s => s === destNorm
        || destNorm.startsWith(s + '/')
        || s.startsWith(destNorm + '/'));
    if (clash) {
        alert(`Safety Error: "${destNorm}" and "${clash}" are nested inside each other.\n\nChoose a destination outside every source folder.`);
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
        const data = await response.json().catch(() => ({}));
        
        if (response.ok && data.success) {
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

            alert("Error starting: " + (data.error || `HTTP ${response.status}`));
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

    // Build this first so it can also be shown on the "nothing to copy" path
    // below -- a source made up entirely of build/cache folders reports
    // total_files === 0, and that is exactly when the user most needs to know
    // why nothing is going to be transferred.
    let skippedHtml = "";
    const skippedBreakdown = summary && summary.skipped_dir_breakdown;
    if (skippedBreakdown && Object.keys(skippedBreakdown).length > 0) {
        const entries = Object.entries(skippedBreakdown).sort((a, b) => b[1] - a[1]);
        const totalSkipped = entries.reduce((acc, kv) => acc + kv[1], 0);
        const breakdownHtml = entries.map(([name, count]) =>
            `<div style="padding: 1px 0;">• <strong>${escapeHtml(name)}</strong>: ${Number(count).toLocaleString()} folder(s)</div>`
        ).join('');

        const skippedSamples = summary.skipped_dirs || [];
        const shown = skippedSamples.slice(0, 5);
        let samplesHtml = "";
        if (shown.length > 0) {
            samplesHtml = `<div style="margin-top: 6px; opacity: 0.85; font-size: 10px; max-height: 70px; overflow-y: auto;">`
                + shown.map(p => `<div style="white-space: nowrap; overflow: hidden; text-overflow: ellipsis;">📁 ${escapeHtml(p)}</div>`).join('')
                + (skippedSamples.length > shown.length
                    ? `<div style="opacity: 0.7;">…and ${skippedSamples.length - shown.length} more</div>`
                    : '')
                + `</div>`;
        }

        skippedHtml = `
        <div style="font-size: 11px; margin-top: 8px; padding: 8px 10px; background: rgba(243, 156, 18, 0.12); border-radius: 6px; border: 1px solid #f39c12;">
            <div style="font-weight: bold; color: #f39c12;">⏭️ Build / cache folders that will NOT be copied (${totalSkipped.toLocaleString()}):</div>
            <div style="margin-top: 4px;">${breakdownHtml}</div>
            ${samplesHtml}
            <div style="opacity: 0.85; font-size: 10px; margin-top: 4px;">
                These are skipped on purpose and will not reach the destination. If you need one of them, move it out of the source folder before transferring.
            </div>
        </div>`;
    }

    if (!summary || !summary.total_files) {
        dash.innerHTML = "<p class='confirm-desc'>Preview Complete! Ready to transfer files.</p>" + skippedHtml;
        return;
    }
    
    let catsHtml = "";
    if (summary.categories) {
        for (const [cat, count] of Object.entries(summary.categories)) {
            catsHtml += `<button class="category-pill-btn" data-category="${escapeHtml(cat)}">📂 <strong>${escapeHtml(cat)}</strong>: ${count} <span style="opacity:0.7; font-size:9px;">(Click to preview)</span></button>`;
        }
    }

    let projsHtml = "";
    if (summary.project_details && summary.project_details.length > 0) {
        projsHtml = `
        <div style="margin-top: 12px; border-top: 1px solid var(--border-color); padding-top: 10px;">
            <div style="display: flex; justify-content: space-between; align-items: center; margin-bottom: 6px; flex-wrap: wrap; gap: 4px;">
                <strong style="font-size: 12px;">💻 Intact Code Repositories (${summary.total_projects}):</strong>
                <div style="display: flex; gap: 4px;">
                    <button class="btn secondary" id="projects-select-all-btn" style="font-size: 10px; padding: 2px 6px;">☑ Select All</button>
                    <button class="btn secondary" id="projects-deselect-all-btn" style="font-size: 10px; padding: 2px 6px;">☐ Deselect All</button>
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
            // Paths travel in data-* attributes rather than inside an inline
            // onclick="fn('...')" string. The old form only doubled backslashes,
            // so a folder called "Priyanka's Portfolio" produced a SyntaxError
            // and the button silently did nothing, while a folder containing a
            // double quote could close the attribute and inject handlers.
            projsHtml += `
            <div class="project-row-item" style="margin-bottom: 4px; font-size: 11px;">
                <label style="cursor: pointer; display: flex; align-items: center; gap: 6px;">
                    <input type="checkbox" ${checkedAttr} class="project-exclude-checkbox" data-path="${escapeHtml(p.path)}">
                    <span style="${textStyle}">📂 <strong>${escapeHtml(p.name)}</strong> ${statusLabel}</span>
                    <button class="btn secondary project-inspect-btn" data-path="${escapeHtml(p.path)}" data-name="${escapeHtml(p.name)}" style="font-size: 9px; padding: 1px 4px; margin-left: auto;">🔍 Inspect</button>
                </label>
            </div>`;
        });
        projsHtml += `</div></div>`;
    }

    let gdriveHtml = "";
    if (summary.gdrive_zips_extracted && summary.gdrive_zips_extracted > 0) {
        const namesList = summary.gdrive_zip_names ? summary.gdrive_zip_names.map(escapeHtml).join(', ') : '';
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
                <div class="stat-val">${escapeHtml(summary.total_files)}</div>
                <div class="stat-lbl">Files Found</div>
            </div>
            <div class="stat-card">
                <div class="stat-val">${escapeHtml(summary.total_size)}</div>
                <div class="stat-lbl">Total Size</div>
            </div>
            <div class="stat-card">
                <div class="stat-val">${escapeHtml(summary.free_space)}</div>
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
        ${skippedHtml}
        <div style="font-size: 11px; opacity: 0.7; margin-top: 10px; color: var(--success-color);">
            ✓ <strong>Safe Read-Only Preview:</strong> 0 files moved. Ready to organize.
        </div>
    `;

    // Wire the generated controls up here: the markup above carries data only,
    // never executable strings. Re-running renderPreviewDashboard() replaces
    // the markup and re-attaches these, so the listeners stay in sync.
    dash.querySelectorAll('.category-pill-btn').forEach(btn => {
        btn.addEventListener('click', () => inspectCategoryFiles(btn.dataset.category));
    });
    dash.querySelectorAll('.project-inspect-btn').forEach(btn => {
        btn.addEventListener('click', () => inspectProject(btn.dataset.path, btn.dataset.name));
    });
    dash.querySelectorAll('.project-exclude-checkbox').forEach(box => {
        box.addEventListener('change', () => onProjectCheckboxChange(box.dataset.path, box.checked));
    });
    const selectAllBtn = document.getElementById('projects-select-all-btn');
    if (selectAllBtn) selectAllBtn.addEventListener('click', () => selectAllProjects(true));
    const deselectAllBtn = document.getElementById('projects-deselect-all-btn');
    if (deselectAllBtn) deselectAllBtn.addEventListener('click', () => selectAllProjects(false));
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
        list.innerHTML = samples.map(f => `<div>📄 ${escapeHtml(f)}</div>`).join('');
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
            bHtml += `<div style="padding: 2px 0;">• <strong>${escapeHtml(gType)}:</strong> ${gCount.toLocaleString()} files</div>`;
        }
    }
    bList.innerHTML = bHtml;

    let sHtml = "";
    if (lastPreviewSummary.garbage_samples && lastPreviewSummary.garbage_samples.length > 0) {
        sHtml = lastPreviewSummary.garbage_samples.map(p => `<div>📄 ${escapeHtml(p)}</div>`).join('');
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
    } else if (data.status === 'cancelling' && heading) {
        heading.innerText = "⛔ Cancelling — finishing the file in flight...";
    }

    // Coerce defensively: a malformed or partial payload used to throw here on
    // .toLocaleString(), which stranded the poller (see startPolling).
    const progressVal = Number(data.progress ?? 0) || 0;
    const totalVal = Number(data.total ?? 0) || 0;
    const percent = totalVal > 0 ? Math.min(100, Math.round((progressVal / totalVal) * 100)) : 0;
    
    if (fill) fill.style.width = `${percent}%`;
    if (percentTxt) percentTxt.innerText = `${percent}%`;
    const etaText = data.eta ? ` • ${data.eta}` : '';
    if (countTxt) countTxt.innerText = `${progressVal.toLocaleString()} / ${totalVal.toLocaleString()}${etaText}`;
    if (msgTxt && data.message) msgTxt.innerText = data.message;
    
    allLogs = data.logs || [];
    filterLogs();
}


function filterLogs() {
    const query = (document.getElementById('log-filter') ? document.getElementById('log-filter').value : "").toLowerCase();
    const logContent = document.getElementById('log-content');
    if (!logContent) return;
    const filtered = query ? allLogs.filter(l => l.toLowerCase().includes(query)) : allLogs;
    const logContainer = document.getElementById('log-container');

    // Only follow the tail when the user is already parked at the bottom.
    // Scrolling unconditionally on every 500ms poll made it impossible to read
    // back through warnings (skipped FAT32 / iCloud files) during a transfer.
    const nearBottom = !logContainer
        || (logContainer.scrollHeight - logContainer.scrollTop - logContainer.clientHeight) < 50;

    // Log lines embed raw file names straight from the scanner.
    logContent.innerHTML = filtered.map(log => `<div>${escapeHtml(log)}</div>`).join('');

    if (logContainer && nearBottom) logContainer.scrollTop = logContainer.scrollHeight;
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
        if (!response.ok) {
            list.innerHTML = `<div style='color: var(--danger-color);'>Could not read this folder (HTTP ${response.status}).</div>`;
            return;
        }
        const data = await response.json();
        
        if (!data.files || data.files.length === 0) {
            list.innerHTML = "<div style='opacity: 0.7;'>No sample files found.</div>";
            return;
        }

        list.innerHTML = data.files.map(f => `<div>📄 ${escapeHtml(f)}</div>`).join('');
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
    const goBtn = document.getElementById('confirm-go-btn');
    if (goBtn) goBtn.disabled = true;
    lastWasPreview = false;
    document.getElementById('preview-mode').checked = false;
    document.getElementById('confirm-box').classList.add('hidden');
    document.getElementById('default-actions').style.display = "block";
    const heading = document.getElementById('status-heading');
    if (heading) heading.innerText = "🔍 Scanning Files...";
    try {
        await startOrganizing();
    } finally {
        if (goBtn) goBtn.disabled = false;
    }
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
        // Distinguish "the request failed" from "there genuinely are none":
        // a 403/500 used to render the reassuring empty-state below.
        if (!response.ok) {
            area.innerHTML = `<div style="color: var(--danger-color); font-size: 12px;">⚠️ Could not load code projects (HTTP ${response.status}). This is not the same as "none found" — please retry.</div>`;
            return;
        }
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
                    <strong>📂 ${escapeHtml(p.name)}</strong> (${escapeHtml(p.file_count)} files)
                </div>
                <button class="btn secondary project-dissolve-btn" data-path="${escapeHtml(p.path)}" style="font-size: 11px; padding: 4px 10px;">
                    Dissolve & Re-Sort
                </button>
            </div>`;
        });
        area.innerHTML = html;
        area.querySelectorAll('.project-dissolve-btn').forEach(btn => {
            btn.addEventListener('click', () => dissolveProject(btn.dataset.path));
        });
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
        const data = await response.json().catch(() => ({}));
        if (!response.ok) {
            alert(`Error dissolving project: ${data.error || `HTTP ${response.status}`}`);
            return;
        }
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
        // A failed request must never render "Source drive is clean" -- the
        // user may delete the source on the strength of that message.
        if (!response.ok) {
            area.innerHTML = `<div style="color: var(--danger-color); font-size: 12px;">⚠️ Could not check for duplicates (HTTP ${response.status}). This is <strong>not</strong> a clean bill of health — do not delete your source drive until this check succeeds.</div>`;
            return;
        }
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
            html += `<div style="white-space: nowrap; overflow: hidden; text-overflow: ellipsis; padding: 2px 0;">• ${escapeHtml(d.source_path)}</div>`;
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
        const data = await response.json().catch(() => ({}));
        if (!response.ok) {
            alert(`Error moving duplicate files: ${data.error || `HTTP ${response.status}`}`);
            return;
        }
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

// Deliberately NOT window.location.href. /api/export_csv answers 400/404/500
// with plain text and no Content-Disposition, so the browser rendered the error
// instead of downloading it -- which replaced the whole single-page UI with a
// bare error string. Inside the pywebview window there is no back button, so
// the only way out was to quit and relaunch, losing the selected paths.
async function downloadCsvReport(dest) {
    if (!dest) return;
    const controller = new AbortController();
    try {
        // Probe first: this tells us whether the export will succeed without
        // committing the window to a navigation we cannot undo.
        const response = await fetch(
            `/api/export_csv?dest=${encodeURIComponent(dest)}&token=${encodeURIComponent(window.API_TOKEN)}`,
            { signal: controller.signal }
        );
        if (!response.ok) {
            const detail = await response.text().catch(() => "");
            alert(`Could not export the CSV audit report.\n\n${detail || `HTTP ${response.status}`}`);
            return;
        }
        // The response is good, so stop the probe before its (potentially very
        // large) body comes down the wire -- we only needed the status line.
        controller.abort();

        // Deliberately NOT a blob + <a download>. This app runs inside
        // pywebview/WKWebView, which routinely ignores "download" on a blob:
        // URL and produces no file and no error. A plain navigation to a
        // response carrying Content-Disposition: attachment is handled
        // natively. The probe above is what actually fixes the original bug:
        // previously an error response (404 "No checkpoint database found")
        // was navigated to directly, replacing the whole app with a bare error
        // page and no way back.
        window.location.href = `/api/export_csv?dest=${encodeURIComponent(dest)}&token=${encodeURIComponent(window.API_TOKEN)}`;
    } catch (e) {
        if (e && e.name === 'AbortError') return;
        alert("Error exporting CSV audit report: " + e);
    }
}

function downloadAuditReport() {
    const dest = document.getElementById('dest-path').value;
    if (!dest) return;
    downloadCsvReport(dest);
}

async function runVerificationChecker() {
    const dest = document.getElementById('dest-path').value;
    const area = document.getElementById('review-content-area');
    if (!dest) return;
    area.classList.remove('hidden');
    area.innerHTML = "<div>Running 100% SHA-256 Hash & File Size Integrity Verification Check...</div>";

    try {
        const response = await fetch(`/api/verify_transfer?dest=${encodeURIComponent(dest)}`);
        if (!response.ok) {
            area.innerHTML = `<div style="color: var(--danger-color); font-size: 12px;">⚠️ Verification could not run (HTTP ${response.status}). Your files were <strong>not</strong> verified — do not delete the source drive.</div>`;
            return;
        }
        const data = await response.json();
        
        if (!data.success) {
            area.innerHTML = `<div style="color: var(--danger-color); font-size: 12px;">⚠️ ${escapeHtml(data.error)}</div>`;
            return;
        }

        if (data.is_perfect) {
            area.innerHTML = `
            <div style="background: rgba(46, 204, 113, 0.15); border: 1px solid #2ecc71; padding: 12px; border-radius: 8px;">
                <div style="color: #2ecc71; font-weight: bold; font-size: 14px;">🟢 100% Integrity Verified & Safe to Delete!</div>
                <div style="font-size: 12px; margin-top: 6px;">
                    • Total Files Checked: <strong>${escapeHtml(data.total_files)}</strong><br>
                    • Successfully Verified Copied Files: <strong>${escapeHtml(data.verified_count)}</strong><br>
                    • Skipped Duplicates (Intact at destination): <strong>${escapeHtml(data.skipped_duplicates)}</strong><br>
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
                errorHtml += `<div><strong>Missing Files (${escapeHtml(data.missing_count)}):</strong> ${data.missing_list.map(escapeHtml).join(', ')}</div>`;
            }
            if (data.mismatched_count > 0) {
                errorHtml += `<div><strong>Mismatched Files (${escapeHtml(data.mismatched_count)}):</strong> ${data.mismatched_list.map(escapeHtml).join(', ')}</div>`;
            }
            area.innerHTML = `
            <div style="background: rgba(231, 76, 60, 0.15); border: 1px solid #e74c3c; padding: 12px; border-radius: 8px;">
                <div style="color: #e74c3c; font-weight: bold; font-size: 14px;">⚠️ Verification Warning: Mismatches Found</div>
                <div style="font-size: 12px; margin-top: 6px;">
                    • Total Files Checked: <strong>${escapeHtml(data.total_files)}</strong><br>
                    • Verified Files: <strong>${escapeHtml(data.verified_count)}</strong><br>
                    • Missing Files: <strong>${escapeHtml(data.missing_count)}</strong><br>
                    • Corrupted / Mismatched Files: <strong>${escapeHtml(data.mismatched_count)}</strong>
                </div>
                <div style="font-size: 11px; margin-top: 8px;">
                    ${errorHtml}
                </div>
                <div style="margin-top: 10px;">
                    <button class="btn secondary" id="repair-resync-btn" data-dest="${escapeHtml(dest)}" style="font-size: 11px; padding: 6px 12px;">⚡ Repair & Re-Sync Failed Files</button>
                </div>
            </div>`;
            const repairBtn = document.getElementById('repair-resync-btn');
            if (repairBtn) {
                repairBtn.addEventListener('click', () => repairAndResync(repairBtn.dataset.dest));
            }
        }
    } catch (e) {
        area.innerHTML = "<div>Error running verification check</div>";
    }
}

async function repairAndResync(destPath) {
    if (!confirm("This will clear corrupted/missing file records from the checkpoint database so they can be re-copied. Proceed?")) return;
    try {
        const response = await fetch('/api/repair_transfer', {
            method: 'POST',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({dest: destPath})
        });
        const data = await response.json().catch(() => ({}));
        if (response.ok && data.success) {
            const purged = data.repaired_count || 0;
            if (purged === 0) {
                alert("No repairable records were found, so nothing was re-queued. Your destination already matches the checkpoint database.");
                return;
            }

            // Run transfer again to re-sync.
            //
            // This used to look for a "confirm-transfer-btn" element that does
            // not exist in index.html (the real id is "confirm-go-btn") and
            // then fall through to runVerification(), which is not defined
            // anywhere. The records had already been purged server-side, so the
            // user was told the files were being re-copied when in fact a
            // ReferenceError was thrown and nothing happened at all.
            const source = document.getElementById('source-path').value;
            if (!source) {
                alert(`Purged ${purged} failed file record(s).\n\nThe original source folder is not set, so the re-copy could not be started automatically. Select the source folder on the Setup screen and click "Start Organizing" to re-copy them.`);
                document.getElementById('dest-path').value = destPath;
                showView('setup-view');
                return;
            }

            alert(`Purged ${purged} failed file record(s). Starting a transfer now to re-copy the missing & mismatched files...`);
            document.getElementById('dest-path').value = destPath;
            document.getElementById('preview-mode').checked = false;
            lastWasPreview = false;
            await startOrganizing();
        } else {
            alert(`Repair error: ${data.error || `HTTP ${response.status}`}`);
        }
    } catch (e) {
        alert("Error connecting to server to repair transfer.");
    }
}


async function loadHistoryView(shouldNavigate = true) {
    if (shouldNavigate) showView('history-view');
    const container = document.getElementById('history-list-container');
    if (!container) return;
    if (shouldNavigate) container.innerHTML = "<div>Loading past runs...</div>";


    try {
        const response = await fetch('/api/history');
        if (!response.ok) {
            container.innerHTML = `<div style="color: var(--danger-color); font-size: 12px; padding: 20px; text-align: center;">⚠️ Could not load past runs (HTTP ${response.status}). Your history has not been lost — please retry.</div>`;
            return;
        }
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
                    <strong style="font-size: 13px;">🕒 ${escapeHtml(run.timestamp)}</strong>
                    <span style="font-size: 10px; background: ${badgeColor}; color: #fff; padding: 2px 8px; border-radius: 10px; font-weight: bold;">${escapeHtml(run.status)}</span>
                </div>
                <div style="font-size: 11px; opacity: 0.9; margin-bottom: 4px;">
                    📂 <strong>Source:</strong> ${escapeHtml(run.source)}<br>
                    🎯 <strong>Destination:</strong> ${escapeHtml(run.dest)}
                </div>
                <div style="font-size: 11px; opacity: 0.7; margin-bottom: 8px;">
                    📊 <strong>Files:</strong> ${escapeHtml(run.total_files)} files (${escapeHtml(run.total_size)}) • <strong>Code Projects:</strong> ${escapeHtml(run.projects_count || 0)}
                </div>
                <div style="display: flex; gap: 8px; flex-wrap: wrap; margin-top: 8px;">
                    <button class="btn secondary history-csv-btn" data-dest="${escapeHtml(run.dest)}" style="font-size: 10px; padding: 4px 8px;">
                        📊 Download CSV Audit Log
                    </button>
                    <button class="btn secondary history-verify-btn" data-dest="${escapeHtml(run.dest)}" style="font-size: 10px; padding: 4px 8px;">
                        ✅ Run Integrity Check
                    </button>
                    <button class="btn secondary history-finder-btn" data-dest="${escapeHtml(run.dest)}" style="font-size: 10px; padding: 4px 8px;">
                        📂 Open Destination in Finder
                    </button>
                </div>
            </div>`;
        });
        container.innerHTML = html;
        container.querySelectorAll('.history-csv-btn').forEach(btn => {
            btn.addEventListener('click', () => downloadHistoryCSV(btn.dataset.dest));
        });
        container.querySelectorAll('.history-verify-btn').forEach(btn => {
            btn.addEventListener('click', () => verifyHistoryRun(btn.dataset.dest));
        });
        container.querySelectorAll('.history-finder-btn').forEach(btn => {
            btn.addEventListener('click', () => openHistoryFinder(btn.dataset.dest));
        });
    } catch (e) {
        container.innerHTML = "<div>Error loading past runs history</div>";
    }
}

function downloadHistoryCSV(destPath) {
    if (!destPath) return;
    downloadCsvReport(destPath);
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
    const startBtn = document.getElementById('start-btn');
    if (cancelBtn) {
        cancelBtn.disabled = true;
        cancelBtn.innerText = "Cancelling...";
    }
    // Keep Start locked out. /api/cancel only requests the stop -- the worker
    // thread is still copying and still holds the checkpoint database, and it
    // is the only thing that may declare a terminal status. Re-enabling Start
    // here is what previously let a second organizer run against the same DB.
    // startPolling()'s terminal branch restores both buttons.
    if (startBtn) {
        startBtn.disabled = true;
        startBtn.innerText = "Cancelling...";
    }
    try {
        const response = await fetch('/api/cancel', { method: 'POST' });
        const data = await response.json().catch(() => ({}));
        if (!response.ok || data.success === false) {
            alert(`Could not cancel: ${data.error || `HTTP ${response.status}`}`);
            if (cancelBtn) {
                cancelBtn.disabled = false;
                cancelBtn.innerText = "⛔ Cancel Operation";
            }
            if (startBtn) {
                startBtn.disabled = false;
                startBtn.innerText = "Start Organizing";
            }
            return;
        }
        const heading = document.getElementById('status-heading');
        if (heading) heading.innerText = "⛔ Cancelling — finishing the file in flight...";
    } catch (e) {
        alert("Error cancelling operation: " + e);
        if (cancelBtn) {
            cancelBtn.disabled = false;
            cancelBtn.innerText = "⛔ Cancel Operation";
        }
        if (startBtn) {
            startBtn.disabled = false;
            startBtn.innerText = "Start Organizing";
        }
    }
}

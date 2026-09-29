let currentDestMode = 'new';
let excludedProjects = [];
let currentInspectedPath = "";
let viewHistory = ['guide-view'];
let pollInterval = null;
let lastWasPreview = false;
let lastPreviewSummary = null;
let allLogs = [];
let isTransferActive = false;

function onSourceInputChanged() {
    excludedProjects = [];
}

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
                isTransferActive = false;
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
                        showView('preview-view');
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
    if (isTransferActive && viewId !== 'progress-view') {
        return;
    }
    if (viewHistory[viewHistory.length - 1] !== viewId) {
        viewHistory.push(viewId);
    }
    document.querySelectorAll('.view').forEach(v => v.classList.remove('active'));
    document.getElementById(viewId).classList.add('active');

    // Update back button & breadcrumbs
    const backBtn = document.getElementById('nav-back-btn');
    backBtn.style.display = (viewHistory.length > 1 && !isTransferActive) ? 'inline-block' : 'none';

    document.querySelectorAll('.crumb').forEach(c => c.classList.remove('active'));
    if (viewId === 'guide-view') document.getElementById('crumb-guide').classList.add('active');
    if (viewId === 'setup-view') document.getElementById('crumb-setup').classList.add('active');
    if (viewId === 'progress-view') document.getElementById('crumb-progress').classList.add('active');
    if (viewId === 'history-view') {
        const crumbHist = document.getElementById('crumb-history');
        if (crumbHist) crumbHist.classList.add('active');
    }
    if (viewId === 'duplicates-view') {
        const crumbDup = document.getElementById('crumb-duplicates');
        if (crumbDup) crumbDup.classList.add('active');
    }
}

function navigateBack() {
    if (isTransferActive) return;
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
        if (prevView === 'duplicates-view') {
            const crumbDup = document.getElementById('crumb-duplicates');
            if (crumbDup) crumbDup.classList.add('active');
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
            if (type === 'source') {
                const existing = input.value.split(',').map(s => s.trim()).filter(Boolean);
                if (existing.length > 0) {
                    if (!existing.includes(data.folder)) {
                        existing.push(data.folder);
                        input.value = existing.join(', ');
                        excludedProjects = [];
                    }
                } else {
                    input.value = data.folder;
                    excludedProjects = [];
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
    const stripTrailingSlash = p => p.trim().normalize('NFD').replace(/\/+$/, '');
    const sources = source.split(',').map(stripTrailingSlash).filter(Boolean);
    const destNorm = stripTrailingSlash(dest);
    const clash = sources.find(s => s === destNorm
        || destNorm.startsWith(s + '/')
        || s.startsWith(destNorm + '/'));
    if (clash) {
        alert(`Safety Error: "${destNorm}" and "${clash}" are nested inside each other.\n\nChoose a destination outside every source folder.`);
        return;
    }

    if (pollInterval) {
        clearInterval(pollInterval);
        pollInterval = null;
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
            isTransferActive = true;
            lastWasPreview = isPreview;
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
    const dash = document.getElementById('preview-dashboard-content');

    if (!summary || (!summary.total_files && !summary.total_projects)) {
        dash.innerHTML = "<div class='text-center py-12 text-slate-500 dark:text-slate-400'><i data-lucide='check-circle' class='w-12 h-12 mx-auto mb-3 text-emerald-500'></i><p class='text-lg font-medium'>Preview Complete! Ready to transfer files.</p></div>";
        try { lucide.createIcons(); } catch(e) {}
        return;
    }

    // 1. Categories
    let catsHtml = "";
    if (summary.categories) {
        for (const [cat, count] of Object.entries(summary.categories)) {
            catsHtml += `
            <button class="category-pill-btn group flex items-center justify-between p-3 bg-slate-50 dark:bg-slate-900/50 border border-slate-200 dark:border-slate-700 rounded-xl hover:border-indigo-400 dark:hover:border-indigo-500 hover:shadow-md transition-all text-left w-full" data-category="${escapeHtml(cat)}">
                <div class="flex items-center gap-3 pointer-events-none">
                    <div class="p-2 bg-indigo-100 dark:bg-indigo-900/30 rounded-lg text-indigo-600 group-hover:bg-indigo-200 dark:group-hover:bg-indigo-900/50 transition-colors">
                        <i data-lucide="folder" class="w-4 h-4"></i>
                    </div>
                    <div>
                        <div class="font-bold text-sm text-slate-800 dark:text-slate-200">${escapeHtml(cat)}</div>
                        <div class="text-[10px] text-slate-500 group-hover:text-indigo-500 flex items-center gap-1 transition-colors"><i data-lucide="eye" class="w-3 h-3"></i> View samples</div>
                    </div>
                </div>
                <div class="pointer-events-none">
                    <span class="text-xs font-bold text-slate-700 dark:text-slate-300 bg-white dark:bg-slate-800 px-2.5 py-1 rounded-full shadow-sm border border-slate-200 dark:border-slate-700">${Number(count).toLocaleString()}</span>
                </div>
            </button>`;
        }
    }

    // 2. Skipped Folders
    let skippedHtml = "";
    const skippedBreakdown = summary.skipped_dir_breakdown;
    if (skippedBreakdown && Object.keys(skippedBreakdown).length > 0) {
        const entries = Object.entries(skippedBreakdown).sort((a, b) => b[1] - a[1]);
        const totalSkipped = entries.reduce((acc, kv) => acc + kv[1], 0);
        
        let breakdownHtml = entries.map(([name, count]) =>
            `<div class="flex justify-between items-center py-1 border-b border-amber-100 dark:border-amber-900/30 last:border-0"><span class="flex items-center gap-2"><i data-lucide="folder-minus" class="w-4 h-4 text-amber-500/80"></i> <strong class="text-sm">${escapeHtml(name)}</strong></span> <span class="text-xs font-semibold bg-amber-100 dark:bg-amber-900/50 px-2 py-0.5 rounded">${Number(count).toLocaleString()} dirs</span></div>`
        ).join('');

        skippedHtml = `
            <div class="flex items-center justify-between mb-4">
                <h3 class="font-bold flex items-center gap-2 text-amber-800 dark:text-amber-500"><div class="p-1.5 bg-amber-100 dark:bg-amber-900/40 rounded-lg"><i data-lucide="skip-forward" class="w-4 h-4 text-amber-600"></i></div> Build / Cache Ignored</h3>
                <span class="text-xs font-bold text-amber-700 dark:text-amber-400 bg-amber-100 dark:bg-amber-900/40 px-2.5 py-1 rounded-full">${totalSkipped.toLocaleString()} total</span>
            </div>
            <div class="bg-amber-50 dark:bg-amber-900/10 rounded-xl border border-amber-200/50 dark:border-amber-800/50 p-3 mb-3">
                ${breakdownHtml}
            </div>
            <div class="text-[10px] text-amber-700/80 dark:text-amber-500/80 italic flex items-start gap-1.5">
                <i data-lucide="info" class="w-3.5 h-3.5 shrink-0"></i>
                <p>These folders are ignored to save space. They will not be copied.</p>
            </div>
        `;
    }

    // 3. Projects
    let projsHtml = "";
    if (summary.project_details && summary.project_details.length > 0) {
        projsHtml = `
            <div class="flex items-center justify-between mb-4">
                <h3 class="font-bold text-slate-800 dark:text-slate-200 flex items-center gap-2"><div class="p-1.5 bg-indigo-50 dark:bg-indigo-900/30 rounded-lg"><i data-lucide="code-2" class="w-4 h-4 text-indigo-600 dark:text-indigo-400"></i></div> Code Repositories</h3>
                <span class="text-xs font-bold text-slate-600 dark:text-slate-300 bg-slate-100 dark:bg-slate-700 px-2.5 py-1 rounded-full">${summary.total_projects} intact</span>
            </div>
            <p class="text-xs text-slate-500 mb-3">These folders are kept fully intact by default.</p>
            
            <div class="flex gap-2 mb-3">
                <button class="flex-1 py-1.5 text-xs font-semibold bg-slate-100 hover:bg-slate-200 dark:bg-slate-700 dark:hover:bg-slate-600 text-slate-700 dark:text-slate-200 rounded-lg transition-colors border border-slate-200 dark:border-slate-600" id="projects-select-all-btn">Select All</button>
                <button class="flex-1 py-1.5 text-xs font-semibold bg-slate-100 hover:bg-slate-200 dark:bg-slate-700 dark:hover:bg-slate-600 text-slate-700 dark:text-slate-200 rounded-lg transition-colors border border-slate-200 dark:border-slate-600" id="projects-deselect-all-btn">Deselect All</button>
            </div>
            
            <div class="relative mb-3">
                <i data-lucide="search" class="w-4 h-4 text-slate-400 absolute left-3 top-2.5 pointer-events-none"></i>
                <input type="text" id="project-search-filter" placeholder="Search repos..." onkeyup="filterProjectsList()" class="w-full pl-9 pr-3 py-2 bg-slate-50 dark:bg-slate-900 border border-slate-200 dark:border-slate-700 rounded-lg text-xs focus:ring-1 focus:ring-indigo-500">
            </div>
            
            <div id="projects-checkbox-container" class="max-h-64 overflow-y-auto bg-slate-50 dark:bg-slate-900/50 p-2 rounded-xl border border-slate-200 dark:border-slate-700 space-y-1 shadow-inner">`;

        summary.project_details.forEach(p => {
            const isExcluded = excludedProjects.includes(p.path);
            const checkedAttr = isExcluded ? '' : 'checked';
            const textStyle = isExcluded ? 'line-through opacity-50' : '';
            const statusLabel = isExcluded 
                ? '<span class="text-rose-500 text-[10px] font-bold"><i data-lucide="zap" class="w-3 h-3 inline"></i> Split up</span>' 
                : '<span class="text-emerald-600 text-[10px] font-bold"><i data-lucide="check" class="w-3 h-3 inline"></i> Keep Intact</span>';
            
            projsHtml += `
            <div class="project-row-item flex flex-col sm:flex-row sm:items-center justify-between p-2.5 bg-white dark:bg-slate-800 rounded-lg border border-slate-200 dark:border-slate-700 gap-2">
                <label class="flex items-start gap-2.5 cursor-pointer flex-1 overflow-hidden">
                    <input type="checkbox" class="project-checkbox mt-1 rounded text-indigo-600 focus:ring-indigo-500 bg-slate-100 border-slate-300 dark:bg-slate-700 dark:border-slate-600" value="${escapeHtml(p.path)}" ${checkedAttr} onchange="onProjectCheckboxChange('${escapeHtml(p.path)}', this.checked)">
                    <div class="truncate ${textStyle}">
                        <div class="text-xs font-semibold text-slate-800 dark:text-slate-200 truncate" title="${escapeHtml(p.path)}">${escapeHtml(p.path.split('/').pop())}</div>
                        <div class="text-[10px] text-slate-400 truncate" title="${escapeHtml(p.path)}">${escapeHtml(p.path)}</div>
                    </div>
                </label>
                <div class="flex items-center justify-between sm:justify-end gap-3 w-full sm:w-auto pl-7 sm:pl-0">
                    ${statusLabel}
                    <button class="p-1.5 text-slate-400 hover:text-indigo-600 hover:bg-indigo-50 dark:hover:bg-indigo-900/30 rounded transition-colors" onclick="inspectProject('${escapeHtml(p.path)}')"><i data-lucide="search" class="w-4 h-4"></i></button>
                </div>
            </div>`;
        });
        projsHtml += `</div>`;
    }

    // 4. Garbage Files
    let garbageHtml = "";
    const gTotal = (summary.garbage_breakdown ? Object.values(summary.garbage_breakdown).reduce((a,b)=>a+b, 0) : 0);
    if (gTotal > 0) {
        garbageHtml = `
            <div class="flex items-center justify-between mb-4">
                <h3 class="font-bold text-slate-800 dark:text-slate-200 flex items-center gap-2"><div class="p-1.5 bg-rose-50 dark:bg-rose-900/30 rounded-lg"><i data-lucide="trash" class="w-4 h-4 text-rose-500"></i></div> System Garbage</h3>
                <span class="text-xs font-bold text-rose-600 dark:text-rose-400 bg-rose-50 dark:bg-rose-900/30 px-2.5 py-1 rounded-full">${gTotal.toLocaleString()} files</span>
            </div>
            <p class="text-xs text-slate-500 mb-3">Temporary system files that will be permanently ignored.</p>
            <button class="w-full py-2 bg-slate-50 hover:bg-slate-100 dark:bg-slate-800 dark:hover:bg-slate-700 text-slate-700 dark:text-slate-300 text-xs font-semibold rounded-lg transition-colors border border-slate-200 dark:border-slate-700 flex justify-center items-center gap-2" onclick="inspectGarbageFiles()">
                <i data-lucide="search" class="w-4 h-4"></i> Inspect Ignored Files
            </button>
        `;
    }

    dash.innerHTML = `
        <div class="mb-4 p-4 bg-emerald-50 dark:bg-emerald-900/10 border border-emerald-200 dark:border-emerald-800/40 rounded-xl flex items-center gap-3 shadow-sm">
            <div class="p-2 bg-emerald-100 dark:bg-emerald-900/50 rounded-full"><i data-lucide="check" class="w-5 h-5 text-emerald-600 dark:text-emerald-400"></i></div>
            <div>
                <h3 class="font-bold text-emerald-800 dark:text-emerald-400">Scan Complete (0 bytes moved)</h3>
                <p class="text-xs text-emerald-700 dark:text-emerald-500/80 mt-0.5">Review the breakdown below, then click Execute to begin transferring.</p>
            </div>
        </div>

        <div class="grid grid-cols-1 lg:grid-cols-2 gap-5">
            <!-- Left Column -->
            <div class="space-y-5">
                ${catsHtml ? `
                <div class="bg-white dark:bg-slate-800 rounded-2xl shadow-sm border border-slate-200 dark:border-slate-700 p-5">
                    <h3 class="text-sm font-bold text-slate-800 dark:text-slate-200 mb-4 flex items-center gap-2"><i data-lucide="pie-chart" class="w-4 h-4 text-indigo-500"></i> File Categories Found</h3>
                    <div class="grid grid-cols-1 sm:grid-cols-2 gap-3">
                        ${catsHtml}
                    </div>
                </div>` : ''}
                
                ${garbageHtml ? `
                <div class="bg-white dark:bg-slate-800 rounded-2xl shadow-sm border border-slate-200 dark:border-slate-700 p-5">
                    ${garbageHtml}
                </div>` : ''}
            </div>

            <!-- Right Column -->
            <div class="space-y-5">
                ${projsHtml ? `
                <div class="bg-white dark:bg-slate-800 rounded-2xl shadow-sm border border-slate-200 dark:border-slate-700 p-5">
                    ${projsHtml}
                </div>` : ''}

                ${skippedHtml ? `
                <div class="bg-white dark:bg-slate-800 rounded-2xl shadow-sm border border-slate-200 dark:border-slate-700 p-5">
                    ${skippedHtml}
                </div>` : ''}
            </div>
        </div>
    `;

    // Reattach listeners
    document.querySelectorAll('.category-pill-btn').forEach(btn => {
        btn.addEventListener('click', (e) => {
            const cat = e.currentTarget.getAttribute('data-category');
            inspectCategoryFiles(cat);
        });
    });

    const selectAllBtn = document.getElementById('projects-select-all-btn');
    if (selectAllBtn) selectAllBtn.addEventListener('click', () => selectAllProjects(true));
    const deselectAllBtn = document.getElementById('projects-deselect-all-btn');
    if (deselectAllBtn) deselectAllBtn.addEventListener('click', () => selectAllProjects(false));

    try { lucide.createIcons(); } catch(e) {}
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

    // The dashboard passes only the path (audit P3-01); name the folder from it.
    title.innerText = `Inspect Folder: ${name || String(path).split('/').pop()}`;
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
    showView('progress-view');
    // ensure logs are visible again
    document.getElementById('log-container').scrollTop = document.getElementById('log-container').scrollHeight;
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
        if (!response.ok || data.success === false) {
            alert(`Error dissolving project: ${data.error || data.message || `HTTP ${response.status}`}`);
            return;
        }
        alert(data.message);
        loadProjectReview();
    } catch (e) {
        alert("Error dissolving project: " + e);
    }
}

let duplicateSourcePaths = [];
let trashedSourcePaths = [];

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
        const duplicates = Array.isArray(data.duplicates) ? data.duplicates : [];
        const trashed = Array.isArray(data.trashed) ? data.trashed : [];

        duplicateSourcePaths = duplicates.map(d => d.source_path);
        trashedSourcePaths = trashed.map(t => t.source_path);

        if (duplicates.length === 0 && trashed.length === 0) {
            area.innerHTML = "<div style='font-size: 12px; opacity: 0.7;'>✓ No duplicate source files were skipped. Source drive is clean.</div>";
            return;
        }

        let html = '';

        if (duplicates.length > 0) {
            let totalSize = duplicates.reduce((acc, curr) => acc + (curr.size || 0), 0);
            let sizeMB = (totalSize / (1024 * 1024)).toFixed(2);

            html += `
            <div style="margin-bottom: 10px; display: flex; justify-content: space-between; align-items: center; gap: 8px; flex-wrap: wrap;">
                <strong>Found ${duplicates.length} Duplicate Files (${sizeMB} MB) on Source Drive</strong>
                <button class="btn primary" style="font-size: 11px; padding: 6px 12px;" onclick="trashAllDuplicates()">
                    🗑️ Move ${duplicates.length} Duplicates to Trash
                </button>
            </div>
            <div style="font-size: 11px; max-height: 120px; overflow-y: auto; margin-bottom: ${trashed.length > 0 ? '12px' : '0'};">`;

            duplicates.forEach(d => {
                html += `<div style="white-space: nowrap; overflow: hidden; text-overflow: ellipsis; padding: 2px 0;">• ${escapeHtml(d.source_path)}</div>`;
            });
            html += `</div>`;
        } else {
            html += `<div style="font-size: 12px; opacity: 0.7; margin-bottom: 10px;">✓ All detected duplicates on the source drive have been isolated in <code>.Duplicates_Trash</code>.</div>`;
        }

        if (trashed.length > 0) {
            let trashedSize = trashed.reduce((acc, curr) => acc + (curr.size || 0), 0);
            let trashedMB = (trashedSize / (1024 * 1024)).toFixed(2);
            html += `
            <div style="border-top: 1px solid rgba(255,255,255,0.08); padding-top: 10px; margin-top: 8px;">
                <div style="margin-bottom: 8px; display: flex; justify-content: space-between; align-items: center; gap: 8px; flex-wrap: wrap;">
                    <strong>♻️ ${trashed.length} Isolated Duplicate(s) in <code>.Duplicates_Trash</code> (${trashedMB} MB)</strong>
                    <button class="btn secondary" style="font-size: 11px; padding: 6px 12px;" onclick="restoreAllDuplicates()">
                        ♻️ Restore ${trashed.length} File${trashed.length === 1 ? '' : 's'} to Original Folder
                    </button>
                </div>
                <div style="font-size: 11px; max-height: 100px; overflow-y: auto; opacity: 0.85;">`;
            trashed.forEach(t => {
                html += `<div style="white-space: nowrap; overflow: hidden; text-overflow: ellipsis; padding: 2px 0;">↩ ${escapeHtml(t.source_path)}</div>`;
            });
            html += `</div></div>`;
        }

        area.innerHTML = html;
    } catch (e) {
        area.innerHTML = "<div>Error loading duplicates</div>";
    }
}

async function trashAllDuplicates() {
    if (!confirm(`Are you sure you want to isolate ${duplicateSourcePaths.length} duplicate files on your SOURCE drive into a .Duplicates_Trash folder?`)) {
        return;
    }
    const dest = document.getElementById('dest-path').value;
    try {
        const response = await fetch('/api/trash_duplicates', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ source_paths: duplicateSourcePaths, dest: dest })
        });
        const data = await response.json().catch(() => ({}));
        if (!response.ok || data.success === false) {
            alert(`Error moving duplicate files: ${data.error || `HTTP ${response.status}`}`);
            return;
        }
        let msg = `Successfully moved ${data.count} duplicate files into .Duplicates_Trash with 0 Touch ID prompts!`;
        if (data.refused && data.refused.length > 0) {
            msg += `\n\nSkipped ${data.refused.length} unverified/failed file(s).`;
        }
        alert(msg);
        loadDuplicateCleaner();
    } catch (e) {
        alert("Error moving duplicate files: " + e);
    }
}

async function restoreAllDuplicates() {
    if (!trashedSourcePaths || trashedSourcePaths.length === 0) {
        return;
    }
    if (!confirm(`Restore ${trashedSourcePaths.length} file(s) from .Duplicates_Trash back to their original source locations?`)) {
        return;
    }
    const dest = document.getElementById('dest-path').value;
    try {
        const response = await fetch('/api/restore_duplicates', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ source_paths: trashedSourcePaths, dest: dest })
        });
        const data = await response.json().catch(() => ({}));
        if (!response.ok || data.success === false) {
            alert(`Error restoring duplicate files: ${data.error || `HTTP ${response.status}`}`);
            return;
        }
        let msg = `Successfully restored ${data.count} file(s) back to their original folders!`;
        if (data.refused && data.refused.length > 0) {
            msg += `\n\nSkipped ${data.refused.length} file(s) that could not be safely restored.`;
        }
        alert(msg);
        loadDuplicateCleaner();
    } catch (e) {
        alert("Error restoring duplicate files: " + e);
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
    if (shouldNavigate && isTransferActive) return;
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
        const response = await fetch('/api/clear_history', { method: 'POST' });
        if (!response.ok) {
            alert(`Error clearing history (HTTP ${response.status})`);
            return;
        }
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

// ==========================================
// STANDALONE DUPLICATE SCANNER LOGIC
// ==========================================

async function selectDupFolder() {
    try {
        const response = await fetch(`/api/select_folder?prompt=${encodeURIComponent("Select folder to scan for duplicates:")}`);
        const data = await response.json();
        if (data.folder) {
            document.getElementById('dup-source-path').value = data.folder;
        }
    } catch (e) {
        console.error("Error selecting folder", e);
    }
}

let dupPollInterval = null;

async function startDuplicateScan() {
    const folder = document.getElementById('dup-source-path').value;
    if (!folder) {
        alert("Please select a folder first.");
        return;
    }
    
    document.getElementById('start-dup-scan-btn').disabled = true;
    document.getElementById('dup-progress-container').classList.remove('hidden');
    document.getElementById('dup-results-container').classList.add('hidden');
    document.getElementById('dup-progress-fill').style.width = "0%";
    document.getElementById('dup-progress-percentage').innerText = "0%";
    document.getElementById('dup-current-message').innerText = "Initializing...";
    
    try {
        const response = await fetch('/api/dup_scan_start', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ folder: folder })
        });
        const data = await response.json();
        if (data.success) {
            pollDuplicateScan();
        } else {
            alert(data.error);
            document.getElementById('start-dup-scan-btn').disabled = false;
        }
    } catch(e) {
        alert("Error starting scan: " + e);
        document.getElementById('start-dup-scan-btn').disabled = false;
    }
}

let isDupPollingInFlight = false;

function pollDuplicateScan() {
    if (dupPollInterval) clearInterval(dupPollInterval);
    isDupPollingInFlight = false;

    dupPollInterval = setInterval(async () => {
        if (isDupPollingInFlight) return;
        isDupPollingInFlight = true;
        try {
            const res = await fetch('/api/dup_scan_status');
            if (!res.ok) return;
            const data = await res.json();

            const fill = document.getElementById('dup-progress-fill');
            const percentTxt = document.getElementById('dup-progress-percentage');
            const msgTxt = document.getElementById('dup-current-message');

            const progressVal = Number(data.progress ?? 0);
            const totalVal = Number(data.total ?? 0);
            const percent = totalVal > 0 ? Math.min(100, Math.round((progressVal / totalVal) * 100)) : 0;

            if (fill) fill.style.width = `${percent}%`;
            if (percentTxt) percentTxt.innerText = `${percent}%`;
            if (msgTxt && data.message) msgTxt.innerText = data.message;

            if (data.status === 'complete' || data.status === 'cancelled' || data.status === 'error') {
                clearInterval(dupPollInterval);
                dupPollInterval = null;
                document.getElementById('start-dup-scan-btn').disabled = false;

                if (data.status === 'complete') {
                    if (msgTxt) msgTxt.innerText = "Loading duplicate groups into view...";
                    const fullRes = await fetch('/api/dup_scan_status?include_results=1');
                    const fullData = await fullRes.json();
                    initDuplicateResults(fullData.results || []);
                } else if (data.status === 'error') {
                    alert("Scan failed: " + data.message);
                }
            }
        } catch (e) {
            console.error("Error polling dup scan", e);
        } finally {
            isDupPollingInFlight = false;
        }
    }, 500);
}

async function cancelDuplicateScan() {
    try {
        await fetch('/api/dup_scan_cancel', { method: 'POST' });
        document.getElementById('dup-current-message').innerText = "Cancelling...";
    } catch (e) {
        console.error("Cancel failed", e);
    }
}

let currentDupResults = [];
let filteredDupIndices = [];
let selectedDupPaths = new Set();
let dupPathToSize = new Map();
let currentDupRenderLimit = 150;

const DUP_VIDEO_EXTS = new Set(['.mp4', '.mov', '.mkv', '.avi', '.webm', '.m4v', '.wmv', '.flv', '.3gp', '.mts', '.m2ts']);
const DUP_PHOTO_EXTS = new Set(['.jpg', '.jpeg', '.png', '.heic', '.heif', '.webp', '.gif', '.bmp', '.tiff', '.tif', '.cr2', '.cr3', '.nef', '.arw', '.dng', '.orf', '.rw2']);
const DUP_ARCHIVE_EXTS = new Set(['.zip', '.rar', '.7z', '.tar', '.gz', '.bz2', '.xz', '.tgz', '.dmg', '.iso', '.pkg']);

function formatDupBytes(bytes) {
    const b = Number(bytes || 0);
    if (b >= 1024 * 1024 * 1024 * 1024) {
        return (b / (1024 * 1024 * 1024 * 1024)).toFixed(2) + " TB";
    }
    if (b >= 1024 * 1024 * 1024) {
        return (b / (1024 * 1024 * 1024)).toFixed(2) + " GB";
    }
    if (b >= 1024 * 1024) {
        return (b / (1024 * 1024)).toFixed(2) + " MB";
    }
    if (b >= 1024) {
        return (b / 1024).toFixed(1) + " KB";
    }
    return b + " B";
}

function getFileExtLower(p) {
    const base = p.split('/').pop() || "";
    const dot = base.lastIndexOf('.');
    return dot >= 0 ? base.slice(dot).toLowerCase() : "";
}

function initDuplicateResults(groups) {
    currentDupResults = Array.isArray(groups) ? groups : [];
    selectedDupPaths.clear();
    dupPathToSize.clear();

    currentDupResults.forEach(group => {
        const sz = Number(group.size || 0);
        (group.files || []).forEach((f, idx) => {
            dupPathToSize.set(f, sz);
            // Pre-select all redundant copies (idx > 0), keeping idx === 0 as Original
            if (idx > 0) {
                selectedDupPaths.add(f);
            }
        });
    });

    const searchInput = document.getElementById('dup-search-input');
    const typeFilter = document.getElementById('dup-type-filter');
    if (searchInput) searchInput.value = "";
    if (typeFilter) typeFilter.value = "all";

    applyDupFiltersAndRender(true);
}

function onDupFilterChanged() {
    applyDupFiltersAndRender(true);
}

function applyDupFiltersAndRender(resetLimit = true) {
    if (resetLimit) currentDupRenderLimit = 150;
    const q = ((document.getElementById('dup-search-input') || {}).value || "").trim().toLowerCase();
    const typeVal = ((document.getElementById('dup-type-filter') || {}).value || "all");

    filteredDupIndices = [];
    currentDupResults.forEach((group, gIdx) => {
        const files = group.files || [];
        if (files.length < 2) return;
        if (typeVal !== 'all') {
            const ext = getFileExtLower(files[0]);
            if (typeVal === 'large' && group.size < 50 * 1024 * 1024) return;
            if (typeVal === 'video' && !DUP_VIDEO_EXTS.has(ext)) return;
            if (typeVal === 'photo' && !DUP_PHOTO_EXTS.has(ext)) return;
            if (typeVal === 'archive' && !DUP_ARCHIVE_EXTS.has(ext)) return;
            if (typeVal === 'doc' && (DUP_VIDEO_EXTS.has(ext) || DUP_PHOTO_EXTS.has(ext) || DUP_ARCHIVE_EXTS.has(ext))) return;
        }
        if (q) {
            const matchesQuery = files.some(f => f.toLowerCase().includes(q));
            if (!matchesQuery) return;
        }
        filteredDupIndices.push(gIdx);
    });

    renderDuplicateGroupsList();
    updateDupMetricsUI();
}

function updateDupMetricsUI() {
    let selectedBytes = 0;
    selectedDupPaths.forEach(p => {
        selectedBytes += (dupPathToSize.get(p) || 0);
    });
    const selectedCount = selectedDupPaths.size;
    const spaceStr = formatDupBytes(selectedBytes);

    const statGroups = document.getElementById('dup-stat-groups');
    const statFiles = document.getElementById('dup-stat-files');
    const statSpace = document.getElementById('dup-stat-space');
    if (statGroups) statGroups.innerText = currentDupResults.length.toLocaleString();
    if (statFiles) statFiles.innerText = selectedCount.toLocaleString();
    if (statSpace) statSpace.innerText = spaceStr;

    const permLabel = document.getElementById('dup-perm-delete-btn-label');
    const quarLabel = document.getElementById('dup-quarantine-btn-label');
    if (permLabel) {
        permLabel.innerText = selectedCount > 0
            ? `Permanently Delete ${selectedCount.toLocaleString()} Files (${spaceStr})`
            : `Permanently Delete Selected`;
    }
    if (quarLabel) {
        quarLabel.innerText = selectedCount > 0
            ? `Quarantine ${selectedCount.toLocaleString()} Files`
            : `Quarantine to .Duplicates_Trash`;
    }

    let totalRedundantFiles = 0;
    let totalRedundantSize = 0;
    currentDupResults.forEach(group => {
        totalRedundantFiles += (group.files.length - 1);
        totalRedundantSize += (group.size * (group.files.length - 1));
    });

    const summaryEl = document.getElementById('dup-results-summary');
    if (summaryEl) {
        if (currentDupResults.length === 0) {
            summaryEl.innerText = "Found 0 duplicates. Your drive has no redundant files.";
        } else {
            const filterNote = filteredDupIndices.length !== currentDupResults.length
                ? ` • Showing ${filteredDupIndices.length.toLocaleString()} matching groups`
                : "";
            summaryEl.innerText =
                `Total: ${totalRedundantFiles.toLocaleString()} redundant copies across ${currentDupResults.length.toLocaleString()} groups (${formatDupBytes(totalRedundantSize)} max reclaimable)${filterNote}`;
        }
    }
}

function renderDuplicateGroupsList() {
    document.getElementById('dup-progress-container').classList.add('hidden');
    document.getElementById('dup-results-container').classList.remove('hidden');

    const list = document.getElementById('dup-groups-list');
    if (currentDupResults.length === 0) {
        list.innerHTML = "<div class='text-sm text-emerald-600 dark:text-emerald-400 font-semibold p-6 text-center bg-emerald-50/50 dark:bg-emerald-900/10 rounded-xl border border-emerald-200/50 dark:border-emerald-800/40'>✓ No exact duplicates found in this folder!</div>";
        return;
    }
    if (filteredDupIndices.length === 0) {
        list.innerHTML = "<div class='text-sm text-slate-500 p-6 text-center'>No duplicate groups match your current search/filter.</div>";
        return;
    }

    const rootFolder = (document.getElementById('dup-source-path').value || "").replace(/\/+$/, "");
    const visibleGroupIndices = filteredDupIndices.slice(0, currentDupRenderLimit);

    let html = "";
    visibleGroupIndices.forEach((gIdx, displayIdx) => {
        const group = currentDupResults[gIdx];
        const groupWaste = group.size * (group.files.length - 1);
        const primaryName = (group.files[0] || "").split('/').pop() || "File";

        let filesHtml = "";
        group.files.forEach(f => {
            const isSelected = selectedDupPaths.has(f);
            const checkedAttr = isSelected ? "checked" : "";
            const fileName = f.split('/').pop() || f;
            const relDir = (rootFolder && f.startsWith(rootFolder + "/"))
                ? f.slice(rootFolder.length + 1, Math.max(rootFolder.length + 1, f.length - fileName.length - 1))
                : f.slice(0, Math.max(0, f.length - fileName.length - 1));

            const badgeHtml = isSelected
                ? `<span class="dup-row-badge px-2 py-0.5 bg-rose-100 dark:bg-rose-900/30 text-rose-700 dark:text-rose-400 text-[10px] rounded-md font-bold shrink-0">Will Remove</span>`
                : `<span class="dup-row-badge px-2 py-0.5 bg-emerald-100 dark:bg-emerald-900/30 text-emerald-700 dark:text-emerald-400 text-[10px] rounded-md font-bold shrink-0">Keep (Original)</span>`;

            filesHtml += `
                <div class="flex items-center justify-between gap-2 p-2 hover:bg-slate-50 dark:hover:bg-slate-800/70 rounded-lg transition-colors border border-transparent hover:border-slate-200 dark:hover:border-slate-700">
                    <label class="flex items-start gap-2.5 cursor-pointer flex-grow min-w-0">
                        <input type="checkbox" class="dup-checkbox mt-1 w-4 h-4 text-rose-600 rounded border-slate-300 focus:ring-rose-500 shrink-0" data-group-idx="${gIdx}" data-path="${escapeHtml(f)}" ${checkedAttr}>
                        <div class="min-w-0 flex-grow">
                            <div class="flex items-center gap-2 flex-wrap">
                                <span class="text-xs font-semibold text-slate-800 dark:text-slate-200 break-all">${escapeHtml(fileName)}</span>
                                ${badgeHtml}
                            </div>
                            <div class="text-[11px] text-slate-500 dark:text-slate-400 break-all mt-0.5">📁 ${escapeHtml(relDir || "/")}</div>
                        </div>
                    </label>
                    <button type="button" class="dup-reveal-btn px-2 py-1 text-[10px] font-semibold bg-slate-100 hover:bg-slate-200 dark:bg-slate-800 dark:hover:bg-slate-700 text-slate-600 dark:text-slate-300 rounded border border-slate-200 dark:border-slate-700 shrink-0" data-path="${escapeHtml(f)}" title="Reveal in macOS Finder">
                        Reveal
                    </button>
                </div>
            `;
        });

        html += `
            <div class="bg-white dark:bg-slate-900 border border-slate-200 dark:border-slate-700 rounded-xl p-3.5 shadow-sm">
                <div class="flex flex-wrap justify-between items-center gap-2 mb-2 pb-2 border-b border-slate-100 dark:border-slate-800">
                    <div class="flex items-center gap-2 min-w-0">
                        <span class="px-2 py-0.5 bg-amber-100 dark:bg-amber-900/30 text-amber-800 dark:text-amber-300 text-[11px] font-bold rounded-md">#${displayIdx + 1}</span>
                        <span class="text-xs font-bold text-slate-800 dark:text-slate-200 truncate">${escapeHtml(primaryName)}</span>
                        <span class="text-[11px] text-slate-500 dark:text-slate-400">(${group.files.length} identical copies)</span>
                    </div>
                    <div class="text-xs font-semibold text-slate-600 dark:text-slate-300">
                        Each: <span class="font-bold">${formatDupBytes(group.size)}</span> • Reclaimable: <span class="font-bold text-emerald-600 dark:text-emerald-400">${formatDupBytes(groupWaste)}</span>
                    </div>
                </div>
                <div class="space-y-1">
                    ${filesHtml}
                </div>
            </div>
        `;
    });

    if (filteredDupIndices.length > currentDupRenderLimit) {
        const remaining = filteredDupIndices.length - currentDupRenderLimit;
        html += `
            <div class="text-center py-3">
                <button type="button" class="px-4 py-2 text-xs font-semibold bg-slate-100 dark:bg-slate-800 hover:bg-slate-200 dark:hover:bg-slate-700 text-slate-700 dark:text-slate-300 rounded-lg border border-slate-300 dark:border-slate-600" onclick="showMoreDuplicateGroups()">
                    Show Next 150 Groups (${remaining.toLocaleString()} more groups available)
                </button>
            </div>
        `;
    }

    list.innerHTML = html;

    list.querySelectorAll('.dup-checkbox').forEach(cb => {
        cb.addEventListener('change', () => {
            onDupCheckboxToggle(cb, Number(cb.dataset.groupIdx), cb.dataset.path);
        });
    });
    list.querySelectorAll('.dup-reveal-btn').forEach(btn => {
        btn.addEventListener('click', () => revealDupFile(btn.dataset.path));
    });
}

function onDupCheckboxToggle(checkbox, groupIdx, filePath) {
    const group = currentDupResults[groupIdx];
    if (!group) return;

    if (checkbox.checked) {
        // Ensure at least 1 copy in this group remains UNCHECKED (kept as Original)
        const unselectedRemaining = group.files.filter(f => f !== filePath && !selectedDupPaths.has(f));
        if (unselectedRemaining.length === 0) {
            checkbox.checked = false;
            alert("Safety Lock: At least 1 copy in every group must remain unchecked so you never lose the original file.\n\nTo delete this copy instead, first uncheck the copy you want to keep.");
            return;
        }
        selectedDupPaths.add(filePath);
    } else {
        selectedDupPaths.delete(filePath);
    }

    const row = checkbox.closest('label');
    const badge = row ? row.querySelector('.dup-row-badge') : null;
    if (badge) {
        if (checkbox.checked) {
            badge.className = "dup-row-badge px-2 py-0.5 bg-rose-100 dark:bg-rose-900/30 text-rose-700 dark:text-rose-400 text-[10px] rounded-md font-bold shrink-0";
            badge.innerText = "Will Remove";
        } else {
            badge.className = "dup-row-badge px-2 py-0.5 bg-emerald-100 dark:bg-emerald-900/30 text-emerald-700 dark:text-emerald-400 text-[10px] rounded-md font-bold shrink-0";
            badge.innerText = "Keep (Original)";
        }
    }

    updateDupMetricsUI();
}

function selectAllDuplicates(selectRedundant) {
    selectedDupPaths.clear();
    if (selectRedundant) {
        currentDupResults.forEach(group => {
            (group.files || []).forEach((f, idx) => {
                if (idx > 0) selectedDupPaths.add(f);
            });
        });
    }
    renderDuplicateGroupsList();
    updateDupMetricsUI();
}

async function revealDupFile(path) {
    try {
        await fetch('/api/dup_reveal', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ path: path })
        });
    } catch (e) {
        console.error("Failed to reveal file", e);
    }
}

function showMoreDuplicateGroups() {
    currentDupRenderLimit += 150;
    renderDuplicateGroupsList();
}

async function trashSelectedDuplicates(permanentDelete = false) {
    const rootFolder = document.getElementById('dup-source-path').value;
    const sourcePaths = Array.from(selectedDupPaths);

    if (sourcePaths.length === 0) {
        alert("No duplicate files selected.");
        return;
    }

    let selectedBytes = 0;
    sourcePaths.forEach(p => {
        selectedBytes += (dupPathToSize.get(p) || 0);
    });
    const spaceStr = formatDupBytes(selectedBytes);

    const actionVerb = permanentDelete
        ? `PERMANENTLY DELETE ${sourcePaths.length.toLocaleString()} duplicate files (${spaceStr}) to immediately free disk space`
        : `quarantine ${sourcePaths.length.toLocaleString()} duplicate files (${spaceStr}) into the .Duplicates_Trash folder`;

    if (!confirm(`Are you sure you want to ${actionVerb}?\n\nAt least 1 verified Original copy in every group is guaranteed to be kept safe.`)) {
        return;
    }

    const permBtn = document.getElementById('dup-perm-delete-btn');
    const quarBtn = document.getElementById('dup-quarantine-btn');
    if (permBtn) permBtn.disabled = true;
    if (quarBtn) quarBtn.disabled = true;

    try {
        const response = await fetch('/api/dup_trash_inplace', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({
                source_paths: sourcePaths,
                root_folder: rootFolder,
                permanent_delete: permanentDelete,
                delete_all_redundant: false
            })
        });
        const data = await response.json();

        if (data.success) {
            const reclaimedStr = formatDupBytes(data.bytes_reclaimed || 0);
            const summaryMsg = permanentDelete
                ? `Successfully deleted ${data.count.toLocaleString()} duplicate files and freed ${reclaimedStr} of disk space!`
                : `Successfully quarantined ${data.count.toLocaleString()} duplicate files (${reclaimedStr}) into .Duplicates_Trash.\n\nNote: To actually reclaim disk space on a full drive, click 'Empty .Duplicates_Trash' when ready.`;
            const refusedMsg = (data.refused && data.refused.length > 0)
                ? `\n\nSkipped / Kept Safe (${data.refused.length}):\n${data.refused.slice(0, 15).join('\n')}` + (data.refused.length > 15 ? `\n...and ${data.refused.length - 15} more` : "")
                : "";
            alert(summaryMsg + refusedMsg);
            initDuplicateResults(data.remaining_results || []);
        } else {
            alert("Error: " + data.error);
        }
    } catch (e) {
        alert("Error during duplicate removal: " + e);
    } finally {
        if (permBtn) permBtn.disabled = false;
        if (quarBtn) quarBtn.disabled = false;
        try { lucide.createIcons(); } catch(e) {}
    }
}

async function emptyDuplicatesTrash() {
    const rootFolder = document.getElementById('dup-source-path').value;
    if (!rootFolder) {
        alert("Please select the scanned folder first.");
        return;
    }
    if (!confirm(`Permanently delete all files inside ${rootFolder}/.Duplicates_Trash to free disk space?`)) {
        return;
    }
    try {
        const response = await fetch('/api/dup_empty_trash', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ root_folder: rootFolder })
        });
        const data = await response.json();
        if (data.success) {
            if (data.files_deleted === 0) {
                alert("No .Duplicates_Trash folder or quarantined files found in this directory.");
            } else {
                alert(`Emptied .Duplicates_Trash: permanently deleted ${data.files_deleted.toLocaleString()} files and freed ${formatDupBytes(data.bytes_freed)}!`);
            }
        } else {
            alert("Error emptying .Duplicates_Trash: " + (data.error || "Unknown error"));
        }
    } catch (e) {
        alert("Error emptying .Duplicates_Trash: " + e);
    }
}

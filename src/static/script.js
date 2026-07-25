let currentDestMode = 'new';
let excludedProjects = [];
let currentInspectedPath = "";
let viewHistory = ['guide-view'];

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
            catsHtml += `<span class="category-pill">${cat}: <strong>${count}</strong></span>`;
        }
    }

    let projsHtml = "";
    if (summary.project_details && summary.project_details.length > 0) {
        projsHtml = `<div style="font-size: 11px; margin-top: 10px;"><strong>Intact Code Projects (${summary.total_projects}) - Click to inspect/re-sort:</strong><br>`;
        summary.project_details.forEach(p => {
            const isExcluded = excludedProjects.includes(p.path);
            const excClass = isExcluded ? 'excluded' : '';
            const excTxt = isExcluded ? ' (Excluded ⚡)' : '';
            projsHtml += `<button class="project-card-btn ${excClass}" onclick="inspectProject('${p.path.replace(/\\/g, '\\\\')}', '${p.name}')">📂 ${p.name}${excTxt}</button>`;
        });
        projsHtml += `</div>`;
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
        <div class="category-pills">
            ${catsHtml}
        </div>
        ${projsHtml}
        <div style="font-size: 11px; opacity: 0.7; margin-top: 8px; color: var(--success-color);">
            ✓ <strong>Safe Read-Only Preview:</strong> 0 files moved. Ready to organize.
        </div>
    `;
}

function updateProgressUI(data) {
    const fill = document.getElementById('progress-fill');
    const percentTxt = document.getElementById('progress-percentage');
    const countTxt = document.getElementById('progress-count');
    const msgTxt = document.getElementById('current-message');
    
    const percent = data.total > 0 ? Math.min(100, Math.round((data.progress / data.total) * 100)) : 0;
    
    fill.style.width = `${percent}%`;
    percentTxt.innerText = `${percent}%`;
    const etaText = data.eta ? ` • ${data.eta}` : '';
    countTxt.innerText = `${data.progress} / ${data.total}${etaText}`;
    msgTxt.innerText = data.message || "";
    
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
    document.getElementById('preview-mode').checked = false;
    document.getElementById('confirm-box').classList.add('hidden');
    document.getElementById('default-actions').style.display = "block";
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
    if (!confirm(`Are you sure you want to move ${duplicateSourcePaths.length} duplicate files on your SOURCE drive to macOS Trash?`)) {
        return;
    }
    try {
        const response = await fetch('/api/trash_duplicates', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ source_paths: duplicateSourcePaths })
        });
        const data = await response.json();
        alert(`Successfully moved ${data.count} duplicate files to Trash!`);
        loadDuplicateCleaner();
    } catch (e) {
        alert("Error moving files to trash: " + e);
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

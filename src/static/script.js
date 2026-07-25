let currentDestMode = 'new';

function selectDestMode(mode) {
    currentDestMode = mode;
    document.querySelectorAll('.option-card').forEach(card => card.classList.remove('active'));
    document.getElementById(`card-${mode}`).classList.add('active');
    
    const radio = document.querySelector(`input[name="dest-mode"][value="${mode}"]`);
    if (radio) radio.checked = true;
}

async function selectFolder(type) {
    const promptText = type === 'source' 
        ? "Select your messy SOURCE folder to organize:" 
        : "Select your DESTINATION folder (where organized files will go):";
        
    try {
        const response = await fetch(`/api/select_folder?prompt=${encodeURIComponent(promptText)}`);
        const data = await response.json();
        if (data.folder) {
            document.getElementById(`${type}-path`).value = data.folder;
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
                dest_mode: currentDestMode
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

function showView(viewId) {
    document.querySelectorAll('.view').forEach(v => v.classList.remove('active'));
    document.getElementById(viewId).classList.add('active');
}

function startPolling() {
    if (pollInterval) clearInterval(pollInterval);
    document.getElementById('done-btn').disabled = true;
    document.getElementById('status-heading').innerText = "Organizing Files...";
    document.getElementById('status-heading').style.color = "var(--text-color)";
    
    pollInterval = setInterval(async () => {
        try {
            const response = await fetch('/api/status');
            const data = await response.json();
            
            updateProgressUI(data);
            
            if (data.status === 'complete' || data.status === 'error') {
                clearInterval(pollInterval);
                finishOrganizing(data.status);
            }
        } catch (e) {
            console.error("Polling error", e);
        }
    }, 500);
}

function updateProgressUI(data) {
    const fill = document.getElementById('progress-fill');
    const percentTxt = document.getElementById('progress-percentage');
    const countTxt = document.getElementById('progress-count');
    const msgTxt = document.getElementById('current-message');
    const logContent = document.getElementById('log-content');
    
    const percent = data.total > 0 ? Math.min(100, Math.round((data.progress / data.total) * 100)) : 0;
    
    fill.style.width = `${percent}%`;
    percentTxt.innerText = `${percent}%`;
    countTxt.innerText = `${data.progress} / ${data.total}`;
    msgTxt.innerText = data.message || "";
    
    // Update logs
    logContent.innerHTML = data.logs.map(log => `<div>${log}</div>`).join('');
    
    // Auto scroll logs
    const logContainer = document.getElementById('log-container');
    logContainer.scrollTop = logContainer.scrollHeight;
}

let pollInterval = null;
let lastWasPreview = false;

function finishOrganizing(status) {
    const heading = document.getElementById('status-heading');
    const btn = document.getElementById('done-btn');
    const confirmBox = document.getElementById('confirm-box');
    const defaultActions = document.getElementById('default-actions');
    const reviewPanel = document.getElementById('review-panel');
    
    if (status === 'complete') {
        if (lastWasPreview) {
            heading.innerText = "Preview Complete!";
            heading.style.color = "var(--primary-color)";
            confirmBox.classList.remove('hidden');
            reviewPanel.classList.add('hidden');
            defaultActions.style.display = "none";
        } else {
            heading.innerText = "All Files Organized Successfully! 🚀";
            heading.style.color = "var(--success-color)";
            confirmBox.classList.add('hidden');
            reviewPanel.classList.remove('hidden');
            defaultActions.style.display = "block";
        }
    } else {
        heading.innerText = "Error Occurred";
        heading.style.color = "var(--error-color)";
        confirmBox.classList.add('hidden');
        reviewPanel.classList.add('hidden');
        defaultActions.style.display = "block";
    }
    
    btn.disabled = false;
    document.getElementById('progress-fill').style.width = "100%";
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

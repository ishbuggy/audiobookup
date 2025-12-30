// src/static/js/modules/job-manager.js

// --- State ---
let jobEventSource = null;
let currentJobId = null;
let isBusy = false;
let jobStartSource = null; // 'manual' or null

// --- DOM Elements ---
const processingPanel = document.getElementById("processing-panel");
const processingList = document.getElementById("processing-list");
const processingPanelHeader = document.querySelector("#processing-panel .panel-header");
const processingPanelTitle = processingPanelHeader.querySelector("h3");
const cancelJobBtn = document.getElementById("cancel-job-btn");
const clearReportBtn = document.getElementById("clear-report-btn");
const authWarningBanner = document.getElementById("auth-warning-banner");
const logOutput = document.getElementById("log-output");
const latestLogLine = document.getElementById("latest-log-line");

// --- Helper: Add Log Line ---
function addLogLine(text) {
    const now = new Date();
    const timeString = now.toLocaleTimeString('en-US', { hour12: false });
    const line = `[${timeString}] ${text}`;
    
    logOutput.textContent += line + "\n";
    logOutput.scrollTop = logOutput.scrollHeight;
    
    // Update the status bar footer, but keep it short (no timestamp)
    if (text.trim()) {
        latestLogLine.textContent = text;
    }
}

// --- Helper: UI Busy State ---
function setActionsBusy(busy) {
    isBusy = busy;
    const buttons = document.querySelectorAll(".action-button, .retry-button, #process-selected-btn");
    buttons.forEach((btn) => {
        btn.disabled = busy;
        if (!busy) {
            btn.classList.remove("loading");
            // Remove processing classes
            btn.classList.remove("is-processing");
            btn.classList.remove("is-automatic");
        }
    });
}

// --- Core: Toggle Panel ---
export function toggleProcessingPanel() {
    processingPanel.classList.toggle("open");
}

// --- Core: Initialize SSE ---
export function initializeSSEConnection(onJobFinishedCallback) {
    if (jobEventSource) {
        jobEventSource.close();
    }
    jobEventSource = new EventSource("/api/jobs/stream");

    jobEventSource.onopen = () => {
        console.log("Persistent SSE connection established.");
    };

    jobEventSource.addEventListener("job_started", (event) => {
        const jobData = JSON.parse(event.data);
        const jobTypeLower = jobData.job_type.toLowerCase();

        // Find the specific button that corresponds to this job type
        const targetButton = document.querySelector(`.action-button[data-script="${jobTypeLower}"]`);

        if (targetButton) {
            targetButton.classList.add("is-processing");
            let startMsg = `--- Job ${jobData.job_id} (${jobData.job_type}) Started ---`;
            
            if (jobStartSource !== "manual") {
                startMsg += " (Automatic)";
                targetButton.classList.add("is-automatic");
            }
            addLogLine(startMsg);

            // NEW: List the books being processed if available
            if (jobData.items && jobData.items.length > 0) {
                const titles = jobData.items.map(item => item.title).join(", ");
                addLogLine(`Queue: ${titles}`);
            }
        }

        jobStartSource = null;
        setActionsBusy(true);
        currentJobId = jobData.job_id;

        if (jobData.job_type === "SYNC" || jobData.job_type === "VERIFY") {
            rebuildSyncPanel(jobData);
        } else if (jobData.job_type === "DOWNLOAD") {
            rebuildProcessingPanel(jobData);
        }
    });

jobEventSource.addEventListener("job_update", (event) => {
        const data = JSON.parse(event.data);
        const item = processingList.querySelector(`.processing-item[data-asin="${data.asin}"]`);
        
        if (item) {
            // Update Sync Stage text if applicable
            if (data.stage_text) {
                const stageElement = document.getElementById("sync-stage-text");
                if (stageElement) stageElement.textContent = data.stage_text;
            }

            // Update Status Text and Progress Bar
            item.querySelector(".status-text").textContent = data.status_text;
            item.querySelector(".progress-bar-inner").style.width = `${data.progress}%`;
            
            const title = item.querySelector('.processing-item-title').textContent;

            // --- LOGGING LOGIC ---
            
            // 1. Log "Processing..." when the download starts (progress 5%)
            if (data.status_text === "Downloading..." && !item.classList.contains("processing-started")) {
                addLogLine(`Processing: ${title}...`);
                item.classList.add("processing-started"); // Prevent duplicate logs
            }

            // 2. Log Completion/Failure based on the flag we just added in the backend
            if (data.final_status === "success") {
                item.classList.add("success");
                addLogLine(`✓ Completed: ${title}`);
            }
            else if (data.final_status === "error") {
                item.classList.add("error");
                addLogLine(`✗ Failed: ${title}`);
            }
        }
    });

    jobEventSource.addEventListener("job_finished", (event) => {
        const data = JSON.parse(event.data);
        addLogLine(`--- Job ${data.job_id} Finished. Status: ${data.status} ---`);

        if (data.job_type === "DOWNLOAD") {
            data.items.forEach((finalItem) => {
                const itemElement = processingList.querySelector(`.processing-item[data-asin="${finalItem.asin}"]`);
                if (itemElement) {
                    itemElement.classList.remove("success", "error", "cancelled");
                    switch (finalItem.status) {
                        case "COMPLETED": itemElement.classList.add("success"); break;
                        case "FAILED": itemElement.classList.add("error"); break;
                        case "CANCELLED":
                            itemElement.classList.add("cancelled");
                            itemElement.querySelector(".status-text").textContent = "Cancelled";
                            break;
                    }
                }
            });
        } else if (data.job_type === "SYNC") {
            processingList.innerHTML = "";
        }

        // Reset UI
        document.querySelectorAll(".action-button").forEach((btn) => {
            btn.classList.remove("is-processing", "is-automatic");
        });

        const panelTitle = document.getElementById("processing-panel-title");
        if (panelTitle) panelTitle.innerHTML = `Job Status`;

        processingPanelTitle.textContent = `Job ${data.job_id} Finished (${data.status})`;
        processingPanelHeader.removeEventListener("click", toggleProcessingPanel);
        processingPanelHeader.style.cursor = "default";

        cancelJobBtn.style.display = "none";
        cancelJobBtn.disabled = false;
        cancelJobBtn.textContent = "Cancel Job";
        clearReportBtn.style.display = "inline-block";

        currentJobId = null;
        
        setTimeout(() => {
            setActionsBusy(false);
            if (onJobFinishedCallback) onJobFinishedCallback(); // Refresh library callback
        }, 1000);
    });
}

// --- Panel Rebuilders ---
function rebuildSyncPanel(jobData) {
    currentJobId = jobData.job_id;
    document.getElementById("cancel-job-btn").style.display = "inline-block";
    
    // Determine labels based on job type
    let title = "Library Synchronization";
    let asin = "sync-job";
    
    if (jobData.job_type === "VERIFY") {
        title = "Library Integrity Check";
        asin = "verify-job";
    }

    processingList.innerHTML = `
    <div class="processing-item" data-asin="${asin}">
        <div class="processing-item-info">
            <p class="processing-item-title">${title}</p>
            <p class="processing-item-author" id="sync-stage-text">Processing...</p>
        </div>
        <div class="processing-item-status">
            <p class="status-text">Initializing...</p>
            <div class="progress-bar">
                <div class="progress-bar-inner" style="width: 0%;"></div>
            </div>
            <div class="status-icon success"><i class="fas fa-check-circle"></i></div>
            <div class="status-icon error"><i class="fas fa-times-circle"></i></div>
        </div>
    </div>`;
    processingPanel.classList.add("open");
}

function rebuildProcessingPanel(jobData) {
    currentJobId = jobData.job_id;
    document.getElementById("cancel-job-btn").style.display = "inline-block";
    processingList.innerHTML = "";

    jobData.items.forEach((book) => {
        const item = document.createElement("div");
        item.className = "processing-item";
        item.setAttribute("data-asin", book.asin);

        let statusText = "Queued...";
        let progress = 0;
        let itemClass = "";

        switch (book.status) {
            case "PROCESSING": statusText = "Processing..."; progress = 25; break;
            case "COMPLETED": statusText = "Complete!"; progress = 100; itemClass = "success"; break;
            case "FAILED": statusText = "Failed!"; progress = 100; itemClass = "error"; break;
        }
        if (itemClass) item.classList.add(itemClass);

        item.innerHTML = `
        <img class="processing-item-thumb" src="${book.cover_url}" alt="Cover">
        <div class="processing-item-info">
            <p class="processing-item-title">${book.title}</p>
            <p class="processing-item-author">${book.author}</p>
        </div>
        <div class="processing-item-status">
            <p class="status-text">${statusText}</p>
            <div class="progress-bar">
                <div class="progress-bar-inner" style="width: ${progress}%;"></div>
            </div>
            <div class="status-icon success"><i class="fas fa-check-circle"></i></div>
            <div class="status-icon error"><i class="fas fa-times-circle"></i></div>
            <div class="status-icon cancelled"><i class="fas fa-ban"></i></div>
        </div>
    `;
        processingList.appendChild(item);
    });

    processingPanel.classList.add("open");
}

// --- API Actions ---
export async function checkForActiveJob() {
    try {
        const response = await fetch("/api/jobs/active");
        if (!response.ok) throw new Error("Failed to fetch active job status.");

        const jobData = await response.json();
        if (jobData && jobData.job_id) {
            console.log(`Found active job ${jobData.job_id} (${jobData.job_type}) on page load.`);
            addLogLine(`--- Reconnected to active job ${jobData.job_id}. ---`);
            setActionsBusy(true);
            if (jobData.job_type === "SYNC") rebuildSyncPanel(jobData);
            else if (jobData.job_type === "DOWNLOAD") rebuildProcessingPanel(jobData);
        }
    } catch (error) {
        console.error("Error checking for active job:", error);
    }
}

export async function startJob(job_type, asins = [], clickedButton = null, job_params = null) {
    if (isBusy) {
        window.showCustomAlert("An operation is already in progress.");
        return;
    }
    jobStartSource = "manual";
    setActionsBusy(true);

    try {
        const payload = { job_type };
        if (asins && asins.length > 0) payload.asins = asins;
        if (job_params) payload.job_params = job_params;

        const response = await fetch("/api/jobs/start", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify(payload),
        });
        const data = await response.json();
        if (!response.ok || !data.success) {
            throw new Error(data.error || `Server responded with status: ${response.status}`);
        }
    } catch (error) {
        console.error(`Error starting ${job_type} job:`, error);
        window.showCustomAlert(`Could not start the ${job_type} job. Please check the application log.`);
        setActionsBusy(false);
    }
}

export function startSyncJob(clickedButton) {
    const job_params = { sync_mode: "DEEP" };
    startJob("SYNC", null, clickedButton, job_params);
}

// --- Public Panel Helpers (for other modules) ---
export function openProcessingPanel(selectedBooks) {
    processingList.innerHTML = "";
    if (selectedBooks) {
        selectedBooks.forEach((book) => {
            const item = document.createElement("div");
            item.className = "processing-item";
            item.setAttribute("data-asin", book.asin);
            item.innerHTML = `
            <img class="processing-item-thumb" src="${book.cover_url}" alt="Cover">
            <div class="processing-item-info">
                <p class="processing-item-title">${book.title}</p>
                <p class="processing-item-author">${book.author}</p>
            </div>
            <div class="processing-item-status">
                <p class="status-text">Queued...</p>
                <div class="progress-bar">
                    <div class="progress-bar-inner" style="width: 0%;"></div>
                </div>
                <div class="status-icon success"><i class="fas fa-check-circle"></i></div>
                <div class="status-icon error"><i class="fas fa-times-circle"></i></div>
                <div class="status-icon cancelled"><i class="fas fa-ban"></i></div>
            </div>
        `;
            processingList.appendChild(item);
        });
    }
    processingPanel.classList.add("open");
}

// --- Event Listeners (Internal) ---
// Setup internal listeners for buttons that live permanently on the page
document.addEventListener("DOMContentLoaded", () => {
    // Cancel Button
    cancelJobBtn.addEventListener("click", async (event) => {
        event.stopPropagation();
        if (!currentJobId) return;

        cancelJobBtn.disabled = true;
        cancelJobBtn.textContent = "Cancelling...";

        try {
            const response = await fetch("/api/jobs/cancel", { method: "POST" });
            const data = await response.json();
            if (data.success) {
                addLogLine(`--- Cancel signal sent for job ${currentJobId}. ---`);
            } else {
                throw new Error(data.error || "Failed to send cancel signal.");
            }
        } catch (error) {
            console.error("Failed to cancel job:", error);
            cancelJobBtn.disabled = false;
            cancelJobBtn.textContent = "Cancel Job";
        }
    });

    // Clear Report Button
    clearReportBtn.addEventListener("click", (event) => {
        event.stopPropagation();
        const finishedItems = processingList.querySelectorAll(".success, .error, .cancelled");
        finishedItems.forEach((item) => item.remove());
        clearReportBtn.style.display = "none";

        if (processingList.children.length === 0) {
            processingPanel.classList.remove("open");
            processingPanelTitle.textContent = "Job Status";
            processingPanelHeader.addEventListener("click", toggleProcessingPanel);
            processingPanelHeader.style.cursor = "pointer";
        } else {
            processingPanelTitle.textContent = "Job Status";
        }
    });

    // Header Toggle
    processingPanelHeader.addEventListener("click", toggleProcessingPanel);
});
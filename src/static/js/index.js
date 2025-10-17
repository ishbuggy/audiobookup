
// --- Element References & Initial State ---
const logOutput = document.getElementById("log-output");
const latestLogLine = document.getElementById("latest-log-line");
const searchBar = document.getElementById("search-bar");
const sortBy = document.getElementById("sort-by");
const filterByStatus = document.getElementById("filter-by-status");

// Processing Panel Elements
const processingPanel = document.getElementById("processing-panel");
const processingList = document.getElementById("processing-list");
const processingPanelHeader = document.querySelector("#processing-panel .panel-header");
const processingPanelTitle = processingPanelHeader.querySelector("h3");
const cancelJobBtn = document.getElementById("cancel-job-btn");
const clearReportBtn = document.getElementById("clear-report-btn");
const authWarningBanner = document.getElementById("auth-warning-banner");

let libraryData = [];
let jobEventSource = null; // New eventSource for background jobs
let currentJobId = null;
let isBusy = false;
let jobStartSource = null; // Will be 'manual' or null

function setActionsBusy(busy) {
    isBusy = busy;
    // Select all buttons that can start a major action
    const buttons = document.querySelectorAll(".action-button, .retry-button, #process-selected-btn");
    buttons.forEach((btn) => {
        btn.disabled = busy;
        if (!busy) {
            // When un-setting busy, remove loading spinners from all buttons
            btn.classList.remove("loading");
        }
    });
}

// --- Main UI Initialization ---
document.addEventListener("DOMContentLoaded", () => {
    checkAuthStatus();
    checkAutomationStatus();
    checkForActiveJob();
    initializeSSEConnection();
    fetchUpdates();
    initializeLazyLoading();
    document.getElementById("fetch-full-summary-btn").addEventListener("click", handleFetchFullSummary);
    searchBar.addEventListener("input", renderLibraryGrid);
    sortBy.addEventListener("change", renderLibraryGrid);
    filterByStatus.addEventListener("change", renderLibraryGrid);
});

function showInstructionsAlert(title, message) {
    document.getElementById("custom-alert-title").innerHTML = title; // Use innerHTML for icons
    document.getElementById("custom-alert-message").innerHTML = message; // Use innerHTML for formatting
    document.body.classList.add("modal-open");
    document.getElementById("custom-alert-modal").style.display = "flex";
}

// Authorization health check
async function checkAuthStatus() {
    try {
        const response = await fetch("/api/audible_auth_status");
        const data = await response.json();

        if (!data.is_valid) {
            document.getElementById("auth-warning-message").textContent = data.error;
            authWarningBanner.style.display = "flex";

            // Use addEventListener for more robust event handling.
            // The { once: true } option ensures the listener is automatically removed
            // after it's clicked once, preventing multiple attachments.
            document.getElementById("re-auth-btn").addEventListener(
                "click",
                () => {
                    showConfirmationModal(
                        '<i class="fas fa-shield-alt"></i> Reset Authentication?',
                        "This will delete your current Audible login credentials and force a full restart of the application. Are you sure you want to proceed?",
                        handleResetAuth,
                    );
                },
                { once: true },
            );
        }
    } catch (error) {
        console.error("Auth check failed:", error);
    }
}

// --- Automation Status Banner Logic ---
async function checkAutomationStatus() {
    try {
        const response = await fetch("/api/settings");
        const settings = await response.json();
        const banner = document.getElementById("automation-status-banner");
        const bannerText = document.getElementById("automation-status-text");

        // Create a list to hold the names of disabled tasks.
        const disabledTasks = [];

        // A user considers sync "enabled" if either Fast or Deep sync is on.
        if (!settings.tasks.is_auto_fast_sync_enabled && !settings.tasks.is_auto_deep_sync_enabled) {
            disabledTasks.push("library sync");
        }

        // (Future-proofing) When auto-download is added, uncomment the following:
        if (!settings.tasks.is_auto_process_enabled) {
            disabledTasks.push("download processing");
        }

        // Only show the banner if at least one task is disabled.
        if (disabledTasks.length > 0) {
            let message = "";
            // Create a human-readable list of the disabled tasks.
            if (disabledTasks.length === 1) {
                message = `Automatic <strong>${disabledTasks[0]}</strong> is disabled.`;
            } else {
                // This handles multiple disabled tasks gracefully, e.g., "sync and downloads"
                const lastTask = disabledTasks.pop();
                message = `Automatic <strong>${disabledTasks.join(", ")}</strong> and <strong>${lastTask}</strong> are disabled.`;
            }

            bannerText.innerHTML = `${message} Click here to configure automation.`;
            banner.style.display = "block";
            banner.onclick = () => {
                window.location.href = "/settings#tasks";
            };
        } else {
            // If no tasks are disabled, ensure the banner is hidden.
            banner.style.display = "none";
        }
    } catch (error) {
        console.error("Could not check automation status:", error);
    }
}

// --- Function to handle the reset and shutdown sequence ---
async function handleResetAuth() {
    // Step 1: Show an informational alert that the process has started.
    showInstructionsAlert(
        '<i class="fas fa-spinner fa-spin"></i> Processing...',
        "Resetting authentication... Please wait.",
    );

    try {
        // Step 2: Call the backend to reset authentication files.
        const response = await fetch("/api/reset_authentication", { method: "POST" });
        const data = await response.json();

        if (!response.ok || !data.success) {
            throw new Error(data.error || "Failed to reset authentication on the server.");
        }

        // Step 3: On success, update the alert and trigger the shutdown.
        document.getElementById("custom-alert-message").innerHTML =
            "Authentication has been reset. The application will now restart. You will be redirected to the setup page in a few moments.";
        // We don't want the user to be able to close this final message.
        document.getElementById("custom-alert-ok-btn").style.display = "none";

        // Trigger shutdown after a short delay to allow the user to read the message.
        setTimeout(triggerShutdown, 3000);
    } catch (error) {
        console.error("Reset authentication failed:", error);
        // Update the alert to show the error message.
        document.getElementById("custom-alert-title").innerHTML =
            '<i class="fas fa-times-circle"></i> Error';
        document.getElementById("custom-alert-message").textContent =
            `Could not reset authentication: ${error.message}`;
    }
}

async function triggerShutdown() {
    try {
        // This call will likely not receive a response as the server shuts down immediately.
        await fetch("/internal/shutdown", { method: "POST" });
    } catch (error) {
        // An error is expected here as the connection is cut. We can ignore it.
        console.log("Shutdown signal sent. The server is restarting.");
    }

    // After shutdown, we wait a bit then start trying to reload the page.
    setTimeout(() => {
        document.getElementById("custom-alert-message").innerHTML =
            "Waiting for application to restart... Reloading the page automatically...";

        // Try to reload every 5 seconds until it succeeds.
        setInterval(() => {
            window.location.reload();
        }, 5000);
    }, 5000); // Initial 5-second delay before starting to poll
}

// --- Real-time Job Streaming Logic ---
function initializeSSEConnection() {
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
            // Add the base processing class to the button
            targetButton.classList.add("is-processing");

            // If the job was started automatically, add the automatic class
            if (jobStartSource !== "manual") {
                addLogLine(
                    `--- New job ${jobData.job_id} (${jobData.job_type}) started automatically. ---`,
                );
                targetButton.classList.add("is-automatic");
            } else {
                addLogLine(`--- New job ${jobData.job_id} (${jobData.job_type}) started. ---`);
            }
        }

        jobStartSource = null; // Reset the flag
        setActionsBusy(true);
        currentJobId = jobData.job_id;

        if (jobData.job_type === "SYNC") {
            rebuildSyncPanel(jobData);
        } else if (jobData.job_type === "DOWNLOAD") {
            rebuildProcessingPanel(jobData);
        }
    });

    jobEventSource.addEventListener("job_update", (event) => {
        const data = JSON.parse(event.data);
        const item = processingList.querySelector(`.processing-item[data-asin="${data.asin}"]`);
        if (item) {
            // Check for and update the stage text for sync jobs
            if (data.stage_text) {
                const stageElement = document.getElementById("sync-stage-text");
                if (stageElement) {
                    stageElement.textContent = data.stage_text;
                }
            }
            item.querySelector(".status-text").textContent = data.status_text;
            item.querySelector(".progress-bar-inner").style.width = `${data.progress}%`;
            if (data.final_status === "success") {
                item.classList.add("success");
            } else if (data.final_status === "error") {
                item.classList.add("error");
            }
        }
    });

    jobEventSource.addEventListener("job_finished", (event) => {
        const data = JSON.parse(event.data);
        addLogLine(
            `--- Job ${data.job_id} finished with status: ${data.status}. Refreshing library... ---`,
        );

        if (data.job_type === "DOWNLOAD") {
            // Use the authoritative item statuses from the event payload.
            data.items.forEach((finalItem) => {
                const itemElement = processingList.querySelector(
                    `.processing-item[data-asin="${finalItem.asin}"]`,
                );
                if (itemElement) {
                    // Remove any existing status classes to ensure a clean state.
                    itemElement.classList.remove("success", "error", "cancelled");

                    // Apply the correct final class based on the payload.
                    switch (finalItem.status) {
                        case "COMPLETED":
                            itemElement.classList.add("success");
                            break;
                        case "FAILED":
                            itemElement.classList.add("error");
                            break;
                        case "CANCELLED":
                            itemElement.classList.add("cancelled");
                            itemElement.querySelector(".status-text").textContent = "Cancelled";
                            break;
                    }
                }
            });
        } else if (data.job_type === "SYNC") {
            // Sync jobs don't have items, just clear the panel.
            processingList.innerHTML = "";
        }

        // The rest of the logic for updating the panel header and buttons is correct and remains.
        document.querySelectorAll(".action-button").forEach((btn) => {
            btn.classList.remove("is-processing");
            btn.classList.remove("is-automatic");
        });

        const panelTitle = document.getElementById("processing-panel-title");
        if (panelTitle) {
            panelTitle.innerHTML = `Job Status`;
        }

        processingPanelTitle.textContent = `Job ${data.job_id} Finished (${data.status})`;
        processingPanelHeader.removeEventListener("click", toggleProcessingPanel);
        processingPanelHeader.style.cursor = "default";

        cancelJobBtn.style.display = "none";
        cancelJobBtn.disabled = false;
        cancelJobBtn.textContent = "Cancel Job";
        clearReportBtn.style.display = "inline-block";

        currentJobId = null;

        setTimeout(() => {
            fetchUpdates();
            setActionsBusy(false);
        }, 1000);
    });

    jobEventSource.onerror = (err) => {
        console.error("Job EventSource failed:", err);
    };
}

// --- MODIFIED: Functions to check for jobs and connect to stream ---
async function checkForActiveJob() {
    try {
        const response = await fetch("/api/jobs/active");
        if (!response.ok) throw new Error("Failed to fetch active job status.");

        const jobData = await response.json();

        if (jobData && jobData.job_id) {
            console.log(`Found active job ${jobData.job_id} of type ${jobData.job_type} on page load.`);
            addLogLine(`--- Reconnected to active job ${jobData.job_id}. ---`);
            setActionsBusy(true);

            // Route to the correct UI builder based on job type
            if (jobData.job_type === "SYNC") {
                rebuildSyncPanel(jobData);
            } else if (jobData.job_type === "DOWNLOAD") {
                rebuildProcessingPanel(jobData);
            }
        }
    } catch (error) {
        console.error("Error checking for active job:", error);
    }
}

function rebuildSyncPanel(jobData) {
    currentJobId = jobData.job_id;
    document.getElementById("cancel-job-btn").style.display = "none";
    // --- CORRECTED HTML STRUCTURE ---
    processingList.innerHTML = `
    <div class="processing-item" data-asin="sync-job">
        <div class="processing-item-info">
            <p class="processing-item-title">Library Synchronization</p>
            <p class="processing-item-author" id="sync-stage-text">Reconnected to active job...</p>
        </div>
        <div class="processing-item-status">
            <p class="status-text">Reconnecting...</p>
            <div class="progress-bar">
                <div class="progress-bar-inner" style="width: 0%;"></div>
            </div>
            <!-- Add the final status icons for consistency -->
            <div class="status-icon success"><i class="fas fa-check-circle"></i></div>
            <div class="status-icon error"><i class="fas fa-times-circle"></i></div>
        </div>
    </div>`;
    processingPanel.classList.add("open");
}

function rebuildProcessingPanel(jobData) {
    currentJobId = jobData.job_id;
    // For download jobs, the cancel button should be visible.
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
            case "PROCESSING":
                statusText = "Processing...";
                progress = 25;
                break;
            case "COMPLETED":
                statusText = "Complete!";
                progress = 100;
                itemClass = "success";
                break;
            case "FAILED":
                statusText = "Failed!";
                progress = 100;
                itemClass = "error";
                break;
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

// --- Library Search & Sort ---
function renderLibraryGrid() {
    let booksToDisplay = [...libraryData];
    const searchTerm = searchBar.value.toLowerCase();
    const sortValue = sortBy.value;
    const statusFilter = filterByStatus.value;

    // 1. Apply Search Filter
    if (searchTerm) {
        booksToDisplay = booksToDisplay.filter((book) => {
            const title = book.title ? book.title.toLowerCase() : "";
            const author = book.author ? book.author.toLowerCase() : "";
            const narrator = book.narrator ? book.narrator.toLowerCase() : "";
            return (
                title.includes(searchTerm) || author.includes(searchTerm) || narrator.includes(searchTerm)
            );
        });
    }

    // 2. NEW: Apply Status Filter
    if (statusFilter) {
        // Only filter if a status is selected
        booksToDisplay = booksToDisplay.filter((book) => book.status === statusFilter);
    }
    // 3. Apply Sorting
    switch (sortValue) {
        case "author_asc":
            booksToDisplay.sort((a, b) => a.author.localeCompare(b.author));
            break;
        case "author_desc":
            booksToDisplay.sort((a, b) => b.author.localeCompare(a.author));
            break;
        case "title_asc":
            booksToDisplay.sort((a, b) => a.title.localeCompare(b.title));
            break;
        case "title_desc":
            booksToDisplay.sort((a, b) => b.title.localeCompare(a.title));
            break;
        case "release_date_desc":
            booksToDisplay.sort((a, b) => new Date(b.release_date) - new Date(a.release_date));
            break;
        case "release_date_asc":
            booksToDisplay.sort((a, b) => new Date(a.release_date) - new Date(b.release_date));
            break;
        case "date_added_desc":
            booksToDisplay.sort((a, b) => new Date(b.date_added) - new Date(a.date_added));
            break;
        case "date_added_asc":
            booksToDisplay.sort((a, b) => new Date(a.date_added) - new Date(b.date_added));
            break;
    }

    // 4. Render the final list
    updateLibraryTable(booksToDisplay);
}

// --- Collapsible Processing Panel Logic ---
function openProcessingPanel(selectedBooks) {
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
function toggleProcessingPanel() {
    processingPanel.classList.toggle("open");
}
processingPanelHeader.addEventListener("click", toggleProcessingPanel);

// --- Cancel Job Button Logic ---
cancelJobBtn.addEventListener("click", async (event) => {
    // This stops the click from also toggling the panel's collapse state
    event.stopPropagation();

    if (!currentJobId) {
        showCustomAlert("No active job to cancel.");
        return;
    }

    cancelJobBtn.disabled = true;
    cancelJobBtn.textContent = "Cancelling...";

    try {
        const response = await fetch("/api/jobs/cancel", { method: "POST" });
        if (!response.ok) throw new Error("Server returned an error during cancellation.");

        const data = await response.json();
        if (data.success) {
            addLogLine(
                `--- Cancel signal sent for job ${currentJobId}. The job will stop after the current book finishes. ---`,
            );
            // No further UI changes are needed here; the 'job_finished' event
            // from the SSE stream will handle the final cleanup.
        } else {
            throw new Error(data.error || "Failed to send cancel signal.");
        }
    } catch (error) {
        console.error("Failed to cancel job:", error);
        showCustomAlert("Could not send the cancel signal. Please check the application log.");
        cancelJobBtn.disabled = false; // Re-enable on failure
        cancelJobBtn.textContent = "Cancel Job";
    }
});

// --- Clear Job Report Button Logic ---
clearReportBtn.addEventListener("click", (event) => {
    event.stopPropagation();

    // 1. Select only the items that are marked as finished.
    const finishedItems = processingList.querySelectorAll(".success, .error, .cancelled");

    // 2. Remove only those specific items from the list.
    finishedItems.forEach((item) => item.remove());

    // 3. Hide the "Clear Finished" button now that its job is done.
    clearReportBtn.style.display = "none";

    // 4. Check if there are any items left in the panel (i.e., active jobs).
    if (processingList.children.length === 0) {
        // If the panel is now completely empty, close it and fully reset the header.
        processingPanel.classList.remove("open");
        processingPanelTitle.textContent = "Job Status";
        processingPanelHeader.addEventListener("click", toggleProcessingPanel);
        processingPanelHeader.style.cursor = "pointer";
    } else {
        // If active items remain, just reset the title to be neutral.
        // The header remains unclickable because it's still in a "job active" state.
        processingPanelTitle.textContent = "Job Status";
    }
});

// --- Book Detail Modal Logic ---
const bookDetailModal = document.getElementById("book-detail-modal");
const detailModalCloseBtn = document.getElementById("detail-modal-close");
const libraryGrid = document.getElementById("library-grid");
libraryGrid.addEventListener("click", async (event) => {
    const card = event.target.closest(".book-card");
    if (card && !event.target.matches("button.retry-button")) {
        const asin = card.dataset.asin;
        if (!asin) return;
        try {
            const response = await fetch(`/api/book/${asin}`);
            if (!response.ok) throw new Error("Failed to fetch book details.");
            const book = await response.json();
            document.getElementById("modal-book-cover").src = book.cover_url_original || "";
            document.getElementById("modal-book-title").textContent = book.title || "N/A";
            document.getElementById("modal-book-author").textContent = book.author || "N/A";
            document.getElementById("modal-book-narrator").textContent = book.narrator || "N/A";
            document.getElementById("modal-book-series").textContent = book.series || "N/A";
            document.getElementById("modal-book-runtime").textContent = book.runtime_min || "N/A";
            document.getElementById("modal-book-release-date").textContent = book.release_date || "N/A";
            document.getElementById("modal-book-asin").textContent = book.asin || "N/A";
            document.getElementById("modal-book-status").textContent = book.status || "N/A";
            document.getElementById("modal-book-publisher").textContent = book.publisher || "N/A";
            let formattedDateAdded = "N/A";
            if (book.date_added && book.date_added !== "N/A") {
                formattedDateAdded = book.date_added.split("T")[0];
            }
            document.getElementById("modal-book-date-added").textContent = formattedDateAdded;
            document.getElementById("modal-book-language").textContent = book.language || "N/A";
            document.getElementById("modal-book-summary").textContent =
                book.summary || "No summary available.";
            document.getElementById("modal-file-path").textContent = book.filepath || "N/A";
            document.getElementById("modal-file-type").textContent = book.file_type || "N/A";
            document.getElementById("modal-file-size").textContent = book.file_size_hr || "N/A";
            document.getElementById("modal-file-mtime").textContent = book.file_mtime_hr || "N/A";
            const errorDetailsDiv = document.getElementById("modal-error-details");
            const errorPre = document.getElementById("modal-book-error");
            if (book.error_message && book.error_message.trim() !== "") {
                errorPre.textContent = book.error_message;
                errorDetailsDiv.style.display = "block";
            } else {
                errorDetailsDiv.style.display = "none";
            }
            const fetchSummaryBtn = document.getElementById("fetch-full-summary-btn");
            // The 'is_summary_full' flag is 0 for truncated, 1 for full.
            if (book.is_summary_full === 0) {
                // If the summary is truncated, show the button and set its ASIN.
                fetchSummaryBtn.style.display = "inline-block";
                fetchSummaryBtn.dataset.asin = asin;
            } else {
                // If the summary is already full, hide the button.
                fetchSummaryBtn.style.display = "none";
            }
            document.body.classList.add("modal-open");
            bookDetailModal.style.display = "flex";
        } catch (error) {
            console.error("Error fetching book details:", error);
            showCustomAlert("Could not load book details.");
        }
    }
});
function closeDetailModal() {
    document.body.classList.remove("modal-open");
    bookDetailModal.style.display = "none";
}
detailModalCloseBtn.onclick = closeDetailModal;

// --- Full Summary Button Logic ---
async function handleFetchFullSummary(event) {
    const btn = event.currentTarget;
    const asin = btn.dataset.asin;
    if (!asin) return;
    btn.classList.add("loading");
    btn.disabled = true;
    btn.textContent = "Fetching...";
    try {
        const response = await fetch(`/api/fetch_full_summary/${asin}`, { method: "POST" });
        if (!response.ok) throw new Error(`Server responded with status: ${response.status}`);
        const data = await response.json();
        if (data.success) {
            document.getElementById("modal-book-summary").textContent = data.summary;
            btn.style.display = "none";
        } else {
            throw new Error(data.error || "Unknown error from server.");
        }
    } catch (error) {
        console.error("Failed to fetch full summary:", error);
        showCustomAlert("Could not fetch the full summary. Please check the application log.");
    } finally {
        btn.classList.remove("loading");
        btn.disabled = false;
        btn.textContent = "Get Full Summary";
    }
}

// --- Download Selection Modal Logic ---
const downloadSelectionModal = document.getElementById("download-selection-modal");
const selectionModalCloseBtn = document.getElementById("selection-modal-close");
const selectionBookList = document.getElementById("selection-book-list");
const selectAllBtn = document.getElementById("select-all-btn");
const selectNoneBtn = document.getElementById("select-none-btn");
const processSelectedBtn = document.getElementById("process-selected-btn");
const selectionCountSpan = document.getElementById("selection-count");
function updateSelectionCount() {
    const count = selectionBookList.querySelectorAll('input[type="checkbox"]:checked').length;
    selectionCountSpan.textContent = count;
}
async function openDownloadSelectionModal() {
    selectionBookList.innerHTML = "<p>Loading books...</p>";
    document.body.classList.add("modal-open");
    downloadSelectionModal.style.display = "flex";

    /**
     * Helper function to render a list of books under a styled heading.
     * @param {Array} books - The array of book objects to render.
     * @param {string} title - The heading title for this category.
     * @param {HTMLElement} container - The parent element to append to.
     */
    const renderCategory = (books, title, container) => {
        if (!books || books.length === 0) {
            return;
        }

        const header = document.createElement("h4");
        header.textContent = title;
        header.style.marginTop = "1em";
        header.style.marginBottom = "0.5em";
        header.style.borderBottom = "1px solid #e9ecef";
        header.style.paddingBottom = "0.5em";
        container.appendChild(header);

        books.forEach((book) => {
            const div = document.createElement("div");
            div.className = "selection-book-item";
            // --- START: CORRECTED HTML STRUCTURE ---
            // The checkbox is now placed correctly, and the label wraps the clickable content.
            div.innerHTML = `
                <img class="selection-book-thumb lazy-load" src="data:image/gif;base64,R0lGODlhAQABAIAAAAAAAP///yH5BAEAAAAALAAAAAABAAEAAAIBRAA7" data-src="/covers/${book.asin}_thumb.jpg" alt="Cover">
                <input type="checkbox" id="asin-${book.asin}" value="${book.asin}">
                <label for="asin-${book.asin}" class="selection-book-info">
                    <span class="title">${book.title}</span>
                    <span class="author">by ${book.author}</span>
                </label>
            `;
            // --- END: CORRECTED HTML STRUCTURE ---
            container.appendChild(div);
        });
    };

    try {
        const response = await fetch("/api/downloadable_books");
        const data = await response.json();

        if (data.new.length === 0 && data.missing.length === 0 && data.errored.length === 0) {
            selectionBookList.innerHTML =
                "<p>No new, missing, or errored books are available to process.</p>";
            updateSelectionCount();
            return;
        }

        selectionBookList.innerHTML = "";
        renderCategory(data.new, "New Books", selectionBookList);
        renderCategory(data.missing, "Missing Books (Files not found)", selectionBookList);
        renderCategory(data.errored, "Books with Errors (Manual Retry)", selectionBookList);

        // --- START: THE CRITICAL FIX FOR COVER ART ---
        // After rendering all the new images, we must tell the lazy loader to watch them.
        initializeLazyLoading();
        // --- END: THE CRITICAL FIX FOR COVER ART ---

        updateSelectionCount();
    } catch (error) {
        console.error("Failed to fetch downloadable books:", error);
        selectionBookList.innerHTML = "<p>Error loading book list. Please try again.</p>";
    }
}
function closeSelectionModal() {
    document.body.classList.remove("modal-open");
    downloadSelectionModal.style.display = "none";
}
selectionModalCloseBtn.onclick = closeSelectionModal;
selectAllBtn.onclick = () => {
    selectionBookList.querySelectorAll('input[type="checkbox"]').forEach((cb) => (cb.checked = true));
    updateSelectionCount();
};
selectNoneBtn.onclick = () => {
    selectionBookList.querySelectorAll('input[type="checkbox"]').forEach((cb) => (cb.checked = false));
    updateSelectionCount();
};
selectionBookList.addEventListener("change", updateSelectionCount);

// MODIFIED: This now calls the new job API and connects to the stream
processSelectedBtn.onclick = async () => {
    const selectedASINs = Array.from(selectionBookList.querySelectorAll("input:checked")).map(
        (cb) => cb.value,
    );
    if (selectedASINs.length === 0) {
        showCustomAlert("Please select at least one book to process.");
        return;
    }
    const selectedBooks = libraryData.filter((book) => selectedASINs.includes(book.asin));
    openProcessingPanel(selectedBooks);
    closeSelectionModal();
    // Call the generic job starter
    startJob("DOWNLOAD", selectedASINs);
};

// --- Lazy Loading Logic ---
const lazyLoadObserver = new IntersectionObserver((entries, observer) => {
    entries.forEach((entry) => {
        if (entry.isIntersecting) {
            const img = entry.target;
            img.src = img.dataset.src;
            img.classList.remove("lazy-load");
            observer.unobserve(img);
        }
    });
});
function initializeLazyLoading() {
    document.querySelectorAll(".lazy-load").forEach((img) => {
        lazyLoadObserver.observe(img);
    });
}

// --- UI Update Functions ---
function addLogLine(text) {
    logOutput.textContent += text + "\n";
    logOutput.scrollTop = logOutput.scrollHeight;
    if (text.trim()) {
        latestLogLine.textContent = text;
    }
}
function updateStats(stats) {
    document.getElementById("stats-downloaded").textContent = stats.downloaded || 0;
    document.getElementById("stats-new").textContent = stats.new || 0;
    document.getElementById("stats-missing").textContent = stats.missing || 0;
    document.getElementById("stats-error").textContent = stats.error || 0;
}

function updateLibraryTable(books) {
    const grid = document.getElementById("library-grid");
    grid.innerHTML = "";
    if (books.length === 0) {
        grid.innerHTML =
            "<p>No books found matching your criteria. Try adjusting your search or sort options.</p>";
        return;
    }
    books.forEach((book) => {
        const card = document.createElement("div");
        card.className = "book-card";
        card.setAttribute("data-asin", book.asin);
        let actionButtonHTML =
            book.status === "ERROR" || book.status === "MISSING"
                ? `<button class="retry-button" data-asin="${book.asin}">Retry</button>`
                : "";
        card.innerHTML = `
        <img class="book-card-cover lazy-load" src="data:image/gif;base64,R0lGODlhAQABAIAAAAAAAP///yH5BAEAAAAALAAAAAABAAEAAAIBRAA7" data-src="${book.cover_url || ""}" alt="Cover for ${book.title}">
        <div class="book-card-info">
            <p class="book-card-title">${book.title}</p>
            <p class="book-card-author">${book.author}</p>
            <span class="book-card-status status-${book.status}">${book.status}</span>
            <div class="book-card-actions">${actionButtonHTML}</div>
        </div>`;
        grid.appendChild(card);
    });
    initializeLazyLoading();
}

// --- Data Fetching ---
async function fetchUpdates() {
    try {
        const response = await fetch("/get_page_data");
        const data = await response.json();
        libraryData = data.books;
        updateStats(data.stats);
        renderLibraryGrid();
    } catch (error) {
        console.error("Failed to fetch updates:", error);
        addLogLine("--- ERROR: Could not refresh page data. ---");
    }
}

document.querySelectorAll(".action-button").forEach((button) => {
    button.addEventListener("click", () => {
        let script = button.dataset.script;
        if (script === "download") {
            openDownloadSelectionModal();
        } else if (script === "sync") {
            // Sync now uses the new stateful job system
            startSyncJob(button);
        }
    });
});

async function startJob(job_type, asins = [], clickedButton = null, job_params = null) {
    if (isBusy) {
        showCustomAlert("An operation is already in progress. Please wait for it to complete.");
        return;
    }
    jobStartSource = "manual";
    setActionsBusy(true);

    try {
        const payload = { job_type };
        if (asins && asins.length > 0) {
            payload.asins = asins;
        }
        if (job_params) {
            payload.job_params = job_params;
        }

        const response = await fetch("/api/jobs/start", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify(payload),
        });

        // The backend now sends an error payload on failure, so we can parse it.
        const data = await response.json();

        // Check for a non-OK response OR an explicit error in the JSON payload.
        if (!response.ok || !data.success) {
            // Use the specific error from the server if available, otherwise use a generic message.
            throw new Error(data.error || `Server responded with status: ${response.status}`);
        }

        // On success, the SSE 'job_started' event will handle all UI updates.
        // We no longer need to do anything here.
    } catch (error) {
        console.error(`Error starting ${job_type} job:`, error);
        showCustomAlert(`Could not start the ${job_type} job. Please check the application log.`);
        // The SSE event won't fire on error, so we must re-enable the buttons here.
        setActionsBusy(false);
    }
}

// --- Function to start a sync job ---
function startSyncJob(clickedButton) {
    processingList.innerHTML = `
    <div class="processing-item" data-asin="sync-job">
        <div class="processing-item-info">
            <p class="processing-item-title">Library Synchronization</p>
            <p class="processing-item-author">Preparing to sync...</p>
        </div>
        <div class="processing-item-status">
            <p class="status-text">Initializing...</p>
            <div class="progress-bar">
                <div class="progress-bar-inner" style="width: 0%;"></div>
            </div>
            <!-- Add the final status icons for consistency -->
            <div class="status-icon success"><i class="fas fa-check-circle"></i></div>
            <div class="status-icon error"><i class="fas fa-times-circle"></i></div>
        </div>
    </div>`;
    processingPanel.classList.add("open");
    // The purpose of the manual button is to do a full, deep sync.
    const job_params = { sync_mode: "DEEP" };
    // Call startJob, passing the hardcoded job_params.
    startJob("SYNC", null, clickedButton, job_params);
}

document.getElementById("library-grid").addEventListener("click", (event) => {
    if (event.target && event.target.matches("button.retry-button")) {
        const btn = event.target;
        const asin = btn.dataset.asin;
        const book = libraryData.find((b) => b.asin === asin);
        if (!book) return;

        openProcessingPanel([book]);
        // Call the generic job starter for a single retry
        startJob("DOWNLOAD", [asin], btn);
    }
});

const logContainer = document.getElementById("log-container");
const toggleLogBtn = document.getElementById("toggle-log-btn");
const toggleIcon = toggleLogBtn.querySelector("i");
toggleLogBtn.addEventListener("click", () => {
    logContainer.classList.toggle("log-expanded");
    if (logContainer.classList.contains("log-expanded")) {
        toggleIcon.classList.remove("fa-chevron-up");
        toggleIcon.classList.add("fa-chevron-down");
        logOutput.scrollTop = logOutput.scrollHeight;
    } else {
        toggleIcon.classList.remove("fa-chevron-down");
        toggleIcon.classList.add("fa-chevron-up");
    }
});

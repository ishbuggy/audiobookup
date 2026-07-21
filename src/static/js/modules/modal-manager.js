// src/static/js/modules/modal-manager.js

import { startJob, openProcessingPanel } from "./job-manager.js";
import { getLibraryData, initializeLazyLoading } from "./library-manager.js";

// --- DOM Elements ---
const bookDetailModal = document.getElementById("book-detail-modal");
const detailModalCloseBtn = document.getElementById("detail-modal-close");
const downloadSelectionModal = document.getElementById("download-selection-modal");
const selectionModalCloseBtn = document.getElementById("selection-modal-close");
const selectionBookList = document.getElementById("selection-book-list");
const selectAllBtn = document.getElementById("select-all-btn");
const selectNoneBtn = document.getElementById("select-none-btn");
const processSelectedBtn = document.getElementById("process-selected-btn");
const selectionCountSpan = document.getElementById("selection-count");
const libraryGrid = document.getElementById("library-grid");
const fetchSummaryBtn = document.getElementById("fetch-full-summary-btn");

// --- Book Detail Modal Logic ---
function closeDetailModal() {
    document.body.classList.remove("modal-open");
    bookDetailModal.style.display = "none";
}

async function handleBookClick(event) {
    const card = event.target.closest(".book-card");
    if (!card) return; // Not a book card click

    const asin = card.dataset.asin;
    if (!asin) return;

    // --- CASE 1: CARD ACTION BUTTON CLICKED (download / re-download) ---
    // Mirrors the detail-modal download logic without opening the modal:
    // a plain download for not-yet-on-disk books, and a confirmation-gated
    // re-download for DOWNLOADED books.
    const cardActionBtn = event.target.closest("button[data-card-action]");
    if (cardActionBtn) {
        const action = cardActionBtn.dataset.cardAction;
        const libraryData = getLibraryData(); // imported from library-manager.js
        const book = libraryData.find((b) => b.asin === asin);
        if (!book) return;

        const runDownload = () => {
            openProcessingPanel([book]);
            startJob("DOWNLOAD", [asin]);
            // Visual Feedback: Scroll to the job panel
            const panel = document.getElementById("processing-panel");
            if (panel) panel.scrollIntoView({ behavior: "smooth", block: "start" });
        };

        if (action === "redownload") {
            if (window.showConfirmationModal) {
                window.showConfirmationModal(
                    '<i class="fas fa-exclamation-triangle"></i> Force Re-download?',
                    `Are you sure you want to re-download "<strong>${window.escapeHtml(book.title)}</strong>"?<br>This will overwrite the existing file.`,
                    runDownload,
                );
            } else if (confirm(`Re-download "${book.title}"?`)) {
                runDownload();
            }
        } else {
            runDownload();
        }
        return;
    }

    // --- CASE 2: CARD CLICKED (OPEN MODAL) ---
    try {
        const response = await fetch(`/api/book/${asin}`);
        if (!response.ok) throw new Error("Failed to fetch book details.");
        const book = await response.json();

        // --- Populate Basic Metadata ---
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
        document.getElementById("modal-book-summary").textContent = book.summary || "No summary available.";
        
        // File Info
        document.getElementById("modal-file-path").textContent = book.filepath || "N/A";
        document.getElementById("modal-file-type").textContent = book.file_type || "N/A";
        document.getElementById("modal-file-size").textContent = book.file_size_hr || "N/A";
        document.getElementById("modal-file-mtime").textContent = book.file_mtime_hr || "N/A";

        // Error Info
        const errorDetailsDiv = document.getElementById("modal-error-details");
        if (book.error_message && book.error_message.trim() !== "") {
            document.getElementById("modal-book-error").textContent = book.error_message;
            errorDetailsDiv.style.display = "block";
        } else {
            errorDetailsDiv.style.display = "none";
        }

        // Full Summary Button
        const fetchSummaryBtn = document.getElementById("fetch-full-summary-btn");
        if (book.is_summary_full === 0) {
            fetchSummaryBtn.style.display = "inline-block";
            fetchSummaryBtn.dataset.asin = asin;
        } else {
            fetchSummaryBtn.style.display = "none";
        }

        // --- Context-Aware Download Button Logic ---
        const downloadBtn = document.getElementById("modal-download-btn");
        
        // Remove old event listeners by cloning
        const newBtn = downloadBtn.cloneNode(true);
        downloadBtn.parentNode.replaceChild(newBtn, downloadBtn);
        
        const configureButton = (text, className, isConfirm) => {
            newBtn.textContent = text;
            newBtn.className = `action-button ${className}`; 
            newBtn.style.display = "inline-block";
            
            newBtn.onclick = () => {
                const runDownload = () => {
                    closeDetailModal();
                    openProcessingPanel([book]); 
                    startJob("DOWNLOAD", [asin]);
                    // Visual Feedback: Scroll to panel
                    setTimeout(() => {
                        const panel = document.getElementById("processing-panel");
                        if(panel) panel.scrollIntoView({ behavior: "smooth", block: "center" });
                    }, 300); // Small delay to allow modal to close/panel to open
                };

                if (isConfirm) {
                    if (window.showConfirmationModal) {
                        window.showConfirmationModal(
                            '<i class="fas fa-exclamation-triangle"></i> Force Re-download?',
                            `Are you sure you want to re-download "<strong>${window.escapeHtml(book.title)}</strong>"?<br>This will overwrite the existing file.`,
                            runDownload
                        );
                    } else {
                        if (confirm(`Re-download "${book.title}"?`)) runDownload();
                    }
                } else {
                    runDownload();
                }
            };
        };

        // Status Logic (colors come from the .is-warning / .blue classes,
        // which configureButton sets fresh on every open)
        if (book.status === "DOWNLOADED") {
            configureButton("Force Re-download", "is-warning", true);
        } else if (book.status === "NEW" || book.status === "MISSING" || book.status === "ERROR") {
            configureButton("Download Now", "blue", false);
        } else {
            newBtn.style.display = "none";
        }

        document.body.classList.add("modal-open");
        bookDetailModal.style.display = "flex";
    } catch (error) {
        console.error("Error fetching book details:", error);
        if (window.showCustomAlert) window.showCustomAlert("Could not load book details.");
    }
}

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
        window.showCustomAlert("Could not fetch the full summary. Please check the application log.");
    } finally {
        btn.classList.remove("loading");
        btn.disabled = false;
        btn.textContent = "Get Full Summary";
    }
}

// --- Download Selection Modal Logic ---
function updateSelectionCount() {
    const count = selectionBookList.querySelectorAll('input[type="checkbox"]:checked').length;
    selectionCountSpan.textContent = count;
}

function closeSelectionModal() {
    document.body.classList.remove("modal-open");
    downloadSelectionModal.style.display = "none";
}

export async function openDownloadSelectionModal() {
    selectionBookList.innerHTML = "<p>Loading books...</p>";
    document.body.classList.add("modal-open");
    downloadSelectionModal.style.display = "flex";

    const renderCategory = (books, title, container) => {
        if (!books || books.length === 0) return;
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
            // Escape book-derived strings before interpolating them into HTML.
            const esc = window.escapeHtml;
            div.innerHTML = `
                <img class="selection-book-thumb lazy-load" src="data:image/gif;base64,R0lGODlhAQABAIAAAAAAAP///yH5BAEAAAAALAAAAAABAAEAAAIBRAA7" data-src="/covers/${esc(book.asin)}_thumb.jpg" alt="Cover">
                <input type="checkbox" id="asin-${esc(book.asin)}" value="${esc(book.asin)}">
                <label for="asin-${esc(book.asin)}" class="selection-book-info">
                    <span class="title">${esc(book.title)}</span>
                    <span class="author">by ${esc(book.author)}</span>
                </label>
            `;
            container.appendChild(div);
        });
    };

    try {
        const response = await fetch("/api/downloadable_books");
        const data = await response.json();

        if (data.new.length === 0 && data.missing.length === 0 && data.errored.length === 0) {
            selectionBookList.innerHTML = "<p>No new, missing, or errored books are available to process.</p>";
            updateSelectionCount();
            return;
        }

        selectionBookList.innerHTML = "";
        renderCategory(data.new, "New Books", selectionBookList);
        renderCategory(data.missing, "Missing Books (Files not found)", selectionBookList);
        renderCategory(data.errored, "Books with Errors (Manual Retry)", selectionBookList);
        
        initializeLazyLoading(); // Initialize for the new thumbnails
        updateSelectionCount();
    } catch (error) {
        console.error("Failed to fetch downloadable books:", error);
        selectionBookList.innerHTML = "<p>Error loading book list. Please try again.</p>";
    }
}

// --- Event Listeners ---
document.addEventListener("DOMContentLoaded", () => {
    // Detail Modal Events
    detailModalCloseBtn.onclick = closeDetailModal;
    libraryGrid.addEventListener("click", handleBookClick);
    fetchSummaryBtn.addEventListener("click", handleFetchFullSummary);

    // Download Selection Events
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

    // Process Selected Button
    processSelectedBtn.onclick = async () => {
        const selectedASINs = Array.from(selectionBookList.querySelectorAll("input:checked")).map((cb) => cb.value);
        if (selectedASINs.length === 0) {
            window.showCustomAlert("Please select at least one book to process.");
            return;
        }
        // Get book objects for the selected ASINs to populate the panel immediately
        const libraryData = getLibraryData();
        const selectedBooks = libraryData.filter((book) => selectedASINs.includes(book.asin));
        
        openProcessingPanel(selectedBooks);
        closeSelectionModal();
        startJob("DOWNLOAD", selectedASINs);
    };
});
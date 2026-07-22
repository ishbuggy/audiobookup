// src/static/js/modules/modal-manager.js

import { startJob } from "./job-manager.js";
import {
    getLibraryData,
    getSelectedAsins,
    clearSelection,
    setSelection,
    initializeLazyLoading,
    renderLibraryGrid,
} from "./library-manager.js";
import { setCoverBuster, bustCoverUrl } from "./cover-cache.js";

// The book currently shown in the detail modal. The metadata editor and cover
// upload handlers (attached once at load) read the active ASIN/values from here
// instead of re-binding listeners on every open.
let currentDetailBook = null;

// Cached value of the `naming.apply_custom_to_filenames` setting, fetched lazily
// the first time the editor opens. When true, saving a title/author edit renames
// the file on disk, so the editor shows a warning.
let applyCustomToFilenames = null;

// Cover cache-busting for images re-uploaded this session is shared with the
// library grid via ./cover-cache.js (setCoverBuster / bustCoverUrl), so a cover
// replaced here stays busted in the grid across refreshes too.

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
const bulkRenameModal = document.getElementById("bulk-rename-modal");

// --- Book Detail Modal Logic ---
function closeDetailModal() {
    document.body.classList.remove("modal-open");
    bookDetailModal.style.display = "none";
}

async function handleBookClick(event) {
    const card = event.target.closest(".book-card");
    if (!card) return; // Not a book card click

    // The multi-select checkbox (Phase 4) lives inside the card; a click on it
    // must toggle the selection, not open the detail modal.
    if (event.target.closest(".select-checkbox")) return;

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

        const runDownload = async () => {
            // startJob populates the panel itself (only if no other job is
            // running) and reports whether the job actually started.
            const started = await startJob("DOWNLOAD", [asin], null, null, [book]);
            if (!started) return;
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

        // Remember the open book for the editor / cover-upload handlers, and
        // make sure the editor + duplicate resolver start collapsed on every open.
        currentDetailBook = book;
        closeMetadataEditor();
        closeDuplicateResolver();

        // Duplicate flag + resolve action (Phase 5): shown only when the book was
        // flagged as a colliding duplicate.
        const isDuplicate = Boolean(book.is_duplicate);
        document.getElementById("modal-duplicate-flag").style.display = isDuplicate ? "inline-block" : "none";
        document.getElementById("modal-resolve-duplicate-btn").style.display = isDuplicate ? "inline-block" : "none";

        // --- Populate Basic Metadata ---
        document.getElementById("modal-book-cover").src = bustCoverUrl(asin, book.cover_url_original);
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
                const runDownload = async () => {
                    closeDetailModal();
                    // startJob populates the panel itself (only if no other job
                    // is running) and reports whether the job actually started.
                    const started = await startJob("DOWNLOAD", [asin], null, null, [book]);
                    if (!started) return;
                    // Visual Feedback: Scroll to panel
                    setTimeout(() => {
                        const panel = document.getElementById("processing-panel");
                        if (panel) panel.scrollIntoView({ behavior: "smooth", block: "center" });
                    }, 300); // Small delay to allow modal to close/panel to open
                };

                if (isConfirm) {
                    if (window.showConfirmationModal) {
                        window.showConfirmationModal(
                            '<i class="fas fa-exclamation-triangle"></i> Force Re-download?',
                            `Are you sure you want to re-download "<strong>${window.escapeHtml(book.title)}</strong>"?<br>This will overwrite the existing file.`,
                            runDownload,
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

// Lazily fetch (and cache for the session) whether saving a metadata edit also
// renames the file on disk. Used by both the single-book editor and the bulk
// rename modal to decide whether to show the "will rename the file" warning.
async function ensureApplyCustomToFilenames() {
    if (applyCustomToFilenames === null) {
        try {
            const res = await fetch("/api/settings");
            const settings = await res.json();
            applyCustomToFilenames = Boolean(settings?.naming?.apply_custom_to_filenames);
        } catch (error) {
            console.error("Could not read settings for rename warning:", error);
            applyCustomToFilenames = false;
        }
    }
    return applyCustomToFilenames;
}

// --- Metadata Editor (Phase 3.1) ---

// Reflect an edit back into the in-memory library array and re-render the grid
// so the change is visible without a full reload. `libraryData` is returned by
// reference from library-manager.js, so mutating the matching entry is enough.
function applyEditToLibrary(asin, fields) {
    const book = getLibraryData().find((b) => b.asin === asin);
    if (book) Object.assign(book, fields);
    renderLibraryGrid();
}

// Show the native Audible value as a hint when a custom override is active, so
// the user knows what "Reset to Audible" would restore. Hidden when the effective
// value already equals the native one.
function setNativeHint(el, effective, native) {
    if (native && native !== effective) {
        el.textContent = `Audible: ${native}`;
        el.style.display = "block";
    } else {
        el.style.display = "none";
    }
}

async function openMetadataEditor() {
    if (!currentDetailBook) return;
    const book = currentDetailBook;

    document.getElementById("modal-edit-title").value = book.title || "";
    document.getElementById("modal-edit-author").value = book.author || "";
    setNativeHint(document.getElementById("modal-edit-title-native"), book.title, book.native_title);
    setNativeHint(document.getElementById("modal-edit-author-native"), book.author, book.native_author);

    // Warn (once we know the setting) that saving will rename the file on disk.
    // The setting is fetched lazily and cached for the session.
    const willRename = await ensureApplyCustomToFilenames();
    document.getElementById("modal-edit-rename-warning").style.display = willRename ? "block" : "none";

    document.getElementById("modal-edit-metadata-btn").style.display = "none";
    document.getElementById("modal-edit-container").style.display = "block";
}

function closeMetadataEditor() {
    const container = document.getElementById("modal-edit-container");
    if (container) container.style.display = "none";
    const editBtn = document.getElementById("modal-edit-metadata-btn");
    if (editBtn) editBtn.style.display = "inline-block";
}

// Persist the editor's fields (or explicit values, e.g. the reset-to-empty case)
// via POST /api/book/<asin>/update, then reconcile the modal + grid from the
// authoritative response and toast the outcome.
async function saveMetadata(payload) {
    if (!currentDetailBook) return;
    const asin = currentDetailBook.asin;

    const saveBtn = document.getElementById("modal-save-metadata-btn");
    const resetBtn = document.getElementById("modal-reset-metadata-btn");
    saveBtn.disabled = true;
    resetBtn.disabled = true;

    try {
        const response = await fetch(`/api/book/${asin}/update`, {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify(payload),
        });
        const data = await response.json();
        if (!response.ok || !data.success) {
            throw new Error(data.error || `Server responded with status ${response.status}.`);
        }

        // Reflect the effective values everywhere: the open modal, the cached
        // book object, and the library grid/list/table.
        document.getElementById("modal-book-title").textContent = data.title || "N/A";
        document.getElementById("modal-book-author").textContent = data.author || "N/A";
        Object.assign(currentDetailBook, {
            title: data.title,
            author: data.author,
            native_title: data.native_title,
            native_author: data.native_author,
            custom_title: data.custom_title,
            custom_author: data.custom_author,
        });
        applyEditToLibrary(asin, {
            title: data.title,
            author: data.author,
            custom_title: data.custom_title,
            custom_author: data.custom_author,
        });

        // If the opt-in filename rename ran, the stored path changed — reflect it.
        if (data.renamed_to) {
            document.getElementById("modal-file-path").textContent = data.renamed_to;
        }

        closeMetadataEditor();
        if (window.showToast) window.showToast("Metadata updated.", "success");
    } catch (error) {
        console.error("Failed to update metadata:", error);
        if (window.showToast) window.showToast(`Could not save metadata: ${error.message}`, "error");
    } finally {
        saveBtn.disabled = false;
        resetBtn.disabled = false;
    }
}

// --- Cover Upload (Phase 3.2) ---
const MAX_COVER_BYTES = 15 * 1024 * 1024; // mirror the server-side cap for a fast client-side reject

async function handleCoverUpload(event) {
    const input = event.target;
    const file = input.files && input.files[0];
    // Reset the input so re-selecting the same file still fires `change`.
    input.value = "";
    if (!file || !currentDetailBook) return;

    if (file.size > MAX_COVER_BYTES) {
        if (window.showToast) window.showToast("Cover image is too large (max 15 MB).", "error");
        return;
    }

    const asin = currentDetailBook.asin;
    const uploadBtn = document.getElementById("modal-cover-upload-btn");
    uploadBtn.disabled = true;

    try {
        const formData = new FormData();
        formData.append("cover", file);
        const response = await fetch(`/api/book/${asin}/cover`, { method: "POST", body: formData });
        const data = await response.json();
        if (!response.ok || !data.success) {
            throw new Error(data.error || `Server responded with status ${response.status}.`);
        }

        // Bust the browser/image cache so the new art shows immediately in both
        // the modal (full cover) and the grid (thumbnail). The token is remembered
        // per-ASIN so a later modal reopen (which re-fetches the book with the bare
        // URL) still busts the cached original.
        const bust = `?t=${setCoverBuster(asin)}`;
        document.getElementById("modal-book-cover").src = `${data.cover_url_original}${bust}`;
        currentDetailBook.cover_url_original = data.cover_url_original;
        applyEditToLibrary(asin, { cover_url: `${data.cover_url_thumb}${bust}` });

        if (window.showToast) window.showToast("Cover updated.", "success");
    } catch (error) {
        console.error("Failed to upload cover:", error);
        if (window.showToast) window.showToast(`Could not update cover: ${error.message}`, "error");
    } finally {
        uploadBtn.disabled = false;
    }
}

// --- Duplicate Resolution (Phase 5) ---
// A flagged duplicate is a book whose filename collided with another and was
// saved as `Title_<ASIN>.m4b`. The resolver lets the user disambiguate it by
// choosing a naming convention (append narrator or release year) — applied as a
// title override via the same POST /api/book/<asin>/update endpoint, with an
// explicit `resolve_duplicate` flag that clears `is_duplicate`. "Keep" just
// dismisses the flag without changing the title.

// Pull a 4-digit year out of an Audible release_date (e.g. "2015-06-01" → "2015").
function releaseYear(book) {
    const match = /\b(\d{4})\b/.exec(book.release_date || "");
    return match ? match[1] : "";
}

// The disambiguated title for a given choice. Built from the native Audible title
// so re-resolving never stacks suffixes. Returns null for "keep" (no title change)
// or when the chosen field is unavailable.
function duplicateNewTitle(book, choice) {
    const base = book.native_title || book.title || "";
    if (choice === "narrator") return book.narrator ? `${base} (${book.narrator})` : null;
    if (choice === "year") {
        const year = releaseYear(book);
        return year ? `${base} (${year})` : null;
    }
    return null; // "keep"
}

// The currently-selected resolution choice.
function selectedDuplicateChoice() {
    const checked = document.querySelector('input[name="dup-resolve-choice"]:checked');
    return checked ? checked.value : "keep";
}

// Update the live preview line for the current choice.
function renderDuplicatePreview() {
    if (!currentDetailBook) return;
    const choice = selectedDuplicateChoice();
    const preview = document.getElementById("modal-duplicate-preview");
    if (choice === "keep") {
        preview.textContent = "(unchanged — keeps the ASIN-suffixed filename)";
        return;
    }
    const newTitle = duplicateNewTitle(currentDetailBook, choice);
    preview.textContent = newTitle || "(not available for this book)";
}

async function openDuplicateResolver() {
    if (!currentDetailBook) return;
    const book = currentDetailBook;

    // Enable only the choices we can actually build for this book, and select
    // the first available one (falling back to "keep").
    const hasNarrator = Boolean(book.narrator);
    const hasYear = Boolean(releaseYear(book));
    const radios = {
        narrator: document.querySelector('input[name="dup-resolve-choice"][value="narrator"]'),
        year: document.querySelector('input[name="dup-resolve-choice"][value="year"]'),
        keep: document.querySelector('input[name="dup-resolve-choice"][value="keep"]'),
    };
    radios.narrator.disabled = !hasNarrator;
    radios.year.disabled = !hasYear;
    if (hasNarrator) radios.narrator.checked = true;
    else if (hasYear) radios.year.checked = true;
    else radios.keep.checked = true;

    // Reuse the shared rename-warning setting (only meaningful when a title
    // actually changes, i.e. not for "keep").
    const willRename = await ensureApplyCustomToFilenames();
    document.getElementById("modal-duplicate-rename-warning").style.display = willRename ? "block" : "none";

    renderDuplicatePreview();
    document.getElementById("modal-resolve-duplicate-btn").style.display = "none";
    document.getElementById("modal-duplicate-container").style.display = "block";
}

function closeDuplicateResolver() {
    const container = document.getElementById("modal-duplicate-container");
    if (container) container.style.display = "none";
    // The resolve button is only restored when the book is still a duplicate.
    const btn = document.getElementById("modal-resolve-duplicate-btn");
    if (btn && currentDetailBook && currentDetailBook.is_duplicate) btn.style.display = "inline-block";
}

// Apply the chosen resolution: POST the title override (if any) plus the
// resolve_duplicate flag, then reconcile the modal + library from the response.
async function applyDuplicateResolution() {
    if (!currentDetailBook) return;
    const asin = currentDetailBook.asin;
    const choice = selectedDuplicateChoice();
    const newTitle = duplicateNewTitle(currentDetailBook, choice);

    const applyBtn = document.getElementById("modal-duplicate-apply-btn");
    const cancelBtn = document.getElementById("modal-duplicate-cancel-btn");
    applyBtn.disabled = true;
    cancelBtn.disabled = true;

    // "keep" (or an unavailable field) sends no custom_title — it only clears the
    // flag. Otherwise set the disambiguated title override.
    const payload = { resolve_duplicate: true };
    if (newTitle) payload.custom_title = newTitle;

    try {
        const response = await fetch(`/api/book/${asin}/update`, {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify(payload),
        });
        const data = await response.json();
        if (!response.ok || !data.success) {
            throw new Error(data.error || `Server responded with status ${response.status}.`);
        }

        // Reflect the effective title/author and the cleared flag everywhere.
        document.getElementById("modal-book-title").textContent = data.title || "N/A";
        document.getElementById("modal-book-author").textContent = data.author || "N/A";
        Object.assign(currentDetailBook, {
            title: data.title,
            author: data.author,
            native_title: data.native_title,
            native_author: data.native_author,
            custom_title: data.custom_title,
            custom_author: data.custom_author,
            is_duplicate: data.is_duplicate,
        });
        applyEditToLibrary(asin, {
            title: data.title,
            author: data.author,
            custom_title: data.custom_title,
            custom_author: data.custom_author,
            is_duplicate: data.is_duplicate,
        });

        if (data.renamed_to) {
            document.getElementById("modal-file-path").textContent = data.renamed_to;
        }

        // The flag is now clear: hide both the indicator and the resolve button.
        document.getElementById("modal-duplicate-flag").style.display = "none";
        document.getElementById("modal-resolve-duplicate-btn").style.display = "none";
        document.getElementById("modal-duplicate-container").style.display = "none";
        if (window.showToast) window.showToast("Duplicate resolved.", "success");
    } catch (error) {
        console.error("Failed to resolve duplicate:", error);
        if (window.showToast) window.showToast(`Could not resolve duplicate: ${error.message}`, "error");
    } finally {
        applyBtn.disabled = false;
        cancelBtn.disabled = false;
    }
}

// --- Bulk Rename (Phase 4) ---
// Reuses the per-book POST /api/book/<asin>/update endpoint: the new value is
// computed client-side for each selected book and applied one POST at a time
// (sequential, so opt-in on-disk renames can't race each other). A live preview
// shows exactly which titles/authors change before anything is written.

// Mirror of the backend `_strip_subtitle`: drop a "Main Title: Subtitle" tail on
// the first ": " (so ratios/times like "12:00" survive) and never return empty.
function stripSubtitle(value) {
    if (!value) return "";
    const main = value.split(": ")[0].trim();
    return main || value;
}

// Read the current bulk-rename form controls.
function readBulkForm() {
    return {
        op: document.getElementById("bulk-rename-op").value,
        find: document.getElementById("bulk-find").value,
        replace: document.getElementById("bulk-replace").value,
        target: document.getElementById("bulk-replace-target").value,
    };
}

// Compute the proposed new value for one book: which override field it writes,
// the current value, and the transformed value.
function bulkTransform(book, form) {
    if (form.op === "subtitle") {
        const before = book.title || "";
        return { field: "custom_title", before, after: stripSubtitle(before) };
    }
    // Find & replace on the chosen field (all occurrences, literal — no regex).
    const field = form.target === "author" ? "custom_author" : "custom_title";
    const before = (form.target === "author" ? book.author : book.title) || "";
    const after = form.find ? before.split(form.find).join(form.replace) : before;
    return { field, before, after };
}

// Build the preview rows for the current selection + form. Each row is tagged
// "change" (will be written), "same" (no effect), or "empty" (would blank the
// value — skipped, since an empty override just reverts to the Audible value).
function computeBulkPreview() {
    const form = readBulkForm();
    const library = getLibraryData();
    const items = [];
    getSelectedAsins().forEach((asin) => {
        const book = library.find((b) => b.asin === asin);
        if (!book) return;
        const { field, before, after } = bulkTransform(book, form);
        let state = "same";
        if (after !== before) state = after.trim() === "" ? "empty" : "change";
        items.push({ asin, field, before, after, state });
    });
    return items;
}

// Render the live preview and keep the summary line + Apply button in sync.
function renderBulkPreview() {
    const items = computeBulkPreview();
    const container = document.getElementById("bulk-rename-preview");
    container.innerHTML = "";

    items.forEach((item) => {
        const row = document.createElement("div");
        row.className = `bulk-preview-row bulk-preview-${item.state}`;

        // Use textContent for all book-derived strings so nothing is injected as
        // markup (the values are user/Audible-supplied).
        const before = document.createElement("span");
        before.className = "bulk-preview-before";
        before.textContent = item.before;

        const arrow = document.createElement("span");
        arrow.className = "bulk-preview-arrow";
        arrow.textContent = "→";

        const after = document.createElement("span");
        after.className = "bulk-preview-after";
        if (item.state === "same") after.textContent = "(no change)";
        else if (item.state === "empty") after.textContent = "(would be empty — skipped)";
        else after.textContent = item.after;

        row.append(before, arrow, after);
        container.appendChild(row);
    });

    const changeCount = items.filter((i) => i.state === "change").length;
    document.getElementById("bulk-rename-summary").textContent =
        `${changeCount} of ${items.length} selected will change.`;
    document.getElementById("bulk-rename-apply-btn").disabled = changeCount === 0;
}

// Show the find/replace inputs only for the find-and-replace operation.
function updateBulkFieldsVisibility() {
    const op = document.getElementById("bulk-rename-op").value;
    document.getElementById("bulk-replace-fields").style.display = op === "replace" ? "block" : "none";
}

function closeBulkRenameModal() {
    document.body.classList.remove("modal-open");
    bulkRenameModal.style.display = "none";
}

async function openBulkRenameModal() {
    if (getSelectedAsins().length === 0) return;

    // Reset the form to its defaults on every open.
    document.getElementById("bulk-rename-op").value = "subtitle";
    document.getElementById("bulk-find").value = "";
    document.getElementById("bulk-replace").value = "";
    document.getElementById("bulk-replace-target").value = "title";
    updateBulkFieldsVisibility();

    // Warn if the opt-in setting will also rename files on disk for each change.
    const willRename = await ensureApplyCustomToFilenames();
    document.getElementById("bulk-rename-warning").style.display = willRename ? "block" : "none";

    renderBulkPreview();
    document.body.classList.add("modal-open");
    bulkRenameModal.style.display = "flex";
}

// Apply the changed rows one POST at a time, reconciling each book from the
// authoritative response, then clear the selection and re-render.
async function applyBulkRename() {
    const items = computeBulkPreview().filter((i) => i.state === "change");
    if (items.length === 0) return;

    const applyBtn = document.getElementById("bulk-rename-apply-btn");
    const cancelBtn = document.getElementById("bulk-rename-cancel-btn");
    const originalLabel = applyBtn.textContent;
    applyBtn.disabled = true;
    cancelBtn.disabled = true;

    const library = getLibraryData();
    let ok = 0;
    const failedAsins = [];

    for (let i = 0; i < items.length; i++) {
        const item = items[i];
        applyBtn.textContent = `Applying ${i + 1} / ${items.length}…`;
        try {
            const response = await fetch(`/api/book/${item.asin}/update`, {
                method: "POST",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify({ [item.field]: item.after }),
            });
            const data = await response.json();
            if (!response.ok || !data.success) {
                throw new Error(data.error || `status ${response.status}`);
            }
            const book = library.find((b) => b.asin === item.asin);
            if (book) {
                Object.assign(book, {
                    title: data.title,
                    author: data.author,
                    native_title: data.native_title,
                    native_author: data.native_author,
                    custom_title: data.custom_title,
                    custom_author: data.custom_author,
                });
            }
            ok++;
        } catch (error) {
            console.error(`Bulk rename failed for ${item.asin}:`, error);
            failedAsins.push(item.asin);
        }
    }

    applyBtn.textContent = originalLabel;
    applyBtn.disabled = false;
    cancelBtn.disabled = false;

    closeBulkRenameModal();
    // Keep only the failed rows selected so the user can retry them directly;
    // clear entirely when everything succeeded.
    if (failedAsins.length === 0) clearSelection();
    else setSelection(failedAsins);
    renderLibraryGrid();

    if (window.showToast) {
        if (failedAsins.length === 0) {
            window.showToast(`Renamed ${ok} book${ok === 1 ? "" : "s"}.`, "success");
        } else {
            window.showToast(`Renamed ${ok}; ${failedAsins.length} failed (still selected — see the log).`, "error");
        }
    }
}

// --- Large-bulk download warning (Phase 6 / FR13) ---
// AudioBookup always re-encodes, so a big batch takes real time. When a bulk
// selection reaches this many books we interrupt with a confirmation that
// explains the expectation and shows a rough total-time estimate.
const LARGE_BULK_THRESHOLD = 10;

// The estimator's learned conversion rate (seconds of processing per minute of
// audio), fetched lazily from the backend the first time a large bulk fires and
// cached for the page's lifetime.
let conversionRatePromise = null;

function ensureConversionRate() {
    if (conversionRatePromise === null) {
        conversionRatePromise = fetch("/api/conversion_rate")
            .then((r) => r.json())
            .then((d) => (typeof d.sec_per_min === "number" && d.sec_per_min > 0 ? d.sec_per_min : 10))
            // If the rate endpoint fails, fall back to the estimator's default
            // guess (10 sec/min) so the warning still shows a figure.
            .catch(() => 10);
    }
    return conversionRatePromise;
}

// Render a rough duration ("about 2 hours 30 minutes") from a second count.
// Deliberately coarse — this is a "this will take a while" expectation, not a
// precise ETA (real processing runs several books/chapters in parallel).
function formatRoughDuration(totalSeconds) {
    const minutes = Math.max(1, Math.round(totalSeconds / 60));
    if (minutes < 60) {
        return `about ${minutes} minute${minutes === 1 ? "" : "s"}`;
    }
    const hours = Math.floor(minutes / 60);
    const remMinutes = minutes % 60;
    const hourPart = `${hours} hour${hours === 1 ? "" : "s"}`;
    if (remMinutes === 0) {
        return `about ${hourPart}`;
    }
    return `about ${hourPart} ${remMinutes} minute${remMinutes === 1 ? "" : "s"}`;
}

// Start the bulk download job for the given ASINs. startJob populates the
// processing panel itself, only after confirming no other job is running.
function startBulkDownload(selectedASINs) {
    const libraryData = getLibraryData();
    const selectedBooks = libraryData.filter((book) => selectedASINs.includes(book.asin));
    closeSelectionModal();
    startJob("DOWNLOAD", selectedASINs, null, null, selectedBooks);
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

    // Metadata editor (Phase 3.1). Listeners are bound once; the handlers read
    // the active book from `currentDetailBook`.
    document.getElementById("modal-edit-metadata-btn").addEventListener("click", openMetadataEditor);
    document.getElementById("modal-cancel-edit-btn").addEventListener("click", closeMetadataEditor);
    document.getElementById("modal-save-metadata-btn").addEventListener("click", () => {
        saveMetadata({
            custom_title: document.getElementById("modal-edit-title").value,
            custom_author: document.getElementById("modal-edit-author").value,
        });
    });
    document.getElementById("modal-reset-metadata-btn").addEventListener("click", () => {
        // Empty strings clear the overrides server-side, reverting to Audible.
        saveMetadata({ custom_title: "", custom_author: "" });
    });

    // Cover upload (Phase 3.2): the button proxies to the hidden file input.
    document
        .getElementById("modal-cover-upload-btn")
        .addEventListener("click", () => document.getElementById("modal-cover-input").click());
    document.getElementById("modal-cover-input").addEventListener("change", handleCoverUpload);

    // Duplicate resolution (Phase 5). The Resolve Duplicate button reveals the
    // panel; the radio choices drive the live preview; Apply writes the override
    // and clears the flag via the per-book update endpoint.
    document.getElementById("modal-resolve-duplicate-btn").addEventListener("click", openDuplicateResolver);
    document.getElementById("modal-duplicate-cancel-btn").addEventListener("click", closeDuplicateResolver);
    document.getElementById("modal-duplicate-apply-btn").addEventListener("click", applyDuplicateResolution);
    document.querySelectorAll('input[name="dup-resolve-choice"]').forEach((radio) => {
        radio.addEventListener("change", renderDuplicatePreview);
    });

    // Bulk rename (Phase 4). The bar's "Bulk Rename" button (rendered/enabled in
    // library-manager) opens the modal; the operation controls drive a live
    // preview; Apply writes the changes via the per-book update endpoint.
    document.getElementById("bulk-rename-btn").addEventListener("click", openBulkRenameModal);
    document.getElementById("bulk-rename-close").addEventListener("click", closeBulkRenameModal);
    document.getElementById("bulk-rename-cancel-btn").addEventListener("click", closeBulkRenameModal);
    document.getElementById("bulk-rename-op").addEventListener("change", () => {
        updateBulkFieldsVisibility();
        renderBulkPreview();
    });
    document.getElementById("bulk-replace-target").addEventListener("change", renderBulkPreview);
    document.getElementById("bulk-find").addEventListener("input", renderBulkPreview);
    document.getElementById("bulk-replace").addEventListener("input", renderBulkPreview);
    document.getElementById("bulk-rename-apply-btn").addEventListener("click", applyBulkRename);

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

        // Small selections start immediately. Large ones get a heads-up first
        // (Phase 6 / FR13): AudioBookup always re-encodes, so a big batch takes
        // real time — set the expectation with a rough estimate before starting.
        if (selectedASINs.length < LARGE_BULK_THRESHOLD) {
            startBulkDownload(selectedASINs);
            return;
        }

        const libraryData = getLibraryData();
        const selectedBooks = libraryData.filter((book) => selectedASINs.includes(book.asin));
        const totalRuntimeMin = selectedBooks.reduce((sum, book) => sum + (Number(book.runtime_min) || 0), 0);

        const secPerMin = await ensureConversionRate();
        // Rough upper-bound estimate: the rate is per-book-sequential, while real
        // processing runs several books/chapters in parallel, so the true wall
        // time is usually less. Only show a figure when we know the runtimes.
        const estimateHtml =
            totalRuntimeMin > 0
                ? `<br><br>Based on recent conversions, this could take roughly <strong>${formatRoughDuration(
                      totalRuntimeMin * secPerMin,
                  )}</strong> (a rough estimate — it often finishes sooner).`
                : "";

        // The confirmation modal closes itself once the callback runs, so the
        // callback only needs to start the download. Fall back to a native confirm
        // if the modal helper isn't present (mirrors the re-download path).
        if (window.showConfirmationModal) {
            window.showConfirmationModal(
                '<i class="fas fa-clock" style="color: #ffc107;"></i> Large download',
                `You've selected <strong>${selectedASINs.length}</strong> books. AudioBookup converts every ` +
                    `book to a DRM-free file, which takes time.${estimateHtml}<br><br>Downloads run in the ` +
                    `background — you can keep using the app while they process. Start now?`,
                () => startBulkDownload(selectedASINs),
            );
        } else if (confirm(`Start downloading ${selectedASINs.length} books? This can take a while.`)) {
            startBulkDownload(selectedASINs);
        }
    };
});

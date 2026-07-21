// src/static/js/modules/library-manager.js

import { addLogLine } from "./job-manager.js";

// --- State ---
let libraryData = [];

// Which library layout is active: "grid" (default cards), "list" (one row
// per book), or "table" (dense tabular). Persisted in localStorage exactly
// like the theme toggle, so a user's choice survives reloads.
const VIEW_STORAGE_KEY = "libraryView";
const VALID_VIEWS = ["grid", "list", "table"];
let currentView = VALID_VIEWS.includes(localStorage.getItem(VIEW_STORAGE_KEY))
    ? localStorage.getItem(VIEW_STORAGE_KEY)
    : "grid";

// --- DOM Elements ---
const searchBar = document.getElementById("search-bar");
const sortBy = document.getElementById("sort-by");
const filterByStatus = document.getElementById("filter-by-status");
const libraryGrid = document.getElementById("library-grid");
const viewSwitcher = document.getElementById("view-switcher");

// --- Lazy Loading ---
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

export function initializeLazyLoading() {
    document.querySelectorAll(".lazy-load").forEach((img) => {
        lazyLoadObserver.observe(img);
    });
}

// --- Data Access ---
export function getLibraryData() {
    return libraryData;
}

// --- UI Updates ---
function updateStats(stats) {
    document.getElementById("stats-downloaded").textContent = stats.downloaded || 0;
    document.getElementById("stats-new").textContent = stats.new || 0;
    document.getElementById("stats-missing").textContent = stats.missing || 0;
    document.getElementById("stats-error").textContent = stats.error || 0;
}

// A 1x1 transparent GIF used as the lazy-load placeholder src until the real
// cover scrolls into view (see initializeLazyLoading / the IntersectionObserver).
const LAZY_PLACEHOLDER = "data:image/gif;base64,R0lGODlhAQABAIAAAAAAAP///yH5BAEAAAAALAAAAAABAAEAAAIBRAA7";

// Status-aware action button markup, shared by every view. Mirrors the detail
// modal: a plain "Download" for anything not yet on disk, and a cautionary
// "Re-download" (confirmation-gated in the click handler) for DOWNLOADED books.
// Transitional statuses (e.g. DOWNLOADING) get no button. The click handling
// lives in modal-manager.js via `data-card-action`, so every view must keep the
// button's `data-asin`/`data-card-action` attributes intact.
function buildActionButtonHTML(book, esc) {
    if (book.status === "DOWNLOADED") {
        return `<button class="retry-button is-warning" data-asin="${esc(book.asin)}" data-card-action="redownload">Re-download</button>`;
    }
    if (book.status === "NEW" || book.status === "MISSING" || book.status === "ERROR") {
        return `<button class="retry-button" data-asin="${esc(book.asin)}" data-card-action="download">Download</button>`;
    }
    return "";
}

// --- Grid View (default cards) ---
function renderGridView(books, esc) {
    books.forEach((book) => {
        const card = document.createElement("div");
        card.className = "book-card";
        card.setAttribute("data-asin", book.asin);
        card.innerHTML = `
        <img class="book-card-cover lazy-load" src="${LAZY_PLACEHOLDER}" data-src="${esc(book.cover_url || "")}" alt="Cover for ${esc(book.title)}">
        <div class="book-card-info">
            <p class="book-card-title">${esc(book.title)}</p>
            <p class="book-card-author">${esc(book.author)}</p>
            <span class="book-card-status status-${esc(book.status)}">${esc(book.status)}</span>
            <div class="book-card-actions">${buildActionButtonHTML(book, esc)}</div>
        </div>`;
        libraryGrid.appendChild(card);
    });
}

// --- List View (one row per book, more metadata visible) ---
function renderListView(books, esc) {
    books.forEach((book) => {
        // Compact secondary metadata line: narrator / series / release date,
        // dropping any pieces the book doesn't have.
        const metaParts = [];
        if (book.narrator) metaParts.push(`Narrated by ${esc(book.narrator)}`);
        if (book.series) metaParts.push(esc(book.series));
        if (book.release_date) metaParts.push(esc(book.release_date));

        const card = document.createElement("div");
        card.className = "book-card list-card";
        card.setAttribute("data-asin", book.asin);
        card.innerHTML = `
        <img class="book-card-cover lazy-load" src="${LAZY_PLACEHOLDER}" data-src="${esc(book.cover_url || "")}" alt="Cover for ${esc(book.title)}">
        <div class="list-card-main">
            <p class="book-card-title">${esc(book.title)}</p>
            <p class="book-card-author">${esc(book.author)}</p>
            ${metaParts.length ? `<p class="list-card-meta">${metaParts.join(" &middot; ")}</p>` : ""}
        </div>
        <span class="book-card-status status-${esc(book.status)}">${esc(book.status)}</span>
        <div class="book-card-actions">${buildActionButtonHTML(book, esc)}</div>`;
        libraryGrid.appendChild(card);
    });
}

// --- Table View (dense, tabular) ---
function renderTableView(books, esc) {
    const table = document.createElement("table");
    table.className = "library-table";
    table.innerHTML = `
        <thead>
            <tr>
                <th class="col-cover"></th>
                <th>Title</th>
                <th>Author</th>
                <th>Series</th>
                <th>Status</th>
                <th class="col-actions">Actions</th>
            </tr>
        </thead>
        <tbody></tbody>`;
    const tbody = table.querySelector("tbody");
    books.forEach((book) => {
        // Each row keeps the `.book-card` class + `data-asin` so the existing
        // click delegation (modal open / card actions) works unchanged.
        const row = document.createElement("tr");
        row.className = "book-card";
        row.setAttribute("data-asin", book.asin);
        row.innerHTML = `
            <td class="col-cover"><img class="book-card-cover lazy-load" src="${LAZY_PLACEHOLDER}" data-src="${esc(book.cover_url || "")}" alt="Cover for ${esc(book.title)}"></td>
            <td class="table-title">${esc(book.title)}</td>
            <td>${esc(book.author)}</td>
            <td>${esc(book.series || "")}</td>
            <td><span class="book-card-status status-${esc(book.status)}">${esc(book.status)}</span></td>
            <td class="col-actions"><div class="book-card-actions">${buildActionButtonHTML(book, esc)}</div></td>`;
        tbody.appendChild(row);
    });
    libraryGrid.appendChild(table);
}

function updateLibraryTable(books) {
    // Reflect the active view as a class on the grid container so the CSS can
    // switch layout (grid / list / table) without touching this JS.
    libraryGrid.className = `view-${currentView}`;
    libraryGrid.innerHTML = "";
    if (books.length === 0) {
        libraryGrid.innerHTML =
            "<p>No books found matching your criteria. Try adjusting your search or sort options.</p>";
        return;
    }
    // Escape all book-derived strings before interpolating them into HTML.
    const esc = window.escapeHtml;
    if (currentView === "list") {
        renderListView(books, esc);
    } else if (currentView === "table") {
        renderTableView(books, esc);
    } else {
        renderGridView(books, esc);
    }
    initializeLazyLoading();
}

export function renderLibraryGrid() {
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
            return title.includes(searchTerm) || author.includes(searchTerm) || narrator.includes(searchTerm);
        });
    }

    // 2. Apply Status Filter
    if (statusFilter) {
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

    updateLibraryTable(booksToDisplay);
}

// --- Data Fetching ---
export async function fetchUpdates() {
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

// --- View Switching ---
// Highlight the button matching the active view so the toggle reflects state.
function updateViewSwitcherUI() {
    if (!viewSwitcher) return;
    viewSwitcher.querySelectorAll("button[data-view]").forEach((btn) => {
        const isActive = btn.dataset.view === currentView;
        btn.classList.toggle("active", isActive);
        btn.setAttribute("aria-pressed", isActive ? "true" : "false");
    });
}

function setView(view) {
    if (!VALID_VIEWS.includes(view) || view === currentView) return;
    currentView = view;
    localStorage.setItem(VIEW_STORAGE_KEY, view);
    updateViewSwitcherUI();
    renderLibraryGrid();
}

// --- Event Listeners ---
document.addEventListener("DOMContentLoaded", () => {
    searchBar.addEventListener("input", renderLibraryGrid);
    sortBy.addEventListener("change", renderLibraryGrid);
    filterByStatus.addEventListener("change", renderLibraryGrid);

    // View switcher (Grid / List / Table), persisted in localStorage.
    if (viewSwitcher) {
        viewSwitcher.addEventListener("click", (event) => {
            const btn = event.target.closest("button[data-view]");
            if (btn) setView(btn.dataset.view);
        });
        updateViewSwitcherUI();
    }

    // Deep-link the dashboard status boxes into the library grid: clicking a
    // count applies the matching status filter and scrolls to the results,
    // reusing the existing filter/render pipeline.
    const libraryContainer = document.getElementById("library-container");
    document.querySelectorAll(".status-box[data-status-filter]").forEach((box) => {
        box.addEventListener("click", () => {
            filterByStatus.value = box.dataset.statusFilter;
            renderLibraryGrid();
            if (libraryContainer) {
                libraryContainer.scrollIntoView({ behavior: "smooth", block: "start" });
            }
        });
    });
});

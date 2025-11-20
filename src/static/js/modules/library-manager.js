// src/static/js/modules/library-manager.js

// --- State ---
let libraryData = [];

// --- DOM Elements ---
const searchBar = document.getElementById("search-bar");
const sortBy = document.getElementById("sort-by");
const filterByStatus = document.getElementById("filter-by-status");
const libraryGrid = document.getElementById("library-grid");
const logOutput = document.getElementById("log-output");
const latestLogLine = document.getElementById("latest-log-line");

// --- Helper: Add Log Line (Duplicate helper, could be in utils but fine here) ---
function addLogLine(text) {
    logOutput.textContent += text + "\n";
    logOutput.scrollTop = logOutput.scrollHeight;
    if (text.trim()) {
        latestLogLine.textContent = text;
    }
}

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

function updateLibraryTable(books) {
    libraryGrid.innerHTML = "";
    if (books.length === 0) {
        libraryGrid.innerHTML =
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
        libraryGrid.appendChild(card);
    });
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
            return (
                title.includes(searchTerm) || author.includes(searchTerm) || narrator.includes(searchTerm)
            );
        });
    }

    // 2. Apply Status Filter
    if (statusFilter) {
        booksToDisplay = booksToDisplay.filter((book) => book.status === statusFilter);
    }

    // 3. Apply Sorting
    switch (sortValue) {
        case "author_asc": booksToDisplay.sort((a, b) => a.author.localeCompare(b.author)); break;
        case "author_desc": booksToDisplay.sort((a, b) => b.author.localeCompare(a.author)); break;
        case "title_asc": booksToDisplay.sort((a, b) => a.title.localeCompare(b.title)); break;
        case "title_desc": booksToDisplay.sort((a, b) => b.title.localeCompare(a.title)); break;
        case "release_date_desc": booksToDisplay.sort((a, b) => new Date(b.release_date) - new Date(a.release_date)); break;
        case "release_date_asc": booksToDisplay.sort((a, b) => new Date(a.release_date) - new Date(b.release_date)); break;
        case "date_added_desc": booksToDisplay.sort((a, b) => new Date(b.date_added) - new Date(a.date_added)); break;
        case "date_added_asc": booksToDisplay.sort((a, b) => new Date(a.date_added) - new Date(b.date_added)); break;
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

// --- Event Listeners ---
document.addEventListener("DOMContentLoaded", () => {
    searchBar.addEventListener("input", renderLibraryGrid);
    sortBy.addEventListener("change", renderLibraryGrid);
    filterByStatus.addEventListener("change", renderLibraryGrid);
});
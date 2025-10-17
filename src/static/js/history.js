let currentPage = 1;
// --- 1. State variables for filters and search ---
let currentJobType = "";
let currentJobStatus = "";
let currentSearchTerm = "";
let debounceTimer;

// --- 2. Run this function when the page content has loaded ---
document.addEventListener("DOMContentLoaded", () => {
    // Get references to the new filter and search elements.
    const jobTypeFilter = document.getElementById("filter-job-type");
    const jobStatusFilter = document.getElementById("filter-job-status");
    const searchInput = document.getElementById("search-history");
    const clearButton = document.getElementById("clear-filters-btn");

    // Attach event listeners to the controls.
    jobTypeFilter.addEventListener("change", () => {
        currentPage = 1; // Reset to first page on filter change
        currentJobType = jobTypeFilter.value;
        fetchAndRenderHistory();
    });

    jobStatusFilter.addEventListener("change", () => {
        currentPage = 1;
        currentJobStatus = jobStatusFilter.value;
        fetchAndRenderHistory();
    });

    // Use a debounce function for the search input to avoid API calls on every keystroke.
    searchInput.addEventListener("input", () => {
        clearTimeout(debounceTimer);
        debounceTimer = setTimeout(() => {
            currentPage = 1;
            currentSearchTerm = searchInput.value;
            fetchAndRenderHistory();
        }, 500); // Wait 500ms after user stops typing
    });

    clearButton.addEventListener("click", () => {
        currentPage = 1;
        currentJobType = "";
        currentJobStatus = "";
        currentSearchTerm = "";
        jobTypeFilter.value = "";
        jobStatusFilter.value = "";
        searchInput.value = "";
        fetchAndRenderHistory();
    });

    // Initial fetch when the page loads.
    fetchAndRenderHistory();
});

/**
 * Renders the pagination controls (buttons, page info).
 * @param {object} paginationData - The pagination info from the API.
 */
function renderPagination(paginationData) {
    // Get references to both the top and bottom containers.
    const containers = [
        document.getElementById("pagination-controls-container-top"),
        document.getElementById("pagination-controls-container"),
    ];

    // Clear any existing controls from both containers first.
    containers.forEach((container) => {
        if (container) container.innerHTML = "";
    });

    const { page, total_pages } = paginationData;

    // If there's only one page (or none), we don't need controls.
    if (total_pages <= 1) {
        return;
    }

    // Loop through each container (top and bottom) to build and append the controls.
    // This ensures both sets of buttons are created and function independently.
    containers.forEach((container) => {
        if (!container) return; // Skip if a container doesn't exist.

        // Create Previous button
        const prevButton = document.createElement("button");
        prevButton.innerHTML = '<i class="fas fa-chevron-left"></i> Previous';
        prevButton.className = "action-button";
        prevButton.disabled = page === 1;
        prevButton.addEventListener("click", () => {
            if (currentPage > 1) {
                currentPage--;
                fetchAndRenderHistory();
            }
        });

        // Create Next button
        const nextButton = document.createElement("button");
        nextButton.innerHTML = 'Next <i class="fas fa-chevron-right"></i>';
        nextButton.className = "action-button";
        nextButton.disabled = page === total_pages;
        nextButton.addEventListener("click", () => {
            if (currentPage < total_pages) {
                currentPage++;
                fetchAndRenderHistory();
            }
        });

        // Create Page Info text
        const pageInfo = document.createElement("span");
        pageInfo.textContent = `Page ${page} of ${total_pages}`;
        pageInfo.style.margin = "0 1.5em";
        pageInfo.style.fontWeight = "600";

        // Assemble the controls inside the current container
        container.appendChild(prevButton);
        container.appendChild(pageInfo);
        container.appendChild(nextButton);
    });
}

/**
 * Fetches job history data from the API and renders it.
 * Now includes filter and search parameters in the request.
 */
async function fetchAndRenderHistory() {
    const historyContainer = document.getElementById("history-list-container");
    historyContainer.innerHTML = "<p>Loading job history...</p>";

    // --- 3. Build the API request URL with search and filter parameters ---
    const params = new URLSearchParams({
        page: currentPage,
    });
    if (currentJobType) params.append("job_type", currentJobType);
    if (currentJobStatus) params.append("job_status", currentJobStatus);
    if (currentSearchTerm) params.append("search_term", currentSearchTerm);

    try {
        const response = await fetch(`/api/jobs/history?${params.toString()}`);
        if (!response.ok) throw new Error("Failed to fetch job history.");

        const data = await response.json();
        const jobs = data.jobs;

        // If no jobs match the criteria, display a helpful message.
        if (jobs.length === 0) {
            historyContainer.innerHTML = "<p>No job history found matching your criteria.</p>";
            renderPagination({ page: 1, total_pages: 0 });
            return;
        }

        historyContainer.innerHTML = ""; // Clear the 'Loading...' message

        jobs.forEach((job) => {
            const jobItem = document.createElement("div");
            jobItem.className = "history-job-item";

            // (The rest of this loop is unchanged)
            let duration = "N/A";
            if (job.start_time && job.end_time) {
                const start = new Date(job.start_time);
                const end = new Date(job.end_time);
                const seconds = Math.round((end - start) / 1000);
                if (seconds < 60) {
                    duration = `${seconds}s`;
                } else {
                    const minutes = Math.floor(seconds / 60);
                    const remainingSeconds = seconds % 60;
                    duration = `${minutes}m ${remainingSeconds}s`;
                }
            }

            let detailsContent = "";
            if (job.job_type === "SYNC") {
                detailsContent =
                    '<p style="padding: 10px 0; margin: 0; font-style: italic; color: #6c757d;">This was a library synchronization task.</p>';
            } else if (job.items && job.items.length > 0) {
                detailsContent = `
            <ul class="history-book-list">
                ${job.items
                    .map(
                        (item) => `
                    <li class="history-book-list-item">
                        <img class="history-book-thumb" src="/covers/${item.asin}_thumb.jpg" alt="Cover for ${item.asin}">
                        <div class="history-book-info">
                            <strong>${item.status}:</strong> ${item.title}
                        </div>
                    </li>
                `,
                    )
                    .join("")}
            </ul>`;
            } else {
                detailsContent =
                    '<p style="padding: 10px 0; margin: 0; font-style: italic; color: #6c757d;">This job had no items to process.</p>';
            }

            jobItem.innerHTML = `
    <div class="history-job-header">
        <div>
            <strong>Job #${job.job_id}</strong> <span style="font-weight: normal;">(${job.job_type} | ${job.status})</span>
            <small style="color: #6c757d; margin-left: 1em;">
                ${new Date(job.start_time).toLocaleString()} | Duration: ${duration}
            </small>
        </div>
        <i class="fas fa-chevron-down"></i>
    </div>
    <div class="history-job-details">
        ${detailsContent}
    </div>
`;
            historyContainer.appendChild(jobItem);
        });

        historyContainer.querySelectorAll(".history-job-header").forEach((header) => {
            header.addEventListener("click", () => {
                header.classList.toggle("active");
            });
        });

        // Render the pagination controls using the data from the API response.
        renderPagination(data);
    } catch (error) {
        console.error("Error rendering job history:", error);
        historyContainer.innerHTML =
            '<p style="color: #721c24;">Error loading job history. Please check the application log.</p>';
    }
}

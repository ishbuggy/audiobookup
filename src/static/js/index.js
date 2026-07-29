// src/static/js/index.js

import { initializeSSEConnection, checkForActiveJob, startSyncJob } from "./modules/job-manager.js";
import { fetchUpdates } from "./modules/library-manager.js";
import { openDownloadSelectionModal } from "./modules/modal-manager.js";

// --- Page Initialization ---
document.addEventListener("DOMContentLoaded", () => {
    // 1. Check System Health
    checkAuthStatus();
    checkAutomationStatus();

    // 2. Initialize Real-time Connection
    // We pass fetchUpdates as a callback so the grid refreshes when a job finishes
    initializeSSEConnection(fetchUpdates);

    // 3. Check for existing work and load data
    checkForActiveJob();
    fetchUpdates();

    // 4. Wire up Main Dashboard Buttons
    document.querySelectorAll(".action-button").forEach((button) => {
        button.addEventListener("click", () => {
            const script = button.dataset.script;
            if (script === "download") {
                openDownloadSelectionModal();
            } else if (script === "sync") {
                startSyncJob(button);
            }
        });
    });

    // 5. Wire up Log Toggle and Copy
    const logContainer = document.getElementById("log-container");
    const toggleLogBtn = document.getElementById("toggle-log-btn");
    const copyLogBtn = document.getElementById("copy-log-btn");
    const downloadLogBtn = document.getElementById("download-log-btn");
    if (toggleLogBtn && logContainer) {
        const toggleIcon = toggleLogBtn.querySelector("i");
        toggleLogBtn.addEventListener("click", () => {
            logContainer.classList.toggle("log-expanded");
            if (logContainer.classList.contains("log-expanded")) {
                toggleIcon.classList.remove("fa-chevron-up");
                toggleIcon.classList.add("fa-chevron-down");
                const logOutput = document.getElementById("log-output");
                if (logOutput) logOutput.scrollTop = logOutput.scrollHeight;
            } else {
                toggleIcon.classList.remove("fa-chevron-down");
                toggleIcon.classList.add("fa-chevron-up");
            }
        });
    }
    if (copyLogBtn) {
        copyLogBtn.addEventListener("click", () => {
            const logText = document.getElementById("log-output").textContent;
            navigator.clipboard
                .writeText(logText)
                .then(() => {
                    if (window.showToast) window.showToast("Log copied to clipboard!", "success");
                    else alert("Log copied!");
                })
                .catch((err) => {
                    console.error("Failed to copy log:", err);
                });
        });
    }
    if (downloadLogBtn) {
        downloadLogBtn.addEventListener("click", () => {
            // Trigger the download by navigating to the API endpoint
            window.location.href = "/api/logs/download";
        });
    }
});

// --- Local Helper Functions (Auth & Automation Checks) ---

async function checkAuthStatus() {
    const authWarningBanner = document.getElementById("auth-warning-banner");
    try {
        const response = await fetch("/api/audible_auth_status");
        const data = await response.json();

        if (!data.is_valid) {
            document.getElementById("auth-warning-message").textContent = data.error;
            authWarningBanner.style.display = "flex";

            document.getElementById("re-auth-btn").addEventListener(
                "click",
                () => {
                    // window.showConfirmationModal and handleResetAuth come from ui.js (global scope)
                    if (window.showConfirmationModal && window.handleResetAuth) {
                        window.showConfirmationModal(
                            '<i class="fas fa-shield-alt"></i> Reset Authentication?',
                            "This will delete your current Audible login credentials and force a full restart of the application. Are you sure you want to proceed?",
                            window.handleResetAuth,
                        );
                    } else {
                        console.error("UI global functions not loaded.");
                    }
                },
                { once: true },
            );
        }
    } catch (error) {
        console.error("Auth check failed:", error);
    }
}

async function checkAutomationStatus() {
    try {
        const response = await fetch("/api/settings");
        const settings = await response.json();
        const banner = document.getElementById("automation-status-banner");
        const bannerText = document.getElementById("automation-status-text");

        const disabledTasks = [];
        if (!settings.tasks.is_auto_fast_sync_enabled && !settings.tasks.is_auto_deep_sync_enabled) {
            disabledTasks.push("library sync");
        }
        if (!settings.tasks.is_auto_process_enabled) {
            disabledTasks.push("download processing");
        }

        if (disabledTasks.length > 0) {
            let message = "";
            if (disabledTasks.length === 1) {
                message = `Automatic <strong>${disabledTasks[0]}</strong> is disabled.`;
            } else {
                const lastTask = disabledTasks.pop();
                message = `Automatic <strong>${disabledTasks.join(", ")}</strong> and <strong>${lastTask}</strong> are disabled.`;
            }

            bannerText.innerHTML = `${message} Click here to configure automation.`;
            banner.style.display = "block";
            banner.onclick = () => {
                window.location.href = "/settings#tasks";
            };
        } else {
            banner.style.display = "none";
        }
    } catch (error) {
        console.error("Could not check automation status:", error);
    }
}

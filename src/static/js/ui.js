// --- Global HTML Escaping Helper ---
// Book metadata (titles, authors, narrators) comes from the Audible API and is
// interpolated into innerHTML template literals across the UI. Escape it so a
// crafted title can't inject markup or scripts. Defined outside the
// DOMContentLoaded handler so ES modules can use it as soon as ui.js runs.
window.escapeHtml = function (value) {
    return String(value ?? '')
        .replaceAll('&', '&amp;')
        .replaceAll('<', '&lt;')
        .replaceAll('>', '&gt;')
        .replaceAll('"', '&quot;')
        .replaceAll("'", '&#39;');
};

// This ensures that the script does not run until the entire HTML document has been loaded and parsed.
document.addEventListener('DOMContentLoaded', () => {

    // --- Custom Alert Logic (Now safe) ---
    const customAlertModal = document.getElementById("custom-alert-modal");
    const customAlertMessage = document.getElementById("custom-alert-message");
    const customAlertOkBtn = document.getElementById("custom-alert-ok-btn");
    const customAlertTitle = document.getElementById("custom-alert-title");

    // --- Confirmation Modal Elements (Now safe) ---
    const confirmationModal = document.getElementById("confirmation-modal");
    const confirmationTitle = document.getElementById("confirmation-title");
    const confirmationMessage = document.getElementById("confirmation-message");
    const confirmationCancelBtn = document.getElementById("confirmation-cancel-btn");
    const confirmationConfirmBtn = document.getElementById("confirmation-confirm-btn");

    let confirmCallback = null;
    
    // --- Make Functions Globally Accessible ---
    window.showCustomAlert = function(message, title = '<i class="fas fa-exclamation-triangle" style="color: #ffc107;"></i> Warning') {
        if (!customAlertModal || !customAlertTitle || !customAlertMessage) return;
        customAlertTitle.innerHTML = title;
        customAlertMessage.innerHTML = message;
        document.body.classList.add("modal-open");
        customAlertModal.style.display = "flex";
    };

    window.closeCustomAlert = function() {
        if (!customAlertModal) return;
        document.body.classList.remove("modal-open");
        customAlertModal.style.display = "none";
    };

    window.showConfirmationModal = function(title, message, onConfirm) {
        if (!confirmationModal) return;
        confirmationTitle.innerHTML = title;
        // Use innerHTML to allow <strong> tags
        confirmationMessage.innerHTML = message; 
        confirmCallback = onConfirm;
        document.body.classList.add("modal-open");
        confirmationModal.style.display = "flex";
    };

    window.closeConfirmationModal = function() {
        if (!confirmationModal) return;
        document.body.classList.remove("modal-open");
        confirmationModal.style.display = "none";
        confirmCallback = null;
    };

    // --- Toast Notification Logic ---
    
    // Create container if it doesn't exist
    let toastContainer = document.getElementById('toast-container');
    if (!toastContainer) {
        toastContainer = document.createElement('div');
        toastContainer.id = 'toast-container';
        document.body.appendChild(toastContainer);
    }

    window.showToast = function(message, type = 'info') {
        const toast = document.createElement('div');
        toast.className = `toast ${type}`;
        
        let iconClass = 'fa-info-circle';
        if (type === 'success') iconClass = 'fa-check-circle';
        if (type === 'error') iconClass = 'fa-exclamation-circle';

        toast.innerHTML = `<i class="fas ${iconClass}"></i> <span>${message}</span>`;
        
        toastContainer.appendChild(toast);

        // Trigger reflow to enable transition
        void toast.offsetWidth;
        toast.classList.add('show');

        // Remove after 3 seconds
        setTimeout(() => {
            toast.classList.remove('show');
            setTimeout(() => {
                if (toastContainer.contains(toast)) {
                    toastContainer.removeChild(toast);
                }
            }, 300); // Wait for fade out
        }, 4000);
    };    

    // --- Function to handle the reset and shutdown sequence ---
    async function handleResetAuth() {
        // Step 1: Show an informational alert that the process has started.
        // CORRECTED: Called the correct, globally available alert function.
        showCustomAlert(
            "Resetting authentication... Please wait.",
            '<i class="fas fa-spinner fa-spin"></i> Processing...',
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

    // Expose the main handler function globally
    window.handleResetAuth = handleResetAuth;

    // --- Attach Event Listeners Safely ---
    if(customAlertOkBtn) {
        customAlertOkBtn.onclick = window.closeCustomAlert;
    }
    
    // --- NEW: Centralized Confirmation Modal Button Listeners ---
    // This is the critical fix. These listeners are now in the same scope
    // as the `confirmCallback` variable and will work on any page.
    if(confirmationCancelBtn) {
        confirmationCancelBtn.addEventListener("click", window.closeConfirmationModal);
    }
    if(confirmationConfirmBtn) {
        confirmationConfirmBtn.addEventListener("click", () => {
            if (typeof confirmCallback === "function") {
                confirmCallback();
            }
            window.closeConfirmationModal();
        });
    }

    // --- Auto Concurrency Detector (Now safe and global) ---
    window.setupAutoConcurrencyDetector = function(buttonId, inputId, altInputId = null) {
        const autoDetectBtn = document.getElementById(buttonId);
        const coresInput = document.getElementById(inputId);
        const altCoresInput = altInputId ? document.getElementById(altInputId) : null;

        if (!autoDetectBtn || !coresInput) {
            console.error("Auto-detect feature could not find required elements:", buttonId, inputId);
            return;
        }

        const handleAutoDetect = async () => {
            const icon = autoDetectBtn.querySelector('i');
            icon.classList.remove('fa-magic');
            icon.classList.add('fa-spinner', 'fa-spin');
            autoDetectBtn.disabled = true;
            try {
                const response = await fetch('/api/get_cpu_cores');
                const data = await response.json();
                if (!response.ok) throw new Error(data.error || 'Server error.');
                
                coresInput.value = data.recommended_concurrency;
                if (altCoresInput) {
                    altCoresInput.value = data.recommended_concurrency;
                }

                window.showCustomAlert(
                    `Detected ${data.total_cores} CPU cores.<br>Recommended concurrency has been set to <strong>${data.recommended_concurrency}</strong>.`,
                    '<i class="fas fa-check-circle" style="color: #28a745;"></i> Success'
                );

            } catch (error) {
                alert(`Could not auto-detect CPU cores: ${error.message}`);
            } finally {
                icon.classList.add('fa-magic');
                icon.classList.remove('fa-spinner', 'fa-spin');
                autoDetectBtn.disabled = false;
            }
        };

        autoDetectBtn.addEventListener('click', handleAutoDetect);
        return handleAutoDetect;
    };
});
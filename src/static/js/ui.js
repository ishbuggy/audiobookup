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
        confirmationMessage.textContent = message;
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

    // --- Attach Event Listeners Safely ---
    if(customAlertOkBtn) {
        customAlertOkBtn.onclick = window.closeCustomAlert;
    }
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
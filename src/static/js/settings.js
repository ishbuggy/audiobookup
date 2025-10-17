// --- 1. WAIT FOR THE DOM TO BE FULLY LOADED ---
document.addEventListener("DOMContentLoaded", () => {
    // --- 2. DEFINE ALL HELPER FUNCTIONS FIRST ---

    /**
     * Finds the currently open accordion panel and recalculates its maxHeight
     * to fit its content. Essential for responsive resizing.
     */
    function recalculateActiveAccordionHeight() {
        const activeAccordion = document.querySelector(".accordion-header.active");
        if (activeAccordion) {
            const activePanel = activeAccordion.nextElementSibling;
            if (activePanel) {
                // Set maxHeight to the panel's current content height
                activePanel.style.maxHeight = activePanel.scrollHeight + "px";
            }
        }
    }

    /**
     * Recalculates and sets the maxHeight of a parent accordion panel.
     * This is necessary when content inside the panel changes height,
     * for example, when showing/hiding the cron input fields.
     * @param {HTMLElement} element - An element inside the accordion panel.
     */
    function updateParentAccordionHeight(element) {
        const parentPanel = element.closest(".accordion-panel");
        // Check if the panel is currently open (has a maxHeight set).
        if (parentPanel && parentPanel.style.maxHeight) {
            // Use a short timeout to allow the browser's rendering engine
            // to update the layout before we measure the new scrollHeight.
            setTimeout(() => {
                parentPanel.style.maxHeight = parentPanel.scrollHeight + "px";
            }, 310); // 310ms is slightly longer than the 0.3s transition.
        }
    }

    /**
     * Handles the visual expansion and collapse of a settings group
     * when its master toggle is checked or unchecked.
     * @param {HTMLInputElement} checkbox - The main enable/disable toggle checkbox.
     * @param {HTMLElement} optionsGroup - The container for the related settings.
     */
    function toggleOptionsGroup(checkbox, optionsGroup) {
        if (checkbox.checked) {
            // To expand, set maxHeight to the element's full scrollable height.
            optionsGroup.style.maxHeight = optionsGroup.scrollHeight + "px";
            optionsGroup.style.opacity = "1";
        } else {
            // To collapse, set maxHeight to 0.
            optionsGroup.style.maxHeight = "0";
            optionsGroup.style.opacity = "0.5";
        }
        // Ensure the parent accordion resizes to fit the change.
        updateParentAccordionHeight(checkbox);
    }

    /**
     * Displays the custom-styled modal alert.
     * @param {string} message - The main message content (can include HTML).
     * @param {string} [title='<i class...'] - The title content (can include HTML for icons).
     */
    function showCustomAlert(
        message,
        title = '<i class="fas fa-exclamation-triangle" style="color: #ffc107;"></i> Warning',
    ) {
        document.getElementById("custom-alert-title").innerHTML = title;
        document.getElementById("custom-alert-message").innerHTML = message;
        document.getElementById("custom-alert-modal").style.display = "flex";
        document.body.classList.add("modal-open");
    }

    /**
     * Hides the custom-styled modal alert.
     */
    function closeCustomAlert() {
        document.getElementById("custom-alert-modal").style.display = "none";
        document.body.classList.remove("modal-open");
    }

    let confirmCallback = null; // To store the action for the confirm button

    function showConfirmationModal(title, message, onConfirm) {
        document.getElementById("confirmation-title").innerHTML = title;
        document.getElementById("confirmation-message").textContent = message;
        confirmCallback = onConfirm;
        document.getElementById("confirmation-modal").style.display = "flex";
        document.body.classList.add("modal-open");
    }

    function closeConfirmationModal() {
        document.getElementById("confirmation-modal").style.display = "none";
        document.body.classList.remove("modal-open");
        confirmCallback = null; // Clear the callback
    }
    /**
     * Handles the API call and UI updates for the manual authentication check.
     * @param {HTMLButtonElement} btn - The "Run Authentication Check" button.
     */
    async function handleManualAudibleAuthCheck(btn) {
        const resultDiv = document.getElementById("audible-auth-check-result");
        btn.classList.add("is-processing");
        btn.disabled = true;
        resultDiv.innerHTML = '<i class="fas fa-spinner fa-spin"></i> Checking...';
        try {
            const response = await fetch("/api/run_audible_auth_check", { method: "POST" });
            const data = await response.json();
            if (!response.ok) throw new Error(data.error || "Server error.");
            if (data.is_valid) {
                resultDiv.innerHTML =
                    '<i class="fas fa-check-circle" style="color: #28a745;"></i> Authentication is valid.';
            } else {
                resultDiv.innerHTML = `<i class="fas fa-times-circle" style="color: #dc3545;"></i> Failed: ${data.error}`;
            }
        } catch (error) {
            resultDiv.innerHTML = `<i class="fas fa-exclamation-triangle"></i> Error: ${error.message}`;
        } finally {
            btn.classList.remove("is-processing");
            btn.disabled = false;
        }
    }

    /**
     * Factory function to create and manage an advanced scheduler widget.
     * Encapsulates all logic for a single schedule (sync or process).
     * @param {string} jobName - The base name for the job (e.g., "fast_sync").
     * @param {string} defaultCron - The default cron string to fall back on.
     * @returns {object} An object with methods to populate and generate cron strings.
     */
    const setupSchedulerWidget = (jobName, defaultCron) => {
        // Get all DOM elements for this specific widget instance.
        const radioButtons = document.querySelectorAll(`input[name="${jobName}_schedule_type"]`);
        const intervalContainer = document.getElementById(`${jobName}-interval-container`);
        const intervalInput = document.getElementById(`${jobName}-interval-hours`);
        const dailyContainer = document.getElementById(`${jobName}-daily-container`);
        const dailyInput = document.getElementById(`${jobName}-daily-time`);
        const cronContainer = document.getElementById(`${jobName}-cron-container`);
        const cronInputs = {
            minute: document.getElementById(`${jobName}-cron-minute`),
            hour: document.getElementById(`${jobName}-cron-hour`),
            dom: document.getElementById(`${jobName}-cron-dom`),
            month: document.getElementById(`${jobName}-cron-month`),
            dow: document.getElementById(`${jobName}-cron-dow`),
        };
        const customLabel = document.getElementById(`${jobName}-custom-label`);

        // Updates which input fields (Interval, Daily, Cron) are visible.
        const updateVisibility = () => {
            const selectedType = document.querySelector(
                `input[name="${jobName}_schedule_type"]:checked`,
            )?.value;
            intervalContainer.style.display = selectedType === "interval" ? "flex" : "none";
            dailyContainer.style.display = selectedType === "daily" ? "flex" : "none";
            cronContainer.style.display = selectedType === "cron" ? "flex" : "none";
            updateParentAccordionHeight(radioButtons[0]);
        };

        // Parses a cron string and updates the UI to match it.
        const populateFromCron = (cronString) => {
            try {
                const parts = cronString.trim().split(/\s+/);
                if (parts.length !== 5) throw new Error("Invalid cron string length");
                const [minute, hour, dom, month, dow] = parts;
                let isSimple = false;

                // Check if the cron string matches the simple "Interval" pattern.
                if (
                    minute === "0" &&
                    hour.startsWith("*/") &&
                    dom === "*" &&
                    month === "*" &&
                    dow === "*"
                ) {
                    document.querySelector(
                        `input[name="${jobName}_schedule_type"][value="interval"]`,
                    ).checked = true;
                    intervalInput.value = hour.substring(2);
                    isSimple = true;
                    // Check if the cron string matches the simple "Daily" pattern.
                } else if (!isNaN(minute) && !isNaN(hour) && dom === "*" && month === "*" && dow === "*") {
                    document.querySelector(
                        `input[name="${jobName}_schedule_type"][value="daily"]`,
                    ).checked = true;
                    dailyInput.value = `${String(hour).padStart(2, "0")}:${String(minute).padStart(2, "0")}`;
                    isSimple = true;
                }

                if (isSimple) {
                    // If it's a simple pattern, show all radio buttons and hide the "Custom" label.
                    radioButtons.forEach((rb) => (rb.parentElement.style.display = "inline-flex"));
                    customLabel.style.display = "none";
                } else {
                    // It's a complex cron string.
                    Object.values(cronInputs).forEach((input, i) => (input.value = parts[i]));
                    if (document.body.classList.contains("advanced-mode")) {
                        // In advanced mode, select the "Cron" radio button.
                        document.querySelector(
                            `input[name="${jobName}_schedule_type"][value="cron"]`,
                        ).checked = true;
                    } else {
                        // In simple mode, hide all radio buttons and show the "Custom" label.
                        radioButtons.forEach((rb) => (rb.parentElement.style.display = "none"));
                        customLabel.style.display = "inline-flex";
                    }
                }
            } catch (e) {
                console.warn(`Could not parse cron string "${cronString}". Falling back to default.`);
                populateFromCron(defaultCron);
            }
            updateVisibility();
        };

        // Reads the current UI state and generates the corresponding cron string.
        const generateCron = () => {
            // If we are in simple mode and have a "Custom" schedule, use the hidden cron values.
            if (
                !document.body.classList.contains("advanced-mode") &&
                customLabel.style.display !== "none"
            ) {
                return Object.values(cronInputs)
                    .map((i) => i.value)
                    .join(" ");
            }
            // Otherwise, generate based on the selected radio button.
            const selectedType = document.querySelector(
                `input[name="${jobName}_schedule_type"]:checked`,
            ).value;
            if (selectedType === "interval") {
                const hours = Math.max(1, parseInt(intervalInput.value, 10));
                return `0 */${hours} * * *`;
            } else if (selectedType === "daily") {
                const [hour, minute] = dailyInput.value.split(":");
                return `${parseInt(minute, 10)} ${parseInt(hour, 10)} * * *`;
            } else {
                return (
                    Object.values(cronInputs)
                        .map((i) => i.value)
                        .join(" ")
                        .trim() || "* * * * *"
                );
            }
        };

        // Add a paste listener to the first cron box for convenience.
        cronInputs.minute.addEventListener("paste", (event) => {
            const paste = (event.clipboardData || window.clipboardData).getData("text");
            const parts = paste.trim().split(/\s+/);
            if (parts.length === 5) {
                event.preventDefault(); // Stop the default paste action.
                Object.values(cronInputs).forEach((input, i) => (input.value = parts[i]));
            }
        });

        radioButtons.forEach((radio) => radio.addEventListener("change", updateVisibility));
        return { populateFromCron, generateCron };
    };

    // --- 3. GET ALL ELEMENT REFERENCES ---
    const accordions = document.querySelectorAll(".accordion-header");
    // Get references for the new sync toggles and panels.
    const autoFastSyncToggle = document.getElementById("auto-fast-sync-toggle");
    const autoFastSyncOptions = document.getElementById("auto-fast-sync-options");
    const autoDeepSyncToggle = document.getElementById("auto-deep-sync-toggle");
    const autoDeepSyncOptions = document.getElementById("auto-deep-sync-options");
    const autoProcessToggle = document.getElementById("auto-process-toggle");
    const autoProcessOptions = document.getElementById("auto-process-options");
    const processErrorCheckbox = document.getElementById("auto-process-error-checkbox");
    const customAlertOkBtn = document.getElementById("custom-alert-ok-btn");
    const customAlertModal = document.getElementById("custom-alert-modal");
    const saveSettingsBtn = document.getElementById("save-settings-btn");
    const exportSettingsBtn = document.getElementById("export-settings-btn");
    const importSettingsBtn = document.getElementById("import-settings-btn");
    const importFileInput = document.getElementById("import-file-input");
    const audibleAuthCheckBtn = document.getElementById("run-audible-auth-check-btn");
    const autoConcurrencyBtn = document.getElementById("auto-concurrency-btn");
    const concurrencyInput = document.getElementById("concurrency-input");
    const advancedModeToggle = document.getElementById("advanced-mode-toggle");
    const totalCoresDisplay = document.getElementById("total-cores-display");
    const totalCoresInput = document.getElementById("total-processing-cores-input");

    // --- 4. ATTACH ALL EVENT LISTENERS ---

    // Create instances of the widget for all three scheduled jobs.
    const fastSyncScheduler = setupSchedulerWidget("fast_sync", "0 */4 * * *");
    const deepSyncScheduler = setupSchedulerWidget("deep_sync", "0 3 * * *");
    const processScheduler = setupSchedulerWidget("process", "0 4 * * *");

    // Advanced Mode Toggle
    const setAdvancedMode = (isAdvanced) => {
        document.body.classList.toggle("advanced-mode", isAdvanced);
        
        // Replace the old logic with a simple call to our new, robust function.
        // Use a timeout to allow the browser to render the new content before we measure it.
        setTimeout(recalculateActiveAccordionHeight, 50);

        // Re-populate all scheduler widgets to reflect the mode change.
        // NOTE: No fetch is needed here, we just need to re-run the populate logic.
        const settingsData = {
            tasks: {
                fast_sync_schedule: { cron: fastSyncScheduler.generateCron() },
                deep_sync_schedule: { cron: deepSyncScheduler.generateCron() },
                process_schedule: { cron: processScheduler.generateCron() },
            }
        };
        fastSyncScheduler.populateFromCron(settingsData.tasks.fast_sync_schedule.cron);
        deepSyncScheduler.populateFromCron(settingsData.tasks.deep_sync_schedule.cron);
        processScheduler.populateFromCron(settingsData.tasks.process_schedule.cron);
    };
    advancedModeToggle.addEventListener("change", () => setAdvancedMode(advancedModeToggle.checked));

    // Accordions
    accordions.forEach((acc) => {
        acc.addEventListener("click", function () {
            this.classList.toggle("active");
            const panel = this.nextElementSibling;
            if (panel.style.maxHeight) {
                panel.style.maxHeight = null;
            } else {
                panel.style.maxHeight = panel.scrollHeight + "px";
            }
        });
    });

    // Main Enable/Disable Toggles for all three sections.
    autoFastSyncToggle.addEventListener("change", () =>
        toggleOptionsGroup(autoFastSyncToggle, autoFastSyncOptions),
    );
    autoDeepSyncToggle.addEventListener("change", () =>
        toggleOptionsGroup(autoDeepSyncToggle, autoDeepSyncOptions),
    );
    autoProcessToggle.addEventListener("change", () =>
        toggleOptionsGroup(autoProcessToggle, autoProcessOptions),
    );
    const confirmationCancelBtn = document.getElementById("confirmation-cancel-btn");
    const confirmationConfirmBtn = document.getElementById("confirmation-confirm-btn");
    const confirmationModal = document.getElementById("confirmation-modal");

    confirmationCancelBtn.addEventListener("click", closeConfirmationModal);
    confirmationConfirmBtn.addEventListener("click", () => {
        if (typeof confirmCallback === "function") {
            confirmCallback();
        }
        closeConfirmationModal();
    });
    // Also add the window click listener for the new modal
    window.addEventListener("click", (event) => {
        if (event.target == confirmationModal) closeConfirmationModal();
    });

    // --- Accordion Resize on Window Change ---
    // This ensures that if an accordion is open and the window is resized
    // (causing content to wrap), the accordion's height is recalculated.
    let resizeTimer;
    window.addEventListener("resize", () => {
        clearTimeout(resizeTimer);
        resizeTimer = setTimeout(recalculateActiveAccordionHeight, 320);
    });
    // Save Button (integrates cron generation)
    saveSettingsBtn.addEventListener("click", async () => {
        const usernameInput = document.getElementById("new_username"); // Get the input element itself
        const newUsername = usernameInput.value.trim();
        const newPassword = document.getElementById("new_password").value;
        const confirmPassword = document.getElementById("confirm_password").value;

        // Correctly get the initial username from the data attribute set in the HTML
        const initialUsername = usernameInput.dataset.initialUsername;

        // This logic is now robust and correct.
        const passwordHasChanged = newPassword !== "" && newPassword === confirmPassword;
        const usernameHasChanged = newUsername !== initialUsername;
        const credentialsHaveChanged = passwordHasChanged || usernameHasChanged;

        const performSave = async () => {
            const settingsToSave = {};
            // Gather all standard settings using the data-path attribute.
            document.querySelectorAll(".setting-input").forEach((input) => {
                const path = input.dataset.path.split(".");
                let current = settingsToSave;
                for (let i = 0; i < path.length - 1; i++) {
                    current = current[path[i]] = current[path[i]] || {};
                }
                const value =
                    input.type === "checkbox"
                        ? input.checked
                        : input.type === "number"
                          ? Number(input.value)
                          : input.value;
                current[path[path.length - 1]] = value;
            });

            // Generate and add the cron strings from all three widgets.
            settingsToSave.tasks.fast_sync_schedule = { cron: fastSyncScheduler.generateCron() };
            settingsToSave.tasks.deep_sync_schedule = { cron: deepSyncScheduler.generateCron() };
            settingsToSave.tasks.process_schedule = { cron: processScheduler.generateCron() };

            // Manually check the new password field's value.
            const newPasswordValue = document.getElementById("new_password").value;

            // Only include the 'password' key in the payload if the user actually entered a new one.
            if (newPasswordValue) {
                settingsToSave.password = newPasswordValue;
            }

            // Sanity check for overly frequent schedules.
            const checkFrequency = (cron, name) => {
                const parts = cron.split(" ");
                const minutePart = parts[0];
                if (
                    minutePart === "*" ||
                    (minutePart.startsWith("*/") && parseInt(minutePart.substring(2)) < 5)
                ) {
                    showCustomAlert(
                        `The schedule for the <strong>${name} job</strong> is set to run more frequently than every 5 minutes. This is not recommended and may cause issues.`,
                    );
                }
            };
            checkFrequency(settingsToSave.tasks.fast_sync_schedule.cron, "Fast Sync");
            checkFrequency(settingsToSave.tasks.deep_sync_schedule.cron, "Deep Sync");
            checkFrequency(settingsToSave.tasks.process_schedule.cron, "Process");

            // Send the settings to the backend.
            try {
                const response = await fetch("/api/settings", {
                    method: "POST",
                    headers: { "Content-Type": "application/json" },
                    body: JSON.stringify(settingsToSave),
                });
                if (!response.ok) throw new Error("Server returned an error.");

                // Handle redirect on credential change
                if (credentialsHaveChanged) {
                    showCustomAlert(
                        "Credentials updated successfully! You will be logged out and redirected to the login page.",
                        '<i class="fas fa-check-circle" style="color: #28a745;"></i> Success',
                    );
                    // Wait 3 seconds then redirect to login
                    setTimeout(() => {
                        window.location.href = "/login";
                    }, 3000);
                } else {
                    // Provide visual feedback on success for non-auth changes.
                    saveSettingsBtn.textContent = "Saved!";
                    saveSettingsBtn.classList.remove("blue");
                    saveSettingsBtn.classList.add("green");

                    setTimeout(() => {
                        saveSettingsBtn.textContent = "Save Changes";
                        saveSettingsBtn.classList.remove("green");
                        saveSettingsBtn.classList.add("blue");
                    }, 2000);
                }
            } catch (error) {
                showCustomAlert(
                    `Could not save settings: ${error.message}`,
                    '<i class="fas fa-times-circle" style="color: #dc3545;"></i> Error',
                );
            }
        };

        // --- Confirmation Logic ---
        if (credentialsHaveChanged) {
            showConfirmationModal(
                '<i class="fas fa-exclamation-triangle" style="color: #dc3545;"></i> Confirm Changes',
                "Changing your username or password will log you out immediately. Are you sure you want to proceed?",
                performSave, // The save function is the callback
            );
        } else {
            // If no credentials changed, save immediately without confirmation.
            performSave();
        }
    });

    // Other Button Event Listeners
    customAlertOkBtn.addEventListener("click", closeCustomAlert);
    window.addEventListener("click", (event) => {
        if (event.target == customAlertModal) closeCustomAlert();
    });
    audibleAuthCheckBtn.addEventListener("click", () => handleManualAudibleAuthCheck(audibleAuthCheckBtn));

// Auto Concurrency Button
    // We now only need to target the single, always-visible input field.
    setupAutoConcurrencyDetector(
        "auto-concurrency-btn",
        "total-processing-cores-input"
    );

    // "Run Now" Buttons
    document.querySelectorAll(".run-now-btn").forEach((btn) => {
        const jobType = btn.dataset.jobType;
        // --- START: MODIFICATION ---
        // The toggle ID is now more specific, e.g., "auto-fast-sync-toggle"
        const toggleId = `auto-${jobType.toLowerCase().replace("_", "-")}-toggle`;
        const toggle = document.getElementById(toggleId);
        // --- END: MODIFICATION ---

        // Enable/disable the button based on the main toggle's state.
        const updateButtonState = () => {
            btn.disabled = !toggle.checked;
        };
        toggle.addEventListener("change", updateButtonState);

        btn.addEventListener("click", async () => {
            const icon = btn.querySelector("i");
            icon.classList.remove("fa-play");
            icon.classList.add("fa-spinner", "fa-spin");
            btn.disabled = true;

            try {
                const response = await fetch("/api/run_scheduled_job_now", {
                    method: "POST",
                    headers: { "Content-Type": "application/json" },
                    body: JSON.stringify({ job_type: jobType }),
                });
                const data = await response.json();
                if (!response.ok) throw new Error(data.error || "Failed to start job.");

                showCustomAlert(
                    `The ${jobType.toLowerCase().replace("_", " ")} job was started successfully. You can now return to the dashboard to monitor its progress.`,
                    '<i class="fas fa-check-circle" style="color: #28a745;"></i> Job Started',
                );
            } catch (error) {
                showCustomAlert(
                    `Could not start the job: ${error.message}`,
                    '<i class="fas fa-times-circle" style="color: #dc3545;"></i> Error',
                );
            } finally {
                icon.classList.add("fa-play");
                icon.classList.remove("fa-spinner", "fa-spin");
                updateButtonState(); // Re-set disabled state based on the toggle.
            }
        });

        updateButtonState(); // Set initial state on page load.
    });

    // "Process Error" Checkbox Warning
    processErrorCheckbox.addEventListener("change", () => {
        if (processErrorCheckbox.checked) {
            showCustomAlert(
                "Enabling automatic processing for <strong>ERROR</strong> books is not recommended for persistent issues.<br><br>The system will only attempt to re-download each failed book <strong>ONCE</strong> automatically.",
            );
        }
    });

    // Export/Import Settings Buttons
    exportSettingsBtn.addEventListener("click", async () => {
        try {
            const response = await fetch("/api/settings");
            const settings = await response.json();
            const blob = new Blob([JSON.stringify(settings, null, 2)], { type: "application/json" });
            const url = URL.createObjectURL(blob);
            const a = document.createElement("a");
            a.href = url;
            a.download = "audible_downloader_settings.json";
            document.body.appendChild(a);
            a.click();
            document.body.removeChild(a);
            URL.revokeObjectURL(url);
        } catch (error) {
            showCustomAlert(
                "Could not export settings.",
                '<i class="fas fa-times-circle" style="color: #dc3545;"></i> Error',
            );
        }
    });

    importSettingsBtn.addEventListener("click", () => importFileInput.click());

    importFileInput.addEventListener("change", (event) => {
        const file = event.target.files[0];
        if (!file) return;
        const reader = new FileReader();
        reader.onload = async (e) => {
            try {
                const settings = JSON.parse(e.target.result);
                const response = await fetch("/api/settings", {
                    method: "POST",
                    headers: { "Content-Type": "application/json" },
                    body: JSON.stringify(settings),
                });
                if (!response.ok) throw new Error("Server rejected the settings file.");
                showCustomAlert(
                    "Settings imported successfully! The page will now reload.",
                    '<i class="fas fa-check-circle" style="color: #28a745;"></i> Success',
                );
                setTimeout(() => window.location.reload(), 2000);
            } catch (error) {
                showCustomAlert(
                    `Error importing settings: ${error.message}`,
                    '<i class="fas fa-times-circle" style="color: #dc3545;"></i> Error',
                );
            }
        };
        reader.readAsText(file);
        importFileInput.value = ""; // Clear the input for subsequent imports.
    });

    // --- 5. INITIAL PAGE LOAD ---
    
    // Set the initial visibility of the scheduler options based on the server-rendered state of the toggles.
    toggleOptionsGroup(autoFastSyncToggle, autoFastSyncOptions);
    toggleOptionsGroup(autoDeepSyncToggle, autoDeepSyncOptions);
    toggleOptionsGroup(autoProcessToggle, autoProcessOptions);

    // Fetch settings to determine the initial state of Advanced Mode and populate scheduler widgets.
    fetch("/api/settings")
        .then((res) => res.json())
        .then((settings) => {
            // This will set the body class and populate the cron widgets correctly.
            setAdvancedMode(settings.advanced_mode_enabled);
        });

    // Check if the URL has the '#tasks' hash
    if (window.location.hash === "#tasks") {
        const tasksHeader = document.getElementById("tasks-accordion-header");
        if (tasksHeader) {
            // --- START: Final Multi-Stage Animation Sequence ---

            // Step 1: After a delay, scroll the top of the header to the top of the viewport.
            setTimeout(() => {
                tasksHeader.scrollIntoView({ behavior: "smooth", block: "start" });
            }, 500); // 0.5s delay

            // Step 2: After the scroll starts, click to trigger the opening animation.
            setTimeout(() => {
                tasksHeader.click();
            }, 700); // 1.0s total delay

            // Step 3: Re-scroll to the SAME header AFTER the animation has finished.
            // This corrects the scroll position to account for the newly visible panel content.
            // The CSS animation is 300ms, so we wait 400ms to be safe.
            setTimeout(() => {
                tasksHeader.scrollIntoView({ behavior: "smooth", block: "start" });
            }, 900); // 1.4s total delay (1000ms + 400ms)

            // Step 4: Apply the highlight flash after the final scroll has settled.
            setTimeout(() => {
                const settingsToHighlight =
                    tasksHeader.nextElementSibling.querySelectorAll(".toggle-control");
                settingsToHighlight.forEach((el) => {
                    el.classList.add("highlight-flash");
                });
            }, 1100); // 2.0s total delay

            // --- END: Final Multi-Stage Animation Sequence ---
        }
    }
});

// This script handles client-side validation for the new password fields.
const password = document.getElementById("new_password");
const confirm_password = document.getElementById("confirm_password");

function validatePassword() {
    if (password.value !== confirm_password.value) {
        confirm_password.setCustomValidity("Passwords Don't Match");
    } else {
        confirm_password.setCustomValidity("");
    }
}
// Add event listeners to check on change or keyup
if (password && confirm_password) {
    password.onchange = validatePassword;
    confirm_password.onkeyup = validatePassword;
}

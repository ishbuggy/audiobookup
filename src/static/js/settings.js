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
     * Shows/hides the format-dependent controls in the Audio & Output Format
     * section: the AAC-quality row appears only for the "m4b" format, and the
     * MP3/LAME options block appears only for "mp3". Advanced-mode gating still
     * applies independently to the individual controls inside the MP3 block.
     */
    function updateFormatVisibility() {
        const formatSelect = document.getElementById("output-format-select");
        if (!formatSelect) return;
        const fmt = formatSelect.value;
        const mp3Block = document.getElementById("mp3-options");
        const aacRow = document.getElementById("aac-quality-row");
        if (mp3Block) mp3Block.classList.toggle("hidden", fmt !== "mp3");
        if (aacRow) aacRow.classList.toggle("hidden", fmt !== "m4b");
        // The open accordion's fixed maxHeight must grow/shrink with the change.
        updateParentAccordionHeight(formatSelect);
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
            const selectedType = document.querySelector(`input[name="${jobName}_schedule_type"]:checked`)?.value;
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
                if (minute === "0" && hour.startsWith("*/") && dom === "*" && month === "*" && dow === "*") {
                    document.querySelector(`input[name="${jobName}_schedule_type"][value="interval"]`).checked = true;
                    intervalInput.value = hour.substring(2);
                    isSimple = true;
                    // Check if the cron string matches the simple "Daily" pattern.
                } else if (!isNaN(minute) && !isNaN(hour) && dom === "*" && month === "*" && dow === "*") {
                    document.querySelector(`input[name="${jobName}_schedule_type"][value="daily"]`).checked = true;
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
                        document.querySelector(`input[name="${jobName}_schedule_type"][value="cron"]`).checked = true;
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
            if (!document.body.classList.contains("advanced-mode") && customLabel.style.display !== "none") {
                return Object.values(cronInputs)
                    .map((i) => i.value)
                    .join(" ");
            }
            // Otherwise, generate based on the selected radio button.
            const selectedType = document.querySelector(`input[name="${jobName}_schedule_type"]:checked`).value;
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

    const verifyLibBtn = document.getElementById("verify-library-btn");

    if (verifyLibBtn) {
        verifyLibBtn.addEventListener("click", () => {
            window.showConfirmationModal(
                '<i class="fas fa-stethoscope"></i> Verify Library?',
                "This will scan all downloaded files to check for corruption or truncation. Any bad files will be marked as ERROR in your library. This may take a few minutes.",
                async () => {
                    try {
                        // Start the job via the standard API
                        const response = await fetch("/api/jobs/start", {
                            method: "POST",
                            headers: { "Content-Type": "application/json" },
                            body: JSON.stringify({ job_type: "VERIFY" }),
                        });
                        const data = await response.json();
                        if (response.ok && data.success) {
                            window.showCustomAlert(
                                "Verification started. Check the Dashboard for progress.",
                                '<i class="fas fa-check-circle" style="color: #28a745;"></i> Started',
                            );
                        } else {
                            throw new Error(data.error || "Failed to start job.");
                        }
                    } catch (error) {
                        window.showCustomAlert(`Error: ${error.message}`);
                    }
                },
            );
        });
    }

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
    const resetAuthBtn = document.getElementById("reset-auth-btn");
    const clearCacheBtn = document.getElementById("clear-cache-btn");

    // --- 4. ATTACH ALL EVENT LISTENERS ---

    // Create instances of the widget for all three scheduled jobs.
    const fastSyncScheduler = setupSchedulerWidget("fast_sync", "0 */4 * * *");
    const deepSyncScheduler = setupSchedulerWidget("deep_sync", "0 3 * * *");
    const processScheduler = setupSchedulerWidget("process", "0 4 * * *");

    if (resetAuthBtn) {
        resetAuthBtn.addEventListener("click", () => {
            // Use the global confirmation modal function from ui.js
            showConfirmationModal(
                '<i class="fas fa-trash-alt"></i> Reset Audible Connection?',
                "This will delete your current Audible login credentials and force the application to restart. You will be required to complete the setup wizard again. Are you sure?",
                // The third argument is the callback function to run on confirmation.
                // We call the global handleResetAuth function from ui.js.
                handleResetAuth,
            );
        });
    }

    // START: CLEAR CACHE BUTTON
    if (clearCacheBtn) {
        // This is the function that will be called if the user clicks "Confirm".
        const handleClearCache = async () => {
            // Immediately show a "processing" alert to the user.
            showCustomAlert(
                "Clearing the image cache... Please wait.",
                '<i class="fas fa-spinner fa-spin"></i> Processing...',
            );

            try {
                // Call the new API endpoint.
                const response = await fetch("/api/clear_image_cache", { method: "POST" });
                const data = await response.json();

                if (!response.ok || !data.success) {
                    throw new Error(data.error || "Failed to clear cache on the server.");
                }

                // On success, update the alert with the success message from the server.
                showCustomAlert(data.message, '<i class="fas fa-check-circle" style="color: #28a745;"></i> Success');
            } catch (error) {
                console.error("Clear image cache failed:", error);
                // On failure, update the alert to show the error.
                showCustomAlert(
                    `Could not clear the image cache: ${error.message}`,
                    '<i class="fas fa-times-circle" style="color: #dc3545;"></i> Error',
                );
            }
        };

        // Attach the main click listener to the button.
        clearCacheBtn.addEventListener("click", () => {
            // Use our global confirmation modal function from ui.js.
            showConfirmationModal(
                '<i class="fas fa-broom"></i> Clear Image Cache?',
                "This will delete all downloaded cover art. Are you sure you want to proceed? The images will be re-downloaded on the next library sync.",
                // Pass the handleClearCache function as the callback to run on confirmation.
                handleClearCache,
            );
        });
    }

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
            },
        };
        fastSyncScheduler.populateFromCron(settingsData.tasks.fast_sync_schedule.cron);
        deepSyncScheduler.populateFromCron(settingsData.tasks.deep_sync_schedule.cron);
        processScheduler.populateFromCron(settingsData.tasks.process_schedule.cron);
    };
    advancedModeToggle.addEventListener("change", () => setAdvancedMode(advancedModeToggle.checked));

    // Output-format select: toggle the AAC-quality row and MP3 options block.
    const outputFormatSelect = document.getElementById("output-format-select");
    if (outputFormatSelect) {
        outputFormatSelect.addEventListener("change", updateFormatVisibility);
    }

    // Live-update the numeric readout next to the VBR quality slider.
    const vbrSlider = document.getElementById("mp3-vbr-quality");
    const vbrValue = document.getElementById("mp3-vbr-quality-value");
    if (vbrSlider && vbrValue) {
        vbrSlider.addEventListener("input", () => {
            vbrValue.textContent = vbrSlider.value;
        });
    }

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
    autoFastSyncToggle.addEventListener("change", () => toggleOptionsGroup(autoFastSyncToggle, autoFastSyncOptions));
    autoDeepSyncToggle.addEventListener("change", () => toggleOptionsGroup(autoDeepSyncToggle, autoDeepSyncOptions));
    autoProcessToggle.addEventListener("change", () => toggleOptionsGroup(autoProcessToggle, autoProcessOptions));

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

        // Guard against a mistyped confirmation before anything is sent. Both fields blank
        // means "keep the current password" — but if EITHER field is filled and they disagree
        // (including text typed only into Confirm), the *entire* save is aborted. Blocking the
        // whole save (rather than quietly dropping just the password) is deliberate: the
        // alternative is a user who sees "Settings saved!", stays logged in, and only discovers
        // the typo when they are locked out at the next login.
        if ((newPassword !== "" || confirmPassword !== "") && newPassword !== confirmPassword) {
            showCustomAlert(
                "The new password and its confirmation do not match. Nothing was saved — please re-enter both fields.",
                '<i class="fas fa-times-circle" style="color: #dc3545;"></i> Passwords Do Not Match',
            );
            return;
        }

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
                // Radio groups share one data-path; only the selected radio should
                // contribute its value (otherwise the last one iterated would win).
                if (input.type === "radio" && !input.checked) return;
                const path = input.dataset.path.split(".");
                let current = settingsToSave;
                for (let i = 0; i < path.length - 1; i++) {
                    current = current[path[i]] = current[path[i]] || {};
                }
                const value =
                    input.type === "checkbox"
                        ? input.checked
                        : input.type === "number" || input.type === "range"
                          ? Number(input.value)
                          : input.value;
                current[path[path.length - 1]] = value;
            });

            // Generate and add the cron strings from all three widgets.
            settingsToSave.tasks.fast_sync_schedule = { cron: fastSyncScheduler.generateCron() };
            settingsToSave.tasks.deep_sync_schedule = { cron: deepSyncScheduler.generateCron() };
            settingsToSave.tasks.process_schedule = { cron: processScheduler.generateCron() };

            // The password input carries no data-path, so the sweep above cannot have picked it
            // up — this is the only place a new password can enter the payload. We send the value
            // captured when the button was clicked (already checked against Confirm) rather than
            // re-reading the field, so what gets hashed is exactly what was validated.
            if (passwordHasChanged) {
                settingsToSave.password = newPassword;
            }

            // Sanity check for overly frequent schedules.
            const checkFrequency = (cron, name) => {
                const parts = cron.split(" ");
                const minutePart = parts[0];
                if (minutePart === "*" || (minutePart.startsWith("*/") && parseInt(minutePart.substring(2)) < 5)) {
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
                    if (window.showToast) {
                        window.showToast("Settings saved successfully!", "success");
                    }
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

    audibleAuthCheckBtn.addEventListener("click", () => handleManualAudibleAuthCheck(audibleAuthCheckBtn));

    // Auto Concurrency Button
    // We now only need to target the single, always-visible input field.
    setupAutoConcurrencyDetector("auto-concurrency-btn", "total-processing-cores-input");

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

    // Set the initial visibility of the format-dependent controls (AAC quality
    // row / MP3 block) from the server-rendered output-format value.
    updateFormatVisibility();

    // Fetch settings to determine the initial state of Advanced Mode and populate scheduler widgets.
    fetch("/api/settings")
        .then((res) => res.json())
        .then((settings) => {
            // Populate the widgets with server data first
            if (settings.tasks) {
                fastSyncScheduler.populateFromCron(settings.tasks.fast_sync_schedule.cron);
                deepSyncScheduler.populateFromCron(settings.tasks.deep_sync_schedule.cron);
                processScheduler.populateFromCron(settings.tasks.process_schedule.cron);
            }

            // This will set the body class and update the UI visibility
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
                const settingsToHighlight = tasksHeader.nextElementSibling.querySelectorAll(".toggle-control");
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

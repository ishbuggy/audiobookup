document.addEventListener("DOMContentLoaded", () => {
    // --- Element References ---
    const step1 = document.getElementById("setup-step-1");
    const step2 = document.getElementById("setup-step-2");
    const step3 = document.getElementById("setup-step-3");

    // Step 1 Elements
    const countrySelect = document.getElementById("country-select");
    const advancedToggle = document.getElementById("advanced-options-toggle");
    const advancedPanel = document.getElementById("advanced-options-panel");
    const profileNameInput = document.getElementById("profile-name");
    const authFileNameInput = document.getElementById("auth-file-name");
    const encryptToggle = document.getElementById("encrypt-file-toggle");
    const passwordPanel = document.getElementById("password-panel");
    const passwordInput = document.getElementById("auth-file-password");
    const passwordConfirmInput = document.getElementById("auth-file-password-confirm");
    const startSetupBtn = document.getElementById("start-setup-btn");

    // Step 2 Elements
    const openAudibleBtn = document.getElementById("open-audible-btn");
    const pasteUrlInput = document.getElementById("paste-url");
    const submitUrlBtn = document.getElementById("submit-url-btn");

    // --- Initial State & Event Listeners ---

    // Auto-generate default names when country changes
    countrySelect.addEventListener("change", () => {
        if (!advancedToggle.checked) {
            const country = countrySelect.value;
            profileNameInput.value = `profile_${country}`;
            authFileNameInput.value = `auth_${country}`;
        }
    });
    // Trigger change event to populate initial values
    countrySelect.dispatchEvent(new Event("change"));

    // Show/hide advanced panel
    advancedToggle.addEventListener("change", () => {
        advancedPanel.style.display = advancedToggle.checked ? "block" : "none";
    });

    // Show/hide password panel
    encryptToggle.addEventListener("change", () => {
        passwordPanel.style.display = encryptToggle.checked ? "block" : "none";
    });

    // --- Socket.IO Connection ---
    const socket = io({ path: "/setup/socket.io" });

    socket.on("connect", () => {
        console.log("Socket.IO connection established for setup.");
    });

    socket.on("disconnect", () => {
        console.log("Socket.IO disconnected.");
    });

    // Listen for the URL from the backend
    socket.on("audible_login_url_ready", (data) => {
        openAudibleBtn.onclick = () => window.open(data.url, "_blank");
        openAudibleBtn.innerHTML = '<i class="fas fa-external-link-alt"></i> Open Audible Login Page';
        openAudibleBtn.disabled = false;
    });

    // Listen for the final output from the PTY process
    socket.on("pty_output", (data) => {
        // Check for success or failure keywords in the final output
        if (data.output.includes("SUCCESS! Authentication is valid")) {
            step2.style.display = "none";
            step3.innerHTML = `
                <h1 style="color: #155724;"><i class="fas fa-check-circle"></i> Success!</h1>
                <p>Authentication complete. You will be redirected to the dashboard automatically.</p>
            `;
            step3.style.display = "block";
            setTimeout(() => (window.location.href = "/"), 3000);
        } else if (data.output.includes("FAILED")) {
            step2.style.display = "none";
            step3.innerHTML = `
                <h1 style="color: #721c24;"><i class="fas fa-times-circle"></i> Failed</h1>
                <p>Authentication failed. Please restart the container and try again.</p>
                <pre style="background: #f8d7da; padding: 1em; border-radius: 5px; text-align: left; white-space: pre-wrap;">${data.output}</pre>
            `;
            step3.style.display = "block";
        }
    });

    // --- Button Click Handlers ---

    // START SETUP button
    startSetupBtn.addEventListener("click", () => {
        // Validate password confirmation if encryption is enabled
        if (encryptToggle.checked) {
            if (passwordInput.value.length === 0) {
                alert("Password cannot be empty when encryption is enabled.");
                return;
            }
            if (passwordInput.value !== passwordConfirmInput.value) {
                alert("Passwords do not match.");
                return;
            }
        }

        // Collect all form data
        const setupData = {
            profile_name: profileNameInput.value,
            auth_file: authFileNameInput.value,
            country_code: countrySelect.value,
            with_username: document.getElementById("legacy-account-toggle").checked,
            encrypt: encryptToggle.checked,
            auth_file_password: encryptToggle.checked ? passwordInput.value : null,
        };

        // Send data to backend to start the process
        socket.emit("start_audible_setup", setupData);

        // Transition UI to the next step
        step1.style.display = "none";
        step2.style.display = "block";
    });

    // SUBMIT URL button
    submitUrlBtn.addEventListener("click", () => {
        const url = pasteUrlInput.value;
        if (url) {
            // Add a newline to simulate pressing Enter in the terminal
            socket.emit("pty_input", { input: url + "\\n" });
            submitUrlBtn.innerHTML = '<i class="fas fa-spinner fa-spin"></i> Submitting...';
            submitUrlBtn.disabled = true;
            pasteUrlInput.disabled = true;
        }
    });
});

document.addEventListener("DOMContentLoaded", () => {
    const step2Div = document.getElementById("setup-step-2");
    const step3Div = document.getElementById("setup-step-3");
    const saveBtn = document.getElementById("setup-save-settings-btn");
    const socket = io({ path: "/setup/socket.io" });

    // 1. Setup the auto-detect functionality and get a reference to the handler
    const triggerAutoDetect = setupAutoConcurrencyDetector(
        "setup-auto-concurrency-btn",
        "setup-total-cores-input",
    );

    // 2. Listen for the success event from the backend
    socket.on("audible_setup_successful", () => {
        step2Div.style.display = "none";
        step3Div.style.display = "block";
        // 3. The user can trigger the autodetect, or continue with the default number of threads
    });

    // 4. Setup the save button (this logic is unique to this page)
    const handleSaveAndContinue = async () => {
        const coresInput = document.getElementById("setup-total-cores-input");
        const cores = parseInt(coresInput.value, 10);
        if (!cores || cores < 1) {
            alert("Please enter a valid number of cores (at least 1).");
            return;
        }
        saveBtn.classList.add("is-processing");
        saveBtn.disabled = true;
        const settingsToSave = { job: { download: { total_processing_cores: cores } } };
        try {
            const response = await fetch("/api/settings", {
                method: "POST",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify(settingsToSave),
            });
            if (!response.ok) throw new Error("Failed to save settings.");
            window.location.href = "/";
        } catch (error) {
            alert(`Error: ${error.message}. Redirecting, but please verify in settings.`);
            setTimeout(() => {
                window.location.href = "/";
            }, 2000);
        }
    };
    saveBtn.addEventListener("click", handleSaveAndContinue);
});

# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [0.14.1] - 2025-10-14

This is a bugfix and user experience release that improves the first-run setup process, standardizes the UI, and provides a clearer installation path for new users.

### Added

- **Interactive Performance Optimization:** Added a new final step to the initial setup wizard that prompts the user to auto-detect and set the optimal number of processing cores for their system, ensuring the best performance from day one.
- **Server Status & Version Footer:** The application version and a "Server Online" indicator are now displayed in the footer of the application pages for clear, at-a-glance feedback.
- **Developer Workflow:** Added a `docker-compose.dev.yml.template` to the repository to formalize and simplify the build-from-source workflow for developers.

### Changed

- **MAJOR: Documentation Overhaul:** The `readme.md` has been completely rewritten for end-users. It now provides a simple copy-paste `docker-compose.yml` and includes dedicated, easy-to-follow installation guides for standard Docker Compose and Unraid.
- **`docker-compose.yml` Simplification:** The main `docker-compose.yml` in the repository is now the user-facing version, containing only the `image` directive to prevent errors on platforms like Unraid's Compose Manager. The `build` configuration is now exclusively in the developer template.
- **Code Quality:** Refactored some duplicated JavaScript for modals and the auto-concurrency detector into a single, shared `src/static/js/common.js` file to improve maintainability.

### Fixed

- **UI/UX (First Run):** Fixed numerous UI/UX bugs across all setup pages, including:
    - Incorrect dark mode colors and inconsistent element widths.
    - Improper alignment of branding headers and the theme toggle button.
    - Fixed an issue where the custom alert modal would render HTML tags as plain text instead of formatted content.

## [0.14.0] - 2025-10-13

This is a foundational release focused on making the project fully portable, maintainable, and ready for public distribution via GitHub and Docker Compose. It introduces a portable `docker-compose.yml`, a `.gitignore` to ensure a clean repository, and fixes several critical bugs related to the application lifecycle and temporary file handling.

### Added

- **`.gitignore` File:** Added a comprehensive `.gitignore` file to prevent local development files (`.venv`, `node_modules`), user data (`appdata/`), editor settings (`.vscode/`), and other unnecessary files from being committed to the repository.
- **`docker-compose.override.yml` Support:** The project now implicitly supports a `docker-compose.override.yml` file, allowing users to specify their personal, absolute volume paths without modifying the main, portable compose file.
- **Attribution Headers:** Added proper license and attribution headers to `chunked_conversion_logic.py` and `processing_logic.py` to credit the original work they were adapted from.
- **Live Task Runner Reconfiguration:** The `TaskRunner` can now be reconfigured on-the-fly. Changes made to the "Total Processing Cores" setting in the UI now take effect immediately without requiring a container restart.

### Changed

- **MAJOR: `docker-compose.yml` for Portability:** The `docker-compose.yml` file has been completely overhauled to be portable and user-friendly for a public GitHub release.
    - Replaced all hardcoded, absolute volume paths with relative paths (`./appdata`, `./audiobooks`), allowing for an "out-of-the-box" setup.
    - Added extensive comments to guide new users through configuration.
    - Added the standard `TZ` environment variable for robust timezone support in scheduled tasks.
    - Renamed the service and container to `audiobookup` to match project branding.
    - Removed the obsolete `version` tag.
- **Temporary File Handling:** All temporary files generated during the download and conversion process are now created in a dedicated `/config/temp_processing` directory inside a mapped volume. This prevents the container's internal filesystem from filling up, fixing a critical issue for systems with limited Docker image sizes (like Unraid).
- **`readme.md` Update:** The "Installation" and "Getting Started" sections of the README have been rewritten to reflect the new, simplified `git clone` and `docker compose up` workflow.

### Fixed

- **CRITICAL: Concurrency Setting Not Applying:** Fixed a critical bug where changes to the `Total Processing Cores` setting were not being applied until a full container restart. The Task Runner's worker pool is now correctly reconfigured immediately after settings are saved.
- **UI Bug in Job Settings:** Fixed a UI bug where the "Auto-detect" button for processing cores only updated the read-only display field and not the hidden input field, which prevented the detected value from being saved correctly.
- **JavaScript Syntax Error:** Corrected a JavaScript syntax error (`catch error` instead of `catch (error)`) in `settings.html` that prevented the CPU auto-detection logic from running.

### Removed

- **Obsolete `conversion_logic.py`:** Deleted the old, unused `conversion_logic.py` file, which was left over from the v0.13.0 refactor, bringing the codebase in line with the documentation.

## [0.13.0] - 2025-10-09

This is a landmark architectural release that completely overhauls the backend processing engine for significantly improved performance, efficiency, and intelligent resource management, especially for multi-book download jobs.

### Changed

- **MAJOR: Backend Concurrency Overhaul:** The entire download and conversion pipeline has been refactored to use a new, centralized "Task Runner" architecture.
    - Replaced the inefficient "nested thread pool with global semaphore" model with a single, global `ThreadPoolExecutor` managed by a `PriorityQueue`.
    - This implements a "waterfall" CPU allocation strategy, ensuring all available CPU cores are intelligently assigned to the highest-priority tasks (1. Encode, 2. Prepare, 3. Merge). This dramatically speeds up the completion time of individual books and maximizes system throughput.
- **MAJOR: Chunked Conversion is Now Standard:** The new architecture is built exclusively around the more efficient parallel, chapter-based chunked conversion method.
- **Job Settings UI Rework:** The "Job Settings" section has been simplified and clarified to align with the new backend architecture.
    - "Normal Mode" now presents a simple, read-only "Total Processing Cores" setting that is configured via an "Auto-detect" button.
    - "Advanced Mode" reveals manual controls for `Total Processing Cores` (the global CPU worker limit) and `Max Parallel Downloads` (which now correctly throttles only the simultaneous download/prepare phase).

### Added

- **"Head-Start" Download Strategy:** The system now intelligently prioritizes the download and preparation of the first book in a multi-book queue. Subsequent book downloads begin in parallel only after the first book's download is complete, ensuring CPU cores start encoding the first book as quickly as possible.

### Fixed

- **CRITICAL: Job Status Reporting:** Fixed a critical logic bug where successfully completed jobs were incorrectly marked as `FAILED` in the database because their intermediate status was not being updated correctly.
- **UI Race Condition:** Fixed a frontend race condition where the final status of a book in the "Job Status" panel could incorrectly display as "Cancelled" even on success. The backend now sends an authoritative final status for all items when a job completes.
- **Concurrency Setting Bug:** Fixed a bug where the `Total Processing Cores` setting was being ignored due to an incorrect path, causing the worker pool to be permanently stuck at a default of 4 threads.
- **Merge Failure Bug:** Fixed a critical bug where the final `ffmpeg` merge step would fail due to a missing `chapters.txt` metadata file that was omitted during the refactoring.
- **Circular Import Crash:** Resolved a critical `ImportError` on application startup caused by a circular dependency between the `job_manager` and `chunked_conversion_logic` modules.

### Removed

- **Monolithic Conversion Path:** The old, single-threaded `conversion_logic.py` file and all related logic have been removed.
- **"Enable Chunked Conversion" Setting:** The UI toggle for enabling chunked conversion has been removed from the settings page, as this is now the standard, non-optional behavior.

## [0.12.0] - 2025-10-10

This is a major user interface and user experience release that introduces application branding, a full dark mode, and a comprehensive redesign of the main dashboard for improved usability and information hierarchy.

### Added

- **Application Branding:** The application is now officially branded as "AudioBookup".
    - Added custom favicons for all pages.
    - Added the official logo to the main dashboard header.
    - Updated all page titles to reflect the new brand name.
- **Dark Mode:** A full, persistent dark mode has been implemented.
    - A theme toggle button (moon/sun icon) is now present in the header of every page.
    - The user's theme choice is saved in `localStorage` and persists between sessions.
- **Library Status Filter:** Added a new "Filter by Status" dropdown to the main library controls, allowing users to filter the view by "New", "Missing", "Error", or "Downloaded".
- **Animated Deep Linking:** Clicking the "Automation is Disabled" banner now smoothly scrolls the settings page to the "Scheduled Tasks" accordion, animates it opening, and flashes a highlight on the relevant settings for better user guidance.

### Changed

- **MAJOR: Dashboard UI Redesign:** The main dashboard layout has been completely overhauled.
    - It now uses a responsive two-column grid layout on wider screens, with a primary "actions" column on the left and a secondary "status" column on the right.
    - The "Job Status" panel has been moved to the main actions column, making it immediately visible when a job is started.
    - The "Library Status" cards are now arranged in a 2x2 grid for a more compact and consistent look.
- **UI Consistency:** Performed a comprehensive review and update of all UI elements for better consistency.
    - The "Retry" button on book cards, the "Save Changes" button on the settings page, and all modal dialog buttons ("OK", "Confirm", "Cancel") now use the application's standard action button styling.
    - Form inputs on the login and setup pages now correctly fill the width of their container for a more professional look.

### Fixed

- **CRITICAL: Dark Mode CSS:** Fixed a critical circular dependency in the initial CSS variable definitions that caused numerous visual bugs (e.g., white-on-white elements) in both themes.
- **Responsive Layout:**
    - Fixed an issue on mobile where the header action icons could overlap the main page title. The header now correctly wraps into a vertical stack on narrow screens.
    - Corrected the main layout grid to prevent the "Library Status" cards from being pushed off-screen at certain browser widths.
    - Fixed inconsistent widths of the "Library Status" cards.
- **UI/UX:**
    - Corrected the positioning of the theme toggle button on the centered login and setup pages.
    - Fixed numerous minor style inconsistencies in dark mode, including button backgrounds, progress bar visibility, modal backgrounds, and text colors in dropdowns and accordions.

## [0.11.0] - 2025-09-27

This is a critical security and user experience release. It adds a complete, session-based user authentication system to the entire web interface and replaces the command-line setup process with a full graphical user interface.

### Added

- **User Authentication System:** The entire application is now protected by a persistent, session-based login system.
    - **Mandatory First-Run Setup:** On a fresh installation, users are required to log in with default credentials (`admin`/`changeme`) and are then forced to set a new, secure password.
    - **Credential Management:** Users can now change their administrator username and password from the main settings page. Changing credentials securely logs the user out.
    - **Automatic Secret Key Generation:** The application automatically generates and persists a unique `secret.key` in the `/config` volume, ensuring session security.
- **GUI for Audible Setup:** A new multi-step, graphical user interface wizard for connecting the application to an Audible account. This provides a user-friendly experience with advanced options for configuration.

### Changed

- **MAJOR: Setup Process Overhaul:** The first-time Audible connection process has been completely refactored from an in-browser terminal to the new, intuitive GUI wizard.
- **MAJOR: Backend Automation Engine:** The automation engine for the setup process was migrated from the low-level `ptyprocess` library to the more robust, industry-standard `pexpect` library to resolve critical deadlocks and improve reliability.
- **Code & UI Clarity Refactor:** Performed a comprehensive renaming of functions, variables, and API endpoints to eliminate ambiguity between local user authentication and the "Audible Connection" (e.g., `get_auth_status` is now `get_audible_auth_status`).

### Fixed

- **CRITICAL: Settings Page Save:** Fixed a critical bug where the "Save Changes" button on the settings page was completely non-functional due to a JavaScript error.
- **CRITICAL: Setup Wizard Deadlocks:** Fixed a series of critical deadlocks in the new setup wizard that caused the process to hang indefinitely.
- **Book Detail Modal:** Fixed a bug where the "Get Full Summary" button was not appearing for older books in the library due to a `NULL` vs. `0` data handling error on the backend.
- **Dashboard UI:** Fixed a JavaScript bug in the `checkAuthStatus` function that was calling a renamed API endpoint, preventing the "Authentication Issue" banner from ever appearing.

## [0.10.0] - 2025-09-17

This is a comprehensive release focused on overhauling the task scheduler with advanced, independent sync modes, improving code quality and UI consistency, and adding several significant user-facing features and bug fixes.

### Added

- **Advanced Scheduling System:** The entire scheduling engine has been replaced with the robust, industry-standard `APScheduler`.
    - **Cron-based Scheduling:** Tasks can now be configured with full cron strings for maximum flexibility.
    - **Advanced Mode UI:** A new "Advanced Mode" toggle on the settings page reveals advanced options like cron inputs and "Run Now" buttons, keeping the interface simple for regular users.
    - **Timezone Configuration:** A new setting allows users to select their local timezone, ensuring schedules run at the expected local time.
- **Separated Sync Modes:** The library sync feature has been split into two distinct types for performance and efficiency.
    - **Fast Sync (API-only):** A lightweight sync that only checks for new books and metadata from Audible.
    - **Deep Sync (Full Scan):** A comprehensive sync that performs the API check and then does a full scan of local files.
    - **Independent Scheduling:** Users can now configure separate automated schedules for Fast and Deep syncs on the settings page.
- **CPU-based Concurrency Detection:** Added an "Auto-detect" button to the Job Settings that determines the number of available CPU cores and suggests a safe level of concurrency (`cores - 1`). Includes smart warnings for single-core systems and when the recommendation is capped by the safety limit.
- **Job History Pagination:** The Job History page is now fully paginated, displaying 50 jobs per page to efficiently handle a large history.
- **Job History Filtering & Search:** Added controls to the Job History page to filter jobs by type (Sync, Download) and status (Completed, Failed, Cancelled). Added a search bar to find jobs containing specific books by title or author, with all filtering and searching handled efficiently on the backend.
- **Enhanced Job History Items:** Job history entries for download tasks now display a thumbnail of the book's cover art and gracefully handle items for books that have since been deleted from the main library.
- **Phased Progress for Sync Jobs:** The "Sync Library" job in the UI now displays which major phase is active (e.g., "Phase 1/3: Fetching from Audible"), providing clearer feedback.
- **License Attribution:** Added attribution for the Immich project to the license file and source code for the adapted CPU detection logic.

### Changed

- **MAJOR: Code Quality & Standardization:** Performed a comprehensive code review. The entire Python codebase has been cleaned and standardized using `ruff`, resolving all linter errors, including unused variables and overly complex functions.
- **MAJOR: CSS Refactoring & Unification:** Performed a complete refactoring of the main `style.css` file. Consolidated all shared component styles (modals, buttons, forms, accordions), removed over 100 lines of redundant code, and reorganized the entire file into a logical, maintainability-focused structure.
- **Manual "Sync Library" Action:** The main dashboard's "Sync Library" button has been simplified to a single action that always performs a comprehensive "Deep Sync" for a more intuitive user experience.
- **Download Selection Modal:** The "Process Downloads" modal has been significantly enhanced:
    - It now displays books in three distinct, clearly labeled categories: `NEW`, `MISSING`, and `ERROR`.
    - The list is now a rich, visual interface that includes a small thumbnail of the cover art for each book.
- **UI/UX:** Completely redesigned the layout of the Book Detail modal to make better use of space. Key metadata is now displayed in a column next to the large cover art, while the summary and file information are in a unified, scrollable section below.
- **UI/UX:** Relocated the "Enable Advanced Mode" toggle on the settings page from a prominent body section to a more subtle position in the page header for a cleaner layout.

### Fixed

- **CRITICAL: Download Job Start Failure:** Fixed a critical `UnboundLocalError` that prevented all manual download jobs from starting. The crash was caused by a missing variable assignment in the download job creation logic path.
- **CRITICAL: Scheduler Implementation:** Fixed a series of critical bugs in the `APScheduler` implementation. Replaced the incorrect `BlockingScheduler` with `BackgroundScheduler`, resolved thread context conflicts with the Flask server, and implemented a robust, event-driven mechanism to reliably detect and apply schedule changes from the settings page, completely fixing jobs not running at their scheduled times.
- **CRITICAL: API Job Start Logic:** Fixed a critical `TypeError` that was silently preventing manual sync jobs from starting from the main dashboard.
- **CRITICAL: Manual Authentication Reset:** Fixed a critical bug where the "Re-authenticate" feature was attempting to delete credentials from the wrong directory (`/config` instead of the correct `/database` volume), causing the reset to fail.
- **Dashboard Search & Sort:** Fixed a JavaScript error on the main dashboard that was preventing the library search bar and sort dropdown from functioning. The error was caused by a leftover event listener from another page.
- **Book Detail Modal Layout:** Fixed multiple layout and scrolling bugs in the redesigned Book Detail modal. The entire modal content is now correctly contained and scrolls as a single unit, fixing overflow issues on various screen sizes.
- **Download Retry Logic:** Fixed a logic bug where a book that failed an automatic download would have its `retry_count` permanently incremented, preventing it from ever being included in future automatic jobs. Manual retries now correctly reset this counter.
- **UI Style Unification:** Corrected multiple CSS inconsistencies. Unified the accordion style on the Settings page to match the History page, restored missing unique icons to the Settings accordion headers, and fixed the "boxy" appearance of modal headers to be seamless.
- **Download Selection Modal UI:** Fixed inconsistent styling in the "Select Books to Process" modal. Buttons now use the unified application style, book cover thumbnails are correctly sized, and items are vertically centered for a cleaner look.
- **Numerous Settings Page UI Bugs:**
    - Fixed multiple JavaScript crashes that prevented accordions and buttons from functioning.
    - Corrected the CSS for custom radio buttons to ensure the "selected" state is clearly visible.
    - Fixed a bug where the "Cron" scheduling option was incorrectly visible when "Advanced Mode" was disabled.
    - Added a missing "Schedule Type" label and themed the time input to match the application's style.
    - Corrected a Jinja2 template syntax error in the timezone selector that caused an "Internal Server Error".

This is a comprehensive release focused on overhauling the task scheduler with advanced, independent sync modes, improving code quality, and adding several significant user-facing features and bug fixes.

### Added

- **Advanced Scheduling System:** The entire scheduling engine has been replaced with the robust, industry-standard `APScheduler`.
    - **Cron-based Scheduling:** Tasks can now be configured with full cron strings for maximum flexibility.
    - **Advanced Mode UI:** A new "Advanced Mode" toggle on the settings page reveals advanced options like cron inputs and "Run Now" buttons, keeping the interface simple for regular users.
    - **Timezone Configuration:** A new setting allows users to select their local timezone, ensuring schedules run at the expected local time.
- **Separated Sync Modes:** The library sync feature has been split into two distinct types for performance and efficiency.
    - **Fast Sync (API-only):** A lightweight sync that only checks for new books and metadata from Audible.
    - **Deep Sync (Full Scan):** A comprehensive sync that performs the API check and then does a full scan of local files.
    - **Independent Scheduling:** Users can now configure separate automated schedules for Fast and Deep syncs on the settings page.
- **CPU-based Concurrency Detection:** Added an "Auto-detect" button to the Job Settings that determines the number of available CPU cores and suggests a safe level of concurrency (`cores - 1`). Includes smart warnings for single-core systems and when the recommendation is capped by the safety limit.
- **Job History Pagination:** The Job History page is now fully paginated, displaying 50 jobs per page to efficiently handle a large history.
- **Enhanced Job History Items:** Job history entries for download tasks now display a thumbnail of the book's cover art and gracefully handle items for books that have since been deleted from the main library.
- **Phased Progress for Sync Jobs:** The "Sync Library" job in the UI now displays which major phase is active (e.g., "Phase 1/3: Fetching from Audible"), providing clearer feedback.
- **License Attribution:** Added attribution for the Immich project to the license file and source code for the adapted CPU detection logic.

### Changed

- **MAJOR: Code Quality & Standardization:** Performed a comprehensive code review. The entire Python codebase has been cleaned and standardized using `ruff`, resolving all linter errors, including unused variables and overly complex functions.
- **Manual "Sync Library" Action:** The main dashboard's "Sync Library" button has been simplified to a single action that always performs a comprehensive "Deep Sync" for a more intuitive user experience.
- **Download Selection Modal:** The "Process Downloads" modal has been significantly enhanced:
    - It now displays books in three distinct, clearly labeled categories: `NEW`, `MISSING`, and `ERROR`.
    - The list is now a rich, visual interface that includes a small thumbnail of the cover art for each book.

### Fixed

- **CRITICAL: Scheduler Implementation:** Fixed a series of critical bugs in the `APScheduler` implementation. Replaced the incorrect `BlockingScheduler` with `BackgroundScheduler`, resolved thread context conflicts with the Flask server, and implemented a robust, event-driven mechanism to reliably detect and apply schedule changes from the settings page, completely fixing jobs not running at their scheduled times.
- **CRITICAL: API Job Start Logic:** Fixed a critical `TypeError` that was silently preventing manual sync jobs from starting from the main dashboard.
- **CRITICAL: Manual Authentication Reset:** Fixed a critical bug where the "Re-authenticate" feature was attempting to delete credentials from the wrong directory (`/config` instead of the correct `/database` volume), causing the reset to fail.
- **Job History UI:** Fixed a CSS and JavaScript conflict that prevented the accordion view on the Job History page from expanding when clicked.
- **Download Retry Logic:** Fixed a logic bug where a book that failed an automatic download would have its `retry_count` permanently incremented, preventing it from ever being included in future automatic jobs. Manual retries now correctly reset this counter.
- **Numerous Settings Page UI Bugs:**
    - Fixed multiple JavaScript crashes that prevented accordions and buttons from functioning.
    - Corrected the CSS for custom radio buttons to ensure the "selected" state is clearly visible.
    - Fixed a bug where the "Cron" scheduling option was incorrectly visible when "Advanced Mode" was disabled.
    - Added a missing "Schedule Type" label and themed the time input to match the application's style.
    - Corrected a Jinja2 template syntax error in the timezone selector that caused an "Internal Server Error".

## [0.9.0] - 2025-09-12

This is a major architectural release that fundamentally restructures the project for stability, safety, and maintainability. It introduces a clear separation between stateless application code and stateful user data, streamlines the development workflow, and adds significant new automation and user experience features.

### Added

- **Advanced Automation & Scheduling:**
    - The scheduler can now run automatic download jobs on an independent, configurable timer, separate from the library sync schedule.
    - Added granular settings to allow users to control which book statuses (`NEW`, `MISSING`, `ERROR`) are included in automatic download jobs.
    - Added a "smart chaining" setting (enabled by default) to automatically trigger a download job immediately after a sync discovers any new or missing books.
- **Safe Automatic Error Retries:** The system now tracks failed automatic downloads and will only attempt to re-process a book with an `ERROR` status once, preventing potential failure loops.
- **Conversion ETA:** The UI progress bar now displays an estimated time remaining for the active conversion. The estimator learns from the performance of past jobs to improve its accuracy over time.
- **Dedicated Settings Page:** The entire settings UI has been migrated from a modal on the main page to its own dedicated, full-featured `/settings` page for improved organization and usability.
- **Dedicated Job History Page:** The job history view has been migrated from a modal to its own dedicated `/history` page, providing a cleaner interface and a permanent URL for accessing past job information.
- **Dynamic Automation Banner:** The "Automation is Disabled" banner on the main dashboard now provides specific, dynamic feedback, clearly stating which automated tasks are currently disabled.

### Changed

- **MAJOR: Project Structure & Data Separation:** The entire project has been refactored to separate the stateless application code from stateful user data for greatly improved safety and maintainability.
    - Application code now lives exclusively inside the Docker image.
    - A new `/database` volume has been introduced to store critical, irreplaceable data (`library.db`, `.audible` auth files, critical caches).
    - The `/config` volume is now used for non-critical data that can be regenerated (`settings.json`, logs, covers).
    - The host project directory has been reorganized into a clean `src`/`bin` structure.
- **MAJOR: Development Workflow:** The development process is now significantly streamlined. Developers no longer need to manually delete files like `.initialized` before rebuilding the container.
- **UI Navigation:** The "Settings" and "Job History" buttons on the main dashboard have been moved to the top-right of the page as icons and now navigate to their respective new pages instead of opening modals.
- **Scheduler Logic:** The scheduler now uses a priority system (`if`/`elif`) to prevent race conditions where sync and process jobs could be triggered simultaneously.

### Fixed

- Fixed numerous complex CSS and JavaScript bugs on the settings page related to accordion panels, nested toggle animations, and unresponsive buttons.
- Corrected a critical bug where several background processes (like `sync_logic.py`) were not using the correct `HOME=/database` environment variable after the project refactor, causing them to fail.
- Fixed a bug where the application startup process would run the authentication health check twice unnecessarily.

## [0.8.0] - 2025-09-09

This is a landmark release focused on eliminating the application's reliance on shell scripts for its core logic. The entire data processing pipeline, from library synchronization to audiobook conversion, has been ported to native Python. This significantly improves performance, error handling, cross-platform compatibility, and overall maintainability. This version also introduces parallel processing for downloads and granular, real-time progress feedback for all background tasks.

### Added

- **Granular Progress Reporting:** All background jobs (Sync and Download) now provide detailed, real-time progress updates to the UI, showing the current stage (e.g., "Downloading... 75%", "Scanning files... 50/120") and a smoothly updating progress bar.
- **Parallel Downloads:** The application now processes multiple book downloads simultaneously based on the user-configurable "Parallel Download Jobs" setting, dramatically reducing the time required to process a large batch.
- **Manual Authentication Check:** Added a new "Authentication" section to the settings UI with a button to manually trigger an immediate check of the Audible login status.
- **Scheduled Task Configuration:** Added a "Scheduled Tasks" section to the settings UI, allowing the user to configure the interval (in hours) for the automated background authentication health check.

### Changed

- **Major Refactor (Shell to Python):** The core logic from `sync.sh`, `process_book.sh`, and the third-party `audible-convert.sh` has been completely ported to native Python modules (`sync_logic.py`, `processing_logic.py`, `conversion_logic.py`). This removes the `jq` dependency and centralizes all processing logic within the Python backend.
- **Code Organization:** The new Python-based processing logic has been separated into distinct modules for improved separation of concerns and maintainability.

### Fixed

- **Job History for Sync:** The "Job History" modal now correctly displays entries for `SYNC` jobs, providing a complete history of all background tasks.
- **Concurrency Implementation:** Fixed a major bug where the download worker was processing books serially, ignoring the user-defined concurrency setting.
- **Log Verbosity:** The log level for real-time progress updates from `audible-cli` and `ffmpeg` was changed from `INFO` to `DEBUG`, significantly cleaning up the main application log during normal operation.
- **Multiple `ffmpeg` Conversion Bugs:** Resolved several subtle bugs in the Python-based `ffmpeg` command construction that were causing conversions to fail, including issues with argument formatting (`-movflags`) and interactive prompts (`-y`).

### Removed

- **Obsolete Shell Scripts:** The now-redundant `sync.sh`, `process_book.sh`, `audible-convert.sh`, and `download.sh` scripts have been removed from the project.

## [0.7.0] - 2025-09-08

This is a comprehensive stability and maintainability release. The primary focus was a major refactoring of the Python backend from a single file into a modular package, the introduction of a robust logging system, and the conversion of all long-running tasks to the stateful job system. This version also includes numerous bug fixes related to UI state management and a complete re-theming of the setup page for a more cohesive user experience.

### Added

- **Centralized Logging System:** Replaced all `print()` statements with a robust, centralized logging system (using Python's `logging` module) that outputs timestamped logs to both the console and the persistent log file.
- **UI-Driven Re-authentication:** Added a new workflow allowing users to reset their Audible authentication directly from the UI. This triggers a confirmation modal, securely deletes credentials on the backend, and automatically restarts the application, replacing the previous manual instructions.

### Changed

- **Major Backend Refactor:** The monolithic `app.py` has been broken down into a clean, modular package structure (`db.py`, `job_manager.py`, `settings.py`, etc.) to significantly improve maintainability and separation of concerns.
- **Stateful Library Sync:** The "Sync Library" action has been completely refactored into a stateful, background job, consistent with the download system. This prevents race conditions and allows users to safely close the browser during a sync.
- **Background Authentication Check:** The Audible authentication health check was moved from a blocking, on-page-load API call to a non-blocking, periodic background task that runs on a configurable interval, improving UI responsiveness and reducing unnecessary API calls.
- **Granular Job Progress:** The download worker now streams output from the processing script in real-time, restoring granular progress updates (e.g., "Downloading...", "Converting...") to the UI for a better user experience.
- **UI/UX:** The `setup.html` page has been re-themed to match the modern look and feel of the main application.
- **UI/UX:** The "Clear Report" button has been renamed to "Clear Finished" and its logic updated to only remove completed items, preventing the accidental clearing of active jobs from the panel.

### Fixed

- **UI State Management:** Resolved multiple UI state bugs where action buttons ("Sync Library", "Retry") were not correctly disabled during an active job or re-enabled after its completion, especially after a page reload.
- **Job Reconnection:** Fixed an issue where reconnecting to a running "Sync" job would result in an empty Job Status panel.
- **Environment Variable Path:** Corrected a critical bug where the new background health check was failing because it was missing the `PATH` environment variable, resulting in an "'audible-cli' not found" error.
- **Error Handling:** Improved error handling in the book processing script to ensure a book's status is correctly set to `ERROR` if it can't be found in the database at the start of a job.
- **Retry Button:** The individual "Retry" button on book cards now correctly uses the stateful job system, ensuring UI consistency and stability.

## [0.6.0] - 2025-09-05

This is a major architectural overhaul that transforms the application from a simple script-runner into a robust, stateful background task manager. This version introduces persistent, reconnectable download jobs, job management capabilities, and significantly enhanced configuration options.

### Added

- **Stateful Background Jobs:** Download jobs are now managed by a persistent, background worker thread, allowing you to safely close the browser without interrupting downloads.
- **Reconnectable UI:** The frontend now automatically detects and reconnects to running jobs on page load, providing a seamless user experience.
- **Job Cancellation:** A "Cancel Job" button now appears for running jobs, allowing for the graceful termination of a download queue between books.
- **Job History:** A new "Job History" modal displays a list of all past jobs (Completed, Failed, Cancelled) and the books included in each batch.
- **Custom File Naming:** Added a setting to define a custom folder and file naming template (e.g., `{author}/{title}`), giving users full control over their library organization.
- **Audio Quality Settings:** Added a setting to select the desired audio quality (High, Standard, Low) for converted files.
- **Authentication Health Check:** The application now proactively checks if the Audible login is still valid on page load and displays a prominent warning banner if authentication has expired.

### Changed

- **Major Refactor:** The "Process Downloads" and individual "Retry" buttons were completely refactored to use the new stateful job system and real-time SSE stream.
- **UI/UX:** The "Job Status" panel is now a persistent report after a job finishes. It displays final statuses with icons (success, fail, cancelled) and includes a "Clear Report" button to be dismissed manually.
- **UI/UX:** Polished the settings modal by improving the layout and styling of form elements.

### Fixed

- Fixed a critical Python bug (`AttributeError: 'Thread' has no attribute 'Event'`) that prevented download jobs from starting.
- Corrected the `audible-cli` command used for the auth health check after discovering the initial commands were invalid.
- Resolved several JavaScript bugs related to variable redeclaration that prevented UI components (like the "Clear Report" button and settings modal) from functioning correctly.

## [0.5.0] - 2025-09-03

This version introduces powerful library navigation tools, enriches the data presented to the user, and provides a major overhaul of the mobile user experience.

### Added

- **Library Search and Filtering:** A search bar and sort dropdown have been added to the main library view, allowing for real-time, client-side filtering by title, author, or narrator, and sorting by author, title, release date, and date added.
- **On-Demand Full Summaries:** To keep library syncs fast, the application now fetches a truncated summary by default. A "Get Full Summary" button now appears in the detail modal, allowing users to fetch the full, untruncated book summary on demand.
- **Enhanced ERROR Details:** Books that fail to process now store a truncated error log in the database, which is visible in the book's detail modal for easier debugging.
- **Additional Metadata:** The application now fetches and displays the book's Publisher, Language, and the date it was added to the Audible library.

### Changed

- **Major Mobile UI Rework:** The CSS media query for detecting mobile devices has been overhauled to be more reliable on modern, high-resolution screens. The layout now correctly adapts on a wider range of phones and tablets.
- **Improved Detail Modal:** The book detail modal is now fully responsive, using dynamic viewport units (`dvh`) to prevent being obscured by mobile browser navigation bars. The layout has been optimized for readability on narrow screens, with a larger cover image.

### Fixed

- Fixed a critical bug in `process_book.sh` caused by a `sed` typo that prevented all book downloads.
- Restored missing Socket.IO and pseudo-terminal (PTY) logic in `app.py` required for the first-time setup process.

## [0.4.0] - 2025-09-02

This release is a major overhaul of the user experience, introducing several new interactive UI components, a fully responsive mobile-friendly layout, and numerous quality-of-life improvements. The focus was on making the application more intuitive, interactive, and visually polished.

### Added

- **Live "Currently Processing" Panel:** A new, collapsible panel appears below the status cards during downloads, showing real-time, step-based progress (Downloading, Converting) for each book in the queue.
- **Selective Download Modal:** The "Process Downloads" action now opens a modal window, allowing the user to select specific books to process from a list, complete with "Select All" and "Select None" functionality.
- **Individual Book Detail Modal:** Clicking on a book card in the library now opens a detailed modal view showing the high-resolution cover art, all book metadata, and complete file information (path, size, modification date).
- **Responsive Mobile Layout:** The entire UI is now fully responsive, adapting gracefully to smaller screens for a seamless experience on phones and tablets.
- **Custom Alert Modal:** Replaced the default browser `alert()` with a custom, themed modal for a more integrated and professional user experience.
- **Book Card Hover Effect:** Book cards in the library now have a subtle hover effect, improving visual feedback and interactivity.

### Changed

- **Backend Event Streaming:** The backend and worker scripts were significantly refactored to support granular, real-time event streaming for individual books, powering the new live processing panel.
- **UI/UX**: The "Currently Processing" panel was updated to be a permanent, collapsible element instead of a temporary one, making it always accessible.
- **Code Quality**: Resolved CSS and Python linter warnings for better code health and maintainability.

### Fixed

- **UI Layout**: Corrected the layout of the "Library Status" grid on all screen sizes to prevent an uneven three-column view, ensuring a consistent 4-column (desktop) or 2-column (mobile) layout.
- **UI Layout**: Made the "Currently Processing" panel responsive, preventing the progress bar from cutting off book titles on narrow screens by stacking the content vertically.
- **UI**: The "Retry" button on a book card is now removed instantly upon successful download rather than waiting for the entire batch to finish.
- **UI**: Spacing in the settings modal was adjusted to prevent the close button from being too close to the import/export buttons.
- **UI Bug**: Fixed a bug where the selection count in the download modal would not reset to zero when no books were available to download.
- **UI**: Corrected the vertical alignment of status badges and action buttons on book cards in the main library grid.

## [0.3.0] - 2025-09-02

This release focuses on major backend performance optimizations and significant feature enhancements, including parallel processing and persistent, server-side settings management.

### Added

- **Parallel Processing:** The application can now download and process multiple books simultaneously, dramatically reducing the time required to process a large batch.
- **Persistent Server-Side Settings:** Application settings are now stored in a `settings.json` file in the `/config` directory, making them persistent across different browsers and sessions.
- **Settings Modal:** A new settings modal, accessible via a gear icon, provides a centralized and extensible location for application settings.
- **Accordion UI for Settings:** The settings modal uses a professional, expandable accordion layout to organize settings into categories for future expansion.
- **Settings Import/Export:** Added the ability to export all application settings to a JSON file and import them back, allowing for easy backup and configuration sharing.

### Changed

- **Log Readability:** Log output for download jobs is now prepended with the book's ASIN, making the activity log much clearer when running parallel jobs.
- **Code Quality:** Added detailed comments to the `process_book.sh` worker script for better maintainability.
- **UI:** The concurrency setting was moved from the main action bar into the new settings modal.
- **UI:** The "Save Settings" button now provides non-disruptive feedback directly on the button and no longer automatically closes the modal, improving user experience.

### Fixed

- **UI Bug:** Corrected a recurring bug where the library grid would not populate on page load due to missing template code in `index.html`.

### Optimization

- **Filesystem Scan Caching:** Implemented a cache for local file scans (`/config/.file_scan_cache`), dramatically speeding up the "Sync Library" process by avoiding unnecessary metadata reads on unchanged files.
- **Reconciliation Logic:** The database reconciliation process in `sync.sh` was optimized into a faster two-pass system (verification and discovery).

## [0.2.0] - 2025-09-01

This version marks a major overhaul of the user interface, moving from a basic table and log view to a modern, visual, and more responsive dashboard experience. It also includes critical bug fixes and performance optimizations.

### Added

- **Visual Library Grid:** The library view is now a responsive grid of book cards instead of a data table.
- **Cover Art Caching:** The application now downloads and caches cover art locally to `/config/covers`, improving performance and reliability.
- **Collapsible Activity Log:** The activity log is now a sticky footer that can be expanded or collapsed to save screen space.
- **Live Status Bar:** The collapsed log footer acts as a live status bar, showing the most recent log entry in real-time.
- **Loading Animations:** Action buttons now display a loading spinner to provide clear visual feedback when a script is running.

### Changed

- **UI/UX:** Redesigned the "Library Status" section with vibrant, icon-based cards.
- **UI/UX:** Renamed "Sync All" button to **"Sync Library"** for better clarity.
- **UI/UX:** Renamed "Download New Books" button to **"Process Downloads"** to more accurately reflect its function.

### Fixed

- **Character Encoding:** Fixed a critical bug that caused the entire library to be duplicated due to improper handling of special characters (UTF-8). The container environment is now correctly configured with a proper locale.
- **Thumbnail Generation:** Fixed a subtle bug where the `ffmpeg` command would skip processing every other book cover due to an issue with shell input redirection.
- **UI:** The log toggle button is no longer disabled while scripts are running.
- **UI:** The activity log now automatically scrolls to the latest entry when it is expanded.
- **UI:** Corrected a missing icon on the "Missing" status card.
- **Backend:** Fixed a `TemplateNotFound` error by making the Flask template folder path explicit.

### Optimization

- **Lazy Loading:** Implemented lazy loading for all cover art to improve initial page load speed.
- **Thumbnail Generation:** The sync process now automatically creates 200x200px thumbnails from the original cover art, significantly reducing file sizes and improving UI performance.

## [0.1.0] - Initial Release

- Initial stable, fully containerized version.
- Handles interactive setup, persistence, library syncing, and downloading via a web UI.

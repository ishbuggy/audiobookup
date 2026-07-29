# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

## [0.22.0] - 2026-07-29

This release brings AudioBookup's download and conversion options closer to parity with Libation, adding MP3 output, a download-quality request, optional sidecar files, chapter and metadata cleanups, an Audible-branding trim, and expanded file/folder naming — and reorganizes the Settings page into clearer sections. Requesting Widevine DRM, the xHE-AAC codec, or Spatial Audio remains unsupported: these require a licensing path that the underlying download toolchain cannot use, a permanent limitation rather than a deferred feature.

### Added
- **MP3 Output Format:** Alongside the existing DRM-free `.m4b` (AAC) and "Original" lossless options, you can now convert audiobooks straight to MP3, with adjustable quality (a variable-bitrate slider, or a target bitrate — matched to the source by default, rounded up to the nearest standard MP3 rate, and falling back to the bitrate you set when matching is off or the source can't be read — with an optional constant-bitrate mode), plus options to downsample to mono or cap the sample rate. Chapters and cover art are embedded just like the other formats. MP3 encoding is single-threaded and noticeably slower per book than the default path — a deliberate trade-off for gapless, drift-free audio instead of the tiny gaps that faster chunked encoding can introduce. The library scan and file-renaming tools now recognize `.mp3` files, and any companion files travel with a renamed book.
- **Download Quality Request:** A new setting lets you ask Audible for a lower-bitrate source download ("best", "high", or "normal") independent of how the file is later converted — useful for saving bandwidth and disk space when top audio quality doesn't matter as much. Defaults to "best" (today's behavior).
- **Sidecar Files:** Four optional companion files, each off by default: a copy of the cover image saved next to the audiobook, a `.metadata.json` file with the book's key details (author, narrator, series, genres, description, and more), a `.cue` sheet listing chapter timings, and an option to keep the original encrypted Audible download (and its license) alongside the converted file.
- **Expanded Naming Placeholders:** File and folder naming templates gain `{series}`, `{series_part}`, `{year}`, and `{language}` tags, alongside the existing ones. A book missing one of these values (for example, a standalone book with no series) cleanly drops that part of the path instead of leaving an empty or "N/A" folder. The new placeholders apply to books you download from now on (and to any rename triggered by the "apply custom metadata to filenames" option) — books already on disk keep their current file and folder names. The series sequence number used by `{series_part}` is captured during library syncs, so it becomes available once the first sync after updating has run.
- **Chapter & Metadata Cleanups:** New optional cleanups for chapter and tag data, all off by default: combine a multi-part audiobook's nested chapter titles into one flat, readable list; merge "Opening Credits"/"End Credits" tracks into the neighboring chapter instead of leaving them as their own short entries; strip "(Unabridged)" from the title and album tags (never touching a title you've customized yourself); and a chapter-title template for further tweaking.
- **Audible Branding Trim:** An optional setting trims the few seconds of Audible's spoken branding from the start and end of a book and shifts chapter markers to match, so playback starts right on the story. Off by default, and includes a safety check that skips the trim (rather than risk a broken file) if a title reports an implausible brand length. It only applies to converted AAC or MP3 output — an "Original" lossless download is never trimmed.
- **Separate Folder & File Naming Templates:** In addition to the single naming template, Advanced Mode now lets you describe the folder structure and the file name as two separate templates, using the same placeholders. When both are filled in they're combined as "folder/file" and take the place of the single template; leaving either one blank keeps the single template in charge. The composed pair applies to books you download from now on (and to any rename triggered by the "apply custom metadata to filenames" option) — books already on disk keep their current file and folder names.
- **File Timestamp Source:** A new option can set each finished audiobook's file date — and that of its companion files — to the book's release date or the date you bought it, instead of the time it was downloaded, which makes it easy to sort a library by release or purchase date in a file browser or media server. Dates are set to the day, so several books bought on the same day all share that date. Applies to books you download or convert from now on — files already on disk are not re-dated. Advanced Mode, and off by default.
- **Settings Page Reorganization:** The old "Conversion Settings" section has been split into clearer, more focused sections — Downloading, Audio & Output Format, Chapters & Metadata, Sidecar Files, and File & Folder Naming — to make room for everything above without overwhelming the page. Several options that previously only existed by hand-editing the settings file are now exposed in the UI for the first time: the lossless "Original" output mode, the companion-PDF download toggle, the subtitle-truncation naming option, and the setting that renames files to match custom metadata.

### Changed
- **Library Card Cleanup:** Already-downloaded books no longer show a "Re-download" button on their library cards (in any of the Grid, List, or Table views). Re-downloading a book you already have is a deliberate action — it converts the book again using your current settings, and if the output format or naming has changed since the original download, the new file lands at a new path and the previous one is left on disk — so it now lives only in the book's detail modal as "Force Re-download", with its confirmation prompt. The "Download" button on New, Missing, and Error books is unchanged.
- **CI:** Updated the automation that builds and publishes the Docker image to current versions, clearing deprecation warnings. Build-only change — the published image is unaffected.

## [0.21.0] - 2026-07-22

This is an **image-hardening** release with **no functional changes**. It rebuilds the container to clear as many vulnerabilities as possible from a security scan of the published image, while keeping the application's behavior identical. A combined Grype + Trivy scan of the image drops from **31 / 12 Critical** and **146 / 83 High** (Grype / Trivy) down to **26 / 9 Critical** and **76 / 78 High** — Grype's total falls 684 → 457 and Trivy's 592 → 490.

### Security
- **Rebuilt on Debian 13 (trixie).** The container's base image moved from Debian 12 (bookworm) to Debian 13, which ships a much newer and more thoroughly patched set of system libraries (ffmpeg, glibc, expat, libtiff, curl, and many more). This clears the large majority of the operating-system CVEs that simply had no fix available on the older base. Python stays on 3.11, so `audible-cli` and the setup wizard are unaffected, and conversions were verified end-to-end (multi-chapter and single-chapter downloads) on the new ffmpeg 7.
- **Refreshed the privilege-drop helper (gosu).** The image now installs the current upstream `gosu` release (its signature verified at build time) instead of the distribution package, which was compiled with an out-of-date Go toolchain and carried a cluster of Go standard-library CVEs. All of them are removed.
- **Updated Python dependencies and build tooling.** Flask, Werkzeug, and requests are bumped to their latest patched releases, and the image's `pip`/`setuptools`/`wheel` are upgraded during the build, clearing their known advisories.

### Removed
- **Trimmed the image.** Dropped the unused `jq` tool and a redundant explicit `coreutils` install to reduce attack surface. No behavior change (`coreutils` remains present as part of the Debian base).

### Note
- Some vulnerabilities remain because they are unfixed upstream — chiefly `ffmpeg`/`libav*`, which is essential to the app and cannot be removed. The remaining scanner-"fixable" findings are all the CPython 3.11 interpreter, which the scanners map to fixes on the 3.13+ line; staying on 3.11 is deliberate for `audible-cli` compatibility.

## [0.20.0] - 2026-07-22

### Added
- **Large-Download Heads-Up:** Kicking off a bulk download of 10 or more books now shows a brief confirmation first, explaining that AudioBookup converts every book to a DRM-free file (so a big batch takes real time) and offering a rough time estimate based on your recent conversion speeds. Smaller selections start immediately as before, and the estimate is intentionally conservative — downloads run in the background and usually finish sooner.
- **Duplicate Resolution UI:** Books that were flagged as colliding duplicates during a bulk download (saved with a unique `Title_<ASIN>.m4b` filename) now carry a **Duplicate** badge in every library view, and the status filter gained a **Flagged Duplicates** option to list just those books. Opening a flagged book shows a **Resolve Duplicate** button that offers a disambiguating naming convention — append the **narrator** or the **release year** to the title, or keep the ASIN-suffixed name — with a live preview. Applying writes the chosen title through the existing metadata-override system and clears the duplicate flag (removing the badge everywhere); when the `apply_custom_to_filenames` setting is on, the file is renamed on disk to match too.
- **Bulk Rename Tools:** The List and Table library views now have a checkbox on every row and a bulk action bar (select all shown / clear / **Bulk Rename**). With books selected, the Bulk Rename dialog offers two operations — **Strip subtitle from title** (drops a long "Main Title: Subtitle" tail, e.g. *999: The Extraordinary Young Women…* → *999*) and **Find and replace** on the Title or Author (e.g. remove "[Audible Go Edition]" from a batch of titles). A live preview shows exactly which books will change (and skips rows where the result would be empty or unchanged) before you apply; changes are written through the existing metadata-override system, so they show everywhere immediately and revert cleanly with **Reset to Audible**. If the `apply_custom_to_filenames` setting is on, the dialog warns that files will be renamed on disk too.
- **Edit Book Metadata & Cover Art In-App:** The book detail modal now has an **Edit Metadata** button that reveals editable Title and Author fields. Saving stores your override and applies it everywhere the book is shown (grid, list, table, and the modal) without a reload; clearing a field (or the **Reset to Audible** button) reverts to the original Audible value, which is shown as a hint while editing. Covers can be replaced too — a **Change Cover** control under the artwork uploads any image (JPEG/PNG/WebP/BMP, up to 15 MB), which is normalized and swapped in immediately. If the `apply_custom_to_filenames` naming setting is enabled, the editor warns that saving will also rename the file on disk. These use the metadata-override and cover-upload endpoints added in the previous release.
- **List & Table Library Views:** The library now offers three layouts, chosen with a Grid / List / Table switcher next to the search and sort controls; your choice is remembered across reloads (stored in the browser, like the light/dark theme). **Grid** is the original cover-card layout. **List** shows one book per row with a thumbnail and extra metadata (narrator, series, release date) alongside its status and action button. **Table** is a dense, tabular view (micro-thumbnail, title, author, series, status, actions) that scrolls horizontally on narrow screens. All three share the same click behavior — open a book's detail modal, or use its Download / Re-download button — and honor the existing search, sort, and status filters.
- **Download & Re-download Buttons on Library Cards:** Every library card now carries a status-aware action button, so you no longer have to open the detail modal to act on a book. Books that aren't on disk yet (New, Missing, Error) get a **Download** button; already-downloaded books get a cautionary yellow **Re-download** button that asks for confirmation before overwriting the existing file. This replaces the old Error/Missing-only "Retry" button and mirrors the detail modal's logic exactly.
- **Clickable Library Status Boxes:** Clicking a count in the dashboard's Library Status panel (Downloaded, New, Missing, Error) now filters the library grid to that status and scrolls down to the results, reusing the existing filter controls.
- **Clear Finished Jobs Button:** The Job History page gained a **Clear Finished** button that deletes all completed, failed, and cancelled jobs from history (with a confirmation prompt), wired to the existing `POST /api/jobs/clear` endpoint. Any active or queued job is never affected.
- **Optional Lossless / No-Re-encode Mode:** A new `no_reencode` conversion setting (off by default) strips the DRM and keeps Audible's original audio instead of always re-encoding it. When on, the decrypted AAC audio is copied straight through while chapters, metadata, and cover art are muxed on — much faster than the default per-chapter re-encode, with no quality loss (the `quality` setting is ignored in this mode). The default behavior is unchanged for everyone who leaves it off. If a title has to fall back to Audible's slower FLAC decryption (rare), that one book re-encodes as before, since FLAC can't be copied into an `.m4b`. Single-chapter books are left with their native chapters rather than being split into synthetic "Part N" markers. This is set via the settings file for now; a Settings-page toggle is planned for a later release.
- **Clear Old Job History:** A new `POST /api/jobs/clear` endpoint (login- and Origin-gated) removes finished jobs and their items from history. It accepts `{"mode": "all"}` to clear every finished job or `{"mode": "older_than", "days": N}` to clear only those older than N days. Only COMPLETED/FAILED/CANCELLED jobs are eligible — a running or queued job is never deleted. This is the backend for the history-page clear button planned for a later release.
- **Manual Import of Existing Audiobook Files:** You can now bring already-owned `.m4b`/`.m4a` files into the managed library through two paths: a **scan-in-place** job that walks `/data` and adopts any importable file the library doesn't already track, and a **streaming upload** endpoint (`POST /api/library/import/upload`, login- and Origin-gated, with a configurable `import.max_upload_gb` size cap, default 2 GB) that places the file into `/data` and adopts it. Files carrying an Audible ASIN tag that matches a known book are reconciled in place; everything else is recorded as an imported book (a new `source` provenance marker distinguishes them from Audible purchases, and re-scans are idempotent — no duplicate rows). Metadata and cover art are read from the file's own tags. This is the backend foundation; a dedicated import UI is planned for a later release.
- **Optional Subtitle Trimming in Filenames:** A new `truncate_subtitle` naming setting (off by default) drops a long "Main Title: Subtitle" subtitle from the title used in file and folder names (e.g. "999: The Extraordinary Young Women..." becomes "999"). It splits on the first colon-space, so ratios/times like "12:00" are left alone; the embedded metadata keeps the full title, and titles without a subtitle are unaffected.
- **Companion PDF Downloads:** Audiobooks that ship with a supplementary PDF (booklets, maps, reference material) now have that PDF downloaded and saved next to the `.m4b`, sharing its name. Enabled by default; the new `download_supplementary_pdf` conversion setting turns it off. Titles without a PDF are unaffected.

### Fixed
- **Cover Art Is Now Embedded in Downloaded Files:** Converted `.m4b` files were missing their cover art. ffmpeg's mp4 muxer cannot write an attached-picture cover and the extended metadata tags (Publisher, ASIN, etc.) in the same pass — enabling the tags silently turned the cover into an unusable data stream. The cover is now embedded in a dedicated step (via AtomicParsley) that writes the standard cover atom without disturbing the metadata, so files show artwork in players again while keeping all their tags. Cover embedding is best-effort: if it fails, the audiobook is still produced.
- **Downloads Are Verified Before Being Marked Complete:** A book could be reported as successfully downloaded when its file was actually missing from disk (a "ghost" entry with no way to re-download), or silently truncated. After the final merge, the app now confirms the output file exists, is a plausible size, and — when Audible reports a runtime — matches the expected duration, before marking the book DOWNLOADED. Anything that fails those checks is marked ERROR with the reason, so it can be retried.
- **Clear Failure Messages for Unavailable Titles:** A failed download previously showed only a generic `Failed during asset download/preparation.` The progress reader drained the downloader's output before the error could be captured, so the real cause was lost. The actual message from `audible-cli` (for example, a title no longer being available to your account) is now captured and shown as the book's error.
- **Bulk Duplicate Downloads No Longer Overwrite Each Other:** Downloading two different books that share the same author and title in a single job (common with public-domain classics — multiple recordings of *Dracula*, *Pride and Prejudice*, etc.) could silently overwrite the first file with the second. Both books chose their output path before either had written its file, so the existing on-disk collision check couldn't see the conflict. Colliding books now reserve their target path in-process, so the second and later copies get a guaranteed-unique ASIN-suffixed name (`Title_B00XYZ.m4b`) and are flagged as duplicates in the database (surfacing in the UI is planned for a later release).
- **Cancelling a Book Mid-Conversion No Longer Leaves It in Error:** Cancelling a job while a book's ffmpeg (chapter encode, final merge, or lossless remux) was actually running left that book stranded in ERROR with a misleading "… failed." message, even though the job itself was correctly recorded as cancelled. A mid-conversion cancel is now treated like the download-phase cancel — the book's status is left untouched (staying New/Missing) so it's picked up on the next run instead of needing a manual retry. This now also covers the final read-back verification step: cancelling in the brief window while the just-finished file is being checked no longer deletes that valid file and marks the book Error — the completed file is kept and the book is left for the next run.
- **Failed Conversions No Longer Leave a Broken File Behind:** When a finished file failed its post-conversion integrity check (missing, implausibly small, or truncated), the book was correctly marked ERROR but the bad `.m4b` was left sitting at its final path in `/data`, looking like a real book until a later retry happened to overwrite it. The failed artifact is now deleted when verification fails.
- **Faster, Fully Cancellable Conversions:** The short metadata, activation-bytes, and ffprobe/ffmpeg probe calls made during a conversion are now tracked for cancellation like the long-running ones, so cancelling a job reaches them immediately instead of waiting for the current step to finish. Two internal robustness fixes ride along: the collision-check probe no longer holds a global lock across an ffprobe call (which could briefly serialize other books during bulk re-downloads), and the downloader's output is now drained concurrently so a chatty download can't stall. Cancelling during the post-download integrity probes now stops promptly too, instead of falling through to start a full (and equally cancelled) fallback decode that the cancel could no longer reach.
- **Sturdier Manual Import:** Several rough edges in the new import path were smoothed out. Uploaded `.m4a` files now keep their real `.m4a` extension instead of being stored as `.m4b`. An empty or unreadable file found during a scan-in-place is skipped rather than added as a junk library row. A cancelled import scan now reports "Cancelled …" (at the point it stopped) instead of a misleading "Done" at 100%. The scan also no longer skips a directory whose name merely starts with the staging folder's name (e.g. `.import_staging_old`), and an upload rejected for being too large or the wrong type now drains the connection so the browser reliably sees the error. Placing an uploaded file is now fully collision-safe: it walks past every occupied name until it finds a free path, so an upload can never overwrite an existing library file — including a duplicate book already downloaded under its `Title_<ASIN>.m4b` name. A non-empty but unreadable upload (e.g. a non-audio file renamed with an `.m4b` extension) is now rejected with a clear error instead of being adopted as a bogus library row, matching how the scan already treats such files. And cancelling a scan while its last (or only) file is being adopted now correctly reports "Cancelled" rather than "Done".
- **Safer Job-History Clearing:** The "Clear Finished" history action now re-asserts the finished-status condition on the delete itself, so a running or queued job can never be removed even in theory. (No behavior change in normal use.)
- **Import Detail Label & Internal Tidy-ups:** A handful of low-severity polish items from the release review. The book detail panel now labels an imported file by its real extension (an imported `.m4a` reads ".m4a Audiobook" instead of always ".m4b Audiobook"). Internally, the main decryption subprocess's cancellation-registry cleanup is now paired in a `finally` block so an unexpected failure can't leak a live process into the registry.
- **Library UI Polish:** A handful of small front-end rough edges were fixed. Replacing a book's cover now shows the new art everywhere: not only after closing and reopening the detail modal (the cached original was previously served until a hard refresh) but also in the library grid thumbnail, which used to revert to the stale cached image the next time the page data refreshed. If a download can't be started because another job is already running (for example, one launched from another tab), the running job's status panel is no longer briefly replaced by the refused selection — the panel now re-syncs to whatever is actually running. A partial bulk rename now keeps the books that failed selected — with the toast noting they're still selected — so you can retry them without re-picking, instead of clearing the whole selection. The large-download confirmation falls back to a plain browser prompt if the styled dialog is unavailable, and toast notifications now render their message as text, so an error string can never inject markup.

## [0.18.0] - 2026-07-20

This is a **stability and security hardening** release with no new user-facing features. It resolves every finding from an end-to-end code and security review of the v0.17.0 codebase — fresh-install database integrity, several security fixes (login redaction, cover-art authentication, CSRF protection, metadata escaping, open-redirect validation), correct job cancellation and timeouts, and a range of reliability and housekeeping improvements. It also adds the project's first automated test suite and pins all dependencies for reproducible builds.

### Fixed
- **Setup Wizard Cleanup:** The pasted Audible login URL was submitted with two literal junk characters (`\n` as text, not a newline) appended — audible-cli happened to tolerate it, but the URL is now sent exactly as pasted. Also removed a dead success-detection branch that could never fire (success is signalled by a dedicated event) and consolidated the wizard's two Socket.IO connections into one.
- **Job Timestamps Are Now Timezone-Aware:** Job start/end times are recorded with an explicit UTC offset instead of via the deprecated `datetime.utcnow()` (removed-in-spirit since Python 3.12, so this future-proofs a base-image upgrade). Side benefit: the History page previously interpreted the offset-less timestamps as local time, showing UTC times as if they were local — new records now display in your actual local time. Old records keep their old format and rendering.
- **SECURITY: Cover Art Now Requires Login:** The `/covers/` image endpoint was the only page content served without authentication, so anyone with network access to the app could enumerate cover thumbnails and learn your library's contents. Cover requests now require a logged-in session like every other endpoint; the browser sends the session cookie with image requests automatically, so nothing changes for normal use.
- **SSE Listener Cleanup:** Closing a dashboard tab left its server-side event queue (and the thread serving it) alive until 10 undelivered messages had backed up. Disconnected clients are now unsubscribed immediately. The readme also gained a "Deployment Notes" section describing the single-user server design and reverse-proxy requirements.
- **SECURITY: Cross-Site Request Forgery Protection:** State-changing requests (POST etc.) now require the browser-supplied `Origin` header, when present, to match the request's host; mismatches are rejected with a 403 and logged. This blocks malicious websites from triggering actions (starting jobs, changing settings, resetting authentication) in a logged-in user's session. Requests without an `Origin` header (e.g. `curl`) are unaffected. **Reverse-proxy users:** your proxy must forward the `Host` header (the standard configuration) or write requests will be rejected.
- **SECURITY: Book Metadata Escaped in the UI:** Book titles, authors, and other Audible-supplied metadata were interpolated into the page HTML unescaped in the library grid, job panel, download-selection modal, and history page — a book title containing HTML could inject markup or scripts into the UI. All book-derived strings are now HTML-escaped before rendering.
- **Cancellation Now Reaches Queued Work:** Cancelling a download job killed the ffmpeg/audible-cli processes already running, but tasks still waiting in the queue would start fresh work afterwards — spawning new encodes against already-deleted temp directories and littering the log with tracebacks. Queued tasks now check the cancellation signal before doing anything, and books that were cut short by a cancel are recorded as CANCELLED in job history instead of FAILED.
- **Conversion Timeout Scales With Book Length:** The per-book processing watchdog was a fixed 2 hours, which could kill legitimate conversions of very long books on slow hardware. The timeout is now at least 2 hours and grows to 4x the historical conversion-time estimate for the book's runtime.
- **Settings Save No Longer Hangs During Downloads:** Saving settings rebuilt the encoding worker pool and waited for every in-flight ffmpeg task to finish first, so a settings save during a large download could hang the request (and the UI) for minutes. Reconfiguration is now a no-op when the core count hasn't changed, and an actual change swaps in the new pool immediately while old tasks finish on their existing threads.
- **Chained Auto-Download Stability:** The download job that "Process new books on sync" chains after a sync re-entered the finished sync worker's Flask context object from a separate thread, which could intermittently break the hand-off. It now gets its own fresh app context.
- **Verify Job Failure Reporting:** When a Verify Library job crashed, the UI was still told it COMPLETED (history showed FAILED, with no end time). The finish event now carries the real outcome, the end time is recorded on failure, and the job tracker is fully cleared so the state can't leak into the next job.
- **Log File Rotation:** `app.log` now actually rotates (10 MB per file, 3 backups kept) as its documentation always claimed, instead of growing without bound on the `/config` volume.
- **Cover Download Timeout:** Cover-art downloads during a sync had no network timeout, so a stalled connection to Audible's image CDN could hang the entire sync job indefinitely. Cover requests now time out (5s to connect, 30s per read) and fall through to the existing "could not process cover" handling, letting the sync continue.
- **Graceful Logging Fallback:** If the log file in `/config` can't be opened (e.g. a read-only mount), the app now logs a warning and continues with console-only logging instead of crashing at startup. The `/config` and `/database` paths are also overridable via `CONFIG_DIR`/`DATABASE_DIR` environment variables, which lets the code run outside the container for testing — container behavior is unchanged.
- **Collision Protection for Untracked Files:** The v0.17.0 collision check only protected files that were tracked in the library database — an untracked file sitting at a download's target path (e.g. an audiobook from another tool, or one predating AudioBookup) was silently overwritten, with a log line claiming it was safe. Untracked files are now probed for their embedded ASIN tag: only a file that is verifiably an old copy of the same book gets overwritten; anything else keeps its place and the new download is renamed with a unique ID (`Title_B00XYZ.m4b`), matching the existing behavior for tracked collisions.
- **"Reset Audible Connection" Restart Mechanism:** The internal shutdown endpoint relied on a private Werkzeug hook (`werkzeug.server.shutdown`) that modern Werkzeug versions no longer provide, which would make the reset flow crash instead of restarting the app. The endpoint now replies to the browser first and then exits the process, letting Docker's `restart: unless-stopped` policy bring the container back up in Setup Mode.
- **SECURITY: Open Redirect on Login:** The login page redirected to whatever URL the `next` query parameter contained, so a crafted link (e.g. `/login?next=https://evil.example`) could bounce a user to an attacker's site right after they logged in. The redirect target is now validated to be a local path — anything else falls back to the dashboard. (The code comment always claimed this check existed; now it does.)
- **Settings Defaults Corruption:** `load_settings()` returned a shallow copy of the built-in defaults, so the nested dictionaries (job, naming, conversion, tasks) were shared with `DEFAULT_SETTINGS`. Saving settings merged your changes into those shared dictionaries, silently corrupting the in-memory defaults until the next restart — which could make a broken or deleted `settings.json` "fall back" to your last-saved values instead of the real defaults. Settings are now deep-copied on every load.
- **SECURITY: Auth-File Password No Longer Logged:** Starting the setup wizard logged the full setup payload — including the auth-file encryption password in plain text — to the console and `app.log` (which is downloadable from the UI). The password is now redacted from that log line. If you have completed setup before, consider clearing your log from the Settings page.
- **CRITICAL: Fresh-Install Database Schema:** Databases created by a fresh install were built with typeless columns, no PRIMARY KEY on `asin`, and no DEFAULT values (upgraded databases were unaffected). New installs now get the correct schema, and existing databases with the defect are automatically detected and rebuilt on startup — a backup copy (`library.db.pre-schema-fix.bak`) is saved first, and duplicate-ASIN rows (possible only under the old schema) are reported if dropped.

### Added
- **Automated Test Suite:** A pytest-based regression suite (`tests/`, development-only, not shipped in the Docker image) now locks in the recent security and correctness fixes: settings merge/deep-copy behavior, filename sanitization and download collision handling, login redirect validation, and the download-selection database queries.

### Removed
- **Dead Legacy Script Endpoints:** The `/run_action` and `/run_single_action` routes (and their shell-script streaming helper) have been deleted. They streamed output from shell scripts (`sync.sh`, `download.sh`, `process_book.sh`) that no longer exist, and nothing in the UI has called them since the Python job system replaced that pipeline.

### Changed
- **Default File Permissions (`UMASK`):** Files created by the app were world-writable (777 directories / 666 files) because the container hardcoded `umask 0000`, despite its comment claiming otherwise. The default is now `0002`, producing group-writable 775/664 as the comment always intended, and the mask is configurable via a new `UMASK` environment variable. **If you relied on world-writable output** (e.g. certain NAS/SMB share setups), set `UMASK=0000` in your compose file to restore the old behavior. Existing files are untouched; only newly created files are affected.
- **Pinned Dependencies:** All Python dependencies in `requirements.txt` are now pinned to the exact versions from the known-good v0.17.0 image. This makes Docker image builds reproducible — a rebuild on a new machine or at a later date can no longer silently pick up incompatible library versions.

## [0.17.0] - 2026-01-21

This release focuses on **Library Curation & File Management**. It introduces safety checks to prevent data loss, expanded tools for organizing your library, and significantly richer metadata tagging for better compatibility with media players.

### Added
- **Smart Collision Protection:** The download engine now checks if a file already exists at the target destination.
    - If the existing file belongs to a *different* book (e.g., a different edition of *Dracula* with the same Title/Author), the new file is automatically renamed with a unique ID (`Title_B00XYZ.m4b`) to prevent overwriting the existing one.
    - If the file belongs to the *same* book, it is treated as a valid re-download and overwritten.
- **Extended Naming Templates:** The "Folder/File Naming" setting now supports new placeholders: `{narrator}`, `{publisher}`, and `{asin}`. This allows for organization schemes like `{author}/{title} ({narrator})/{title}`.
- **Context-Aware Download Button:** Added a dynamic button to the Book Detail modal:
    - **"Download Now":** Appears for New, Missing, or Error status books.
    - **"Force Re-download":** Appears for Downloaded books. This allows you to easily fix corrupted files or update metadata without manually deleting files from the filesystem.
- **Metadata Enrichment:** The conversion engine now injects industry-standard metadata tags into the `.m4b` file to match tools like Libation.
    - Added: `Album`, `Album Artist`, `Genre` (extracted from Audible categories), `Publisher`, `Language`, and `Audible ASIN`.
    - This ensures better sorting and display in players like Plex, Audiobookshelf, and Apple Books.

## [0.16.0] - 2026-01-03

### Added
- **Multi-Architecture Support (ARM64/Apple Silicon):** The application is now built as a multi-architecture Docker image. It runs natively on **Apple M-Series chips (M1/M2/M3/M4)** and **Raspberry Pi (64-bit)** without emulation, resulting in significantly better performance and stability.
- **Robust Startup Permissions:** The container startup script has been improved to handle permission setting on the `/data` volume more gracefully. It now catches file system errors (common on macOS mounts) without crashing the container, and allows users to opt-out of permission checks completely by setting the environment variable `SKIP_DATA_PERMS=true`.

### Fixed
- **macOS OOM Crashes:** Resolved an issue where running the Intel image on Apple Silicon via Rosetta 2 emulation would cause the container to crash with Exit Code 137 (Out of Memory).

## [0.15.6] - 2026-01-02

### Fixed
- **UI State Management:** Fixed a visual bug where the "Job Status" panel header would display the title of the *previous* completed job instead of the *current* running job when transitioning between tasks (e.g., Sync -> Download).
- **Frontend Self-Healing:** Implemented a "Watchdog" system in the dashboard. If the browser misses a "Job Finished" signal from the server (due to network blips), the UI now automatically detects the completion within 5 seconds and unlocks the interface, preventing buttons from getting stuck in a disabled state.

## [0.15.5] - 2025-12-30

### Fixed
- **UI Freeze:** Fixed a race condition in the "Job Finished" event handler. Previously, if a minor UI update failed (e.g., finding a DOM element), the interface would remain locked in a "Processing" state even though the background job had completed successfully. The handler is now wrapped in a safety block to ensure the UI always unlocks.

## [0.15.4] - 2025-12-30

This release introduces a "Smart Download Strategy" that balances speed with absolute reliability, along with tools to audit existing libraries for corruption.

### Added
- **Library Integrity Check:** Added a new tool in **Settings > Audible Connection** that scans your entire downloaded library. It compares actual file durations against metadata to identify and flag truncated or corrupt files as `ERROR`, allowing for easy re-downloading.
- **Duration Integrity Guard:** The download pipeline now strictly verifies that the downloaded file matches the expected runtime from Audible before processing begins, preventing "silent truncation" errors caused by network drops.

### Changed
- **Smart Download Strategy:** The application now attempts to use the high-speed `.aaxc` format first. If decryption or key rotation issues are detected, it automatically falls back to the legacy `.aax` format. This restores fast download speeds for 95% of books while maintaining 100% compatibility for complex multi-part titles.
- **Optimized Decryption:** The processing engine now attempts a fast "Stream Copy" decryption first. It verifies seek integrity and only falls back to the slower "FLAC Decode" method if the file structure requires it, significantly speeding up processing for standard books.

## [0.15.3] - 2025-12-30

### Fixed
- **CRITICAL: Enhanced AAX (AAXC) Processing:** Fixed a major bug where books using the `.aaxc` format with encrypted key rotation (often multi-part or very long books) would fail to decrypt properly during chapter splitting, resulting in empty or corrupted audio chunks. The system now performs a lossless decryption of the master file before processing to ensure perfect seeking integrity.

## [0.15.2] - 2025-12-04

This release focuses on significantly improving the debugging experience and user feedback loop. It introduces a comprehensive logging overhaul and several quality-of-life UI improvements.

### Added

- **Log Management Tools:** Added buttons to the dashboard footer to **Download** the full debug log as a file and **Copy** the visible log to the clipboard.
- **Enhanced UI Logging:** The dashboard log now includes timestamps, lists specific books in the queue, and provides clear, granular updates when individual books start processing and finish.

### Changed

- **Logging Architecture:** Implemented a "Quiet UI, Loud File" strategy. The visible UI log remains clean and readable (INFO level), while the downloadable `app.log` now captures verbose `DEBUG` details, including full ffmpeg command traces and API responses for easier troubleshooting.

## [0.15.1] - 2025-12-03

### Fixed

- **CRITICAL: Unicode Metadata Crash:** Fixed a critical bug where the download process would crash when processing books containing special characters (e.g., copyright symbols ©, accented letters) in their metadata. The system now gracefully handles non-UTF-8 characters during the metadata extraction phase.

## [0.15.0] - 2025-11-20

This release focuses on stability, user experience, and architectural improvements. It introduces a modular frontend structure, robust job cancellation, smart auto-chunking for large files, and several quality-of-life UI enhancements.

### Added

- **Smart Auto-Chunking:** Books that consist of a single long chapter (over 30 minutes) are now automatically split into 15-minute segments during conversion, ensuring better navigation on players that struggle with massive files.
- **True Job Cancellation:** The "Cancel Job" button now instantly terminates active background processes (like `ffmpeg` encoding or `audible-cli` downloads) via `SIGTERM`, stopping jobs immediately rather than waiting for the current step to finish.
- **Maintenance Tools:** Added dedicated buttons in the Settings menu to **Reset Audible Connection** and **Clear Image Cache** without needing to access the file system.
- **Toast Notifications:** Replaced blocking alerts with non-intrusive "Toast" popups for actions like saving settings or starting jobs.
- **Copy Log Button:** Added a button to the activity log footer to instantly copy the full log contents to the clipboard.
- **Startup Cleanup:** The application now automatically detects and removes orphaned temporary files from previous sessions (e.g., after a container crash) upon startup to prevent disk bloat.

### Changed

- **MAJOR: Frontend Modularization:** The monolithic `index.js` has been refactored into distinct ES6 modules (`job-manager`, `library-manager`, `modal-manager`). This improves code maintainability and prepares the application for a future migration to a modern frontend framework.
- **Sync Logic:** The library sync process now registers its subprocesses, allowing it to be safely and instantly cancelled by the Job Manager.

### Fixed

- **CRITICAL: Reverse Proxy Compatibility:** Resolved a critical issue where the application would return a `403 Forbidden` error when accessed through a reverse proxy with a Web Application Firewall (WAF) enabled.
- **CRITICAL: Application Security:** Hardened the login process against Open Redirect vulnerabilities by validating the `next` redirect parameter on the server.
- **CRITICAL: Login Page Access:** Fixed a bug that caused an `Internal Server Error` when accessing the login page via a `GET` request.
- **UI (Settings):** Corrected a bug where clicking the descriptive text for the new "Reset Connection" feature would incorrectly trigger the button's action.

## [0.14.2] - 2025-10-17

This is a quality-of-life and code-health release that resolves numerous UI bugs, improves the user experience on the settings page, and prepares the frontend for future development.

### Changed

- **JavaScript Modularization:** All inline JavaScript from `index.html`, `settings.html`, `history.html`, and `setup.html` has been extracted into dedicated external module files (`index.js`, `settings.js`, etc.) to improve maintainability and prepare for a future framework migration.
- **Improved Performance Setting:** The "Total Processing Cores" setting is no longer hidden in Advanced Mode and is now directly editable in the simple UI, making it easier for all users to configure application performance.
- **Documentation:** The main `readme.md` has been updated with a project logo and screenshots for a more professional and engaging presentation along with more installation and setup details.

### Fixed

- **CRITICAL: File Permissions:** The Docker entrypoint now sets a `umask` of `0000`, ensuring that all files and directories created by the container (e.g., `.m4b` audiobooks) have correct group-write permissions. This resolves a major usability issue for users on systems like Unraid.
- **UI (Settings):** Fixed numerous responsive CSS bugs where cron inputs and radio buttons would wrap incorrectly or be pushed off-screen on narrow viewports.
- **UI (Footer):** The fixed server status footer no longer overlaps the main content container on pages without a log bar.
- **UI (Jobs):** The progress bar for books with only a single chapter now correctly displays a "Processing" state instead of jumping directly from "Downloading" to "Merging".

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

# AudioBookup

## Project Context

**AudioBookup** is a self-hosted, Dockerized web application for managing, downloading, and converting a personal Audible audiobook library into DRM-free `.m4b` files. Python 3.11 (Flask) backend, vanilla JavaScript (ES modules) frontend, SQLite storage, all shipped as a single multi-arch Docker container (`ghcr.io/ishbuggy/audiobookup`). Current version: **v0.17.0**.

The project was developed incrementally with chat-based LLM assistance before agentic tooling. It is **stable and in use by real users** — "if it ain't broke, don't fix it" is the house philosophy. Prefer minimal, surgical changes; don't refactor working code without a concrete reason.

## Architecture

### Data persistence (three container volumes, strict separation)
- `/database` — critical, non-regenerable data: `library.db` (SQLite), `.audible/` (audible-cli auth), `.setup_complete` flag. `HOME` is set to `/database` so audible-cli finds its config.
- `/config` — regenerable state: `settings.json`, `app.log`, `covers/` cache, `temp_processing/`, `secret.key`, `.eta_cache.json`, `.file_scan_cache`.
- `/data` — final output directory for converted `.m4b` audiobooks.

### Concurrency model (the load-bearing design)
- **`job_manager.py`** — one job (SYNC / DOWNLOAD / VERIFY) at a time, tracked in the `jobs`/`job_items` tables; spawns a worker thread per job.
- **`task_runner.py`** — global `ThreadPoolExecutor` fed by a `PriorityQueue`; ENCODE_CHAPTER > PREPARE_BOOK > MERGE_BOOK priorities maximize throughput.
- **`process_registry.py`** — thread-safe map of job_id → live `subprocess.Popen` objects (ffmpeg, audible-cli); job cancellation sends SIGTERM to all of them instantly.
- **`processing_logic.py` (`BookProcessor`)** — lifecycle of one book: temp dir, naming template + collision handling, submits tasks, updates DB.
- **`chunked_conversion_logic.py`** — **the critical core.** Smart download strategy (AAXC fast → AAX fallback), smart decryption (AAC copy → FLAC fallback), duration/seek integrity checks, auto-chunking of single-chapter books, ffmpeg split/merge. These fallback chains encode hard-won knowledge about Audible's formats — do not simplify or "clean up" this file without explicit discussion.

### Other backend modules
- `__init__.py` — app factory, path constants, SSE `announcer`. `routes.py` — all HTTP endpoints. `auth.py` — `@login_required` (session + setup-state gating). `settings.py` — thread-safe `settings.json` load/save with `DEFAULT_SETTINGS`. `scheduler.py` — APScheduler cron jobs (fast sync, deep sync, auto-process, auth check). `sync_logic.py` — API fetch + optional deep filesystem scan. `verification_logic.py` — library integrity audit. `setup_pty.py` — pexpect-driven `audible quickstart` wizard over Socket.IO. `health_check.py`, `eta_estimator.py`, `logger.py` ("quiet UI, loud file": INFO to console, DEBUG to `app.log`).
- Real-time UI updates flow through **SSE** (`/api/jobs/stream`, `MessageAnnouncer`); the setup wizard alone uses **Socket.IO** (path `/setup/socket.io`).

### Database schema lives in `bin/start.sh`
Schema creation and migration happen at container start via the `DB_SCHEMA` associative array and ALTER TABLE checks — **not** in Python. Any new column or table goes there as an idempotent migration. `bin/start.sh` also gates Normal Mode vs Setup Mode on the `.setup_complete` flag.

### Frontend
Vanilla JS ES modules, no framework, no build step. `static/js/modules/` holds `job-manager.js` (SSE handling, job panel, self-healing watchdog that polls `/api/jobs/active` every 5s), `library-manager.js` (grid, search/sort/filter, lazy loading), `modal-manager.js` (detail + download-selection modals). Page entry points: `index.js`, `settings.js`, `history.js`, `setup.js`; `ui.js` provides global modals/toasts (`window.showCustomAlert`, `window.showConfirmationModal`, `window.showToast`).

## Reference Documentation

Internal and third-party docs live in `ref-docs/` — **gitignored, local-only, never committed or pushed**. Prior plans/prompts are under `ref-docs/setup-docs/`. If a referenced file is missing, ask before assuming its contents. Investigation write-ups and other working documents that shouldn't ship in the repo also belong there.

## Development Guidelines

- **One change at a time.** Keep each change minimal and testable in isolation; the user tests between changes by rebuilding the container. Split big changes into steps.
- **Never collapse or elide code** when showing or writing files — no "// rest unchanged" placeholders in actual file edits.
- **Don't break working features to modernize them.** Larger refactors (e.g., the roadmap's TypeScript/Svelte migration) happen only as deliberate, planned work.
- **Comments:** the codebase is heavily commented in a tutorial style. Match the surrounding density when editing existing files; keep new comments focused on constraints and non-obvious behavior.
- **Subprocess calls to `audible`/`ffmpeg`:** always register long-running processes with `process_registry` (register/unregister in try/finally) so cancellation works, and set `env["HOME"] = DATABASE_DIR` for audible-cli calls. Treat exit code `-15` (SIGTERM) as cancellation, not failure.
- **Settings:** new settings get a default in `settings.py:DEFAULT_SETTINGS`, are saved via the deep-merge in `POST /api/settings`, and are read with `.get()` chains and fallbacks (users have old `settings.json` files).
- **Secrets:** never log credentials or auth-file passwords; `app.log` is user-downloadable from the UI.

## Coding Standards & Tooling

- **Python:** Ruff (lint + format), config in `pyproject.toml` (line length 120, rules E/W/F/I/UP). Run `ruff check src/` and `ruff format --check src/` before considering a Python change complete.
- **JS/HTML/CSS:** Prettier, config in `.prettierrc.json` (4-space indent, print width 110).
- **Tests:** none exist yet. When a test harness is added (pytest), run it before completing any task; until then, verification is manual via the dev container.
- **Changelog:** maintained in `CHANGELOG.md` (Keep a Changelog format). Add an entry under an `[Unreleased]` heading when completing a feature or fix; entries get folded into a version heading at release time.
- **Licensing:** `LICENSE.txt` carries attributions (audible-convert.sh, Immich). If code is adapted from another project, add the attribution comment in-file and update `LICENSE.txt`.

## Development Workflow

### Environment
- Development happens on an Ubuntu Server VM over SSH (VS Code remote). The app **only runs inside the container** — paths like `/config`, `/database`, `/data` don't exist on the host, so Python modules can't be meaningfully executed outside Docker.
- Dev testing: `docker compose -f docker-compose.dev.yml up -d --build` (builds from source; `docker-compose.dev.yml` and `docker-compose.override.yml` are local-only and untracked — a tracked `.template` exists for the dev file).
- `docker-compose.yml` is the **user-facing production example** that pulls from GHCR — treat its contents as documentation for end users.

### Planning before implementation
- For any non-trivial change (new functionality, structural changes, anything touching more than one file), produce a plan before writing code and save it to `PLAN.md` in the project root (overwrite any existing one). Wait for explicit approval before implementing.

### Review before committing
- Review findings go to `REVIEW.md` in the project root (overwrite existing), organized by severity. Reviews report findings only — fixes are a separate, approved step.
- Run `ruff check` first and fix trivia before spending review effort on it.

`PLAN.md` and `REVIEW.md` are working documents, **gitignored and local-only** (like `ref-docs/`) — never commit them.

### Commit strategy
- Commit at natural checkpoints — each coherent, working, reviewable unit — rather than one big commit per feature. Small commits keep rollback useful.
- **Never push without being explicitly told to.** Commit locally freely; push only on request.
- Commits are authored as **ishbuggy** (the repo's configured git user) only — no Claude co-author trailer, no AI attribution lines in commit messages.

### Release process (only when explicitly requested)
1. Ensure `CHANGELOG.md` has the new version section and the release commit (`Release vX.Y.Z: <summary>`) is on `main`.
2. Tag `vX.Y.Z` and `git push --tags`.
3. A GitHub Release published from the tag triggers `.github/workflows/docker-publish.yml`, which builds the multi-arch image (passing `APP_VERSION` from the tag) and pushes to `ghcr.io/ishbuggy/audiobookup`.
- Reaching a good stopping point is **not** permission to tag, release, or push images.

## Roadmap (context, not a to-do list)

- **Deferred from v0.17:** custom metadata editing (`custom_title`/`custom_author` columns, `/api/book/<asin>/update`, edit UI in the detail modal) — planned but never implemented.
- **Future ideas:** proactive/event-driven library sync; multiple country/account profiles (multiple auth chains); TypeScript or Svelte frontend migration.
- Known issues and technical-debt inventory: see `REVIEW.md`.

# AudioBookup

## Project Context

**AudioBookup** is a self-hosted, Dockerized web application for managing, downloading, and converting a personal Audible audiobook library into DRM-free `.m4b` files. Python 3.11 (Flask) backend, vanilla JavaScript (ES modules) frontend, SQLite storage, all shipped as a single multi-arch Docker container (`ghcr.io/ishbuggy/audiobookup`). Latest shipped release: **v0.21.0**; **v0.22.0** is in flight (see `PLAN.md`).

The project was developed incrementally with chat-based LLM assistance before agentic tooling. It is **stable and in use by real users** — "if it ain't broke, don't fix it" is the house philosophy. Prefer minimal, surgical changes; don't refactor working code without a concrete reason.

## Release Roadmap

**The roadmap lives in `ROADMAP.md`** — shipped milestones, the next milestone's theme, and the longer-horizon ideas. Consult it before scoping any work; update it when a milestone's scope or status changes. Milestones are frequently driven by user feedback collected in `ref-docs/user-reports/`.

The rule that stays here because it governs how the assistant works: **this project is built incrementally — do not implement features from a later milestone while working on an earlier one unless explicitly asked.** The roadmap exists specifically to keep scope contained. Forward-compatibility design is the deliberate, documented exception, decided at planning time, not ad hoc.

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
- Real-time UI updates flow through **SSE** (`/api/jobs/stream`, `MessageAnnouncer`); the setup wizard alone uses **Socket.IO** (path `/setup/socket.io`). This split is deliberate — don't consolidate it.

### Database schema lives in `bin/start.sh`
Schema creation and migration happen at container start via the `DB_SCHEMA` associative array and ALTER TABLE checks — **not** in Python. Any new column or table goes there as an idempotent migration; it will run against real users' databases on their next pull, so it must tolerate every prior schema state. `bin/start.sh` also gates Normal Mode vs Setup Mode on the `.setup_complete` flag.

### Frontend
Vanilla JS ES modules, no framework, no build step — deliberate, not legacy. `static/js/modules/` holds `job-manager.js` (SSE handling, job panel, self-healing watchdog that polls `/api/jobs/active` every 5s), `library-manager.js` (grid, search/sort/filter, lazy loading), `modal-manager.js` (detail + download-selection modals). Page entry points: `index.js`, `settings.js`, `history.js`, `setup.js`; `ui.js` provides global modals/toasts (`window.showCustomAlert`, `window.showConfirmationModal`, `window.showToast`).

### Reference Documentation
`ref-docs/` is a **local-only, gitignored** documentation directory (third-party docs, investigation reports, user feedback reports, and any other working notes) — it stays out of version control and never ships with the repo. **Never stage, commit, or force-add anything under `ref-docs/`** — no `git add ref-docs/…`, no `git add -f`, no bulk `git add -A`/`git add .` that would sweep it in. This holds even if a task prompt literally says "commit it": the directory is ignored for a reason, so write the file there and leave it untracked. A `PreToolUse` hook (`.claude/hooks/git-add-guard.sh`) enforces this.

Prior plans/prompts live under `ref-docs/setup-docs/`, user feedback under `ref-docs/user-reports/`, and third-party reference material in per-topic folders (e.g. `ref-docs/libation/`). If a referenced file is missing, ask before assuming its contents.

`.claude/` (the hooks, skills, and session settings referenced throughout this document) is likewise gitignored and local-only — it configures the assistant on this machine and never ships with the repo.

### Investigation and Phase Reports
Investigation/verification write-ups and per-phase implementation summaries live in `ref-docs/reports/` — gitignored, local-only, same as other `ref-docs/` content. **Writing the report is owned by the `phase-report` skill; invoke it rather than placing files by hand.** The structure it enforces: **one `ref-docs/reports/vX.Y.Z/` folder per version, claimed once when the milestone opens; every phase is a subfolder inside it** (`phase-N-<slug>/`), with `release-review/` and `spikes/` alongside, and version-prefixed filenames (`vX.Y.Z-phase-N-<slug>-report.md`, never a bare `report.md`). Non-version work (postmortems, standalone investigations) gets its own `ref-docs/reports/<topic-slug>/` folder. Folders predating this convention stay where they are.

## Development Guidelines

- **One change at a time.** Keep each change minimal and testable in isolation; the user tests between changes by rebuilding the container. Split big changes into steps.
- **Never collapse or elide code** when showing or writing files — no "// rest unchanged" placeholders in actual file edits.
- **Don't break working features to modernize them.** Larger refactors (e.g., the roadmap's TypeScript/Svelte migration) happen only as deliberate, planned work.
- **Comments:** the codebase is heavily commented in a tutorial style. Match the surrounding density when editing existing files; keep new comments focused on constraints and non-obvious behavior.
- **Subprocess calls to `audible`/`ffmpeg`:** always register long-running processes with `process_registry` (register/unregister in try/finally) so cancellation works, and set `env["HOME"] = DATABASE_DIR` for audible-cli calls. Treat exit code `-15` (SIGTERM) as cancellation, not failure.
- **Settings:** new settings get a default in `settings.py:DEFAULT_SETTINGS`, are saved via the deep-merge in `POST /api/settings`, and are read with `.get()` chains and fallbacks (users have old `settings.json` files).
- **Secrets:** never log credentials or auth-file passwords; `app.log` is user-downloadable from the UI.

### Feedback Backlog (BACKLOG.md + BACKLOG_ARCHIVE.md)
- `BACKLOG.md` at the repo root is the capture inbox for user feedback, observations, and improvement ideas **not yet scheduled into a release** — UI friction, conversion papercuts, deployment issues, anything. When the user gives feedback like this mid-session (or a `ref-docs/user-reports/` triage surfaces actionable items), record it there rather than losing it in conversation or scattering TODOs.
- **Capturing and closing items is owned by the `backlog` skill** — invoke it rather than editing the two files by hand. It owns the permanent issue numbering, the entry format, and the rule that most often gets half-applied: **two files, split by state** — `BACKLOG.md` holds only open items at full working detail, and a closed item is compacted and *moved* into `BACKLOG_ARCHIVE.md` in the same edit, never left behind, never renumbered.
- Both backlog files are **gitignored and local-only** (like `PLAN.md`/`REVIEW.md`), but unlike those they are persistent and append-oriented — never overwritten by convention: resolved items get their status updated (`planned → vX.Y.Z`, `done`, or `declined` + why), not deleted.
- When planning a new release, sweep `BACKLOG.md` for candidates before writing the plan; items that graduate into a `PLAN.md` get marked accordingly. `ROADMAP.md` defines what each version is — the backlog holds the unscheduled raw material.

### Context hygiene
- After a commit that closes out a coherent unit of work (a completed piece from PLAN.md, a full review-and-fix cycle, a standalone doc edit), explicitly suggest running /clear before starting unrelated work — don't just move on silently.
- Before starting a new Plan Mode discussion on a different topic than what's currently in context, suggest /clear first, unless I've said I want the prior discussion carried forward.
- If a single task is running long and context feels heavy (many file reads, long back-and-forth) but the task isn't finished, suggest /compact with a focus hint naming the current task, rather than /clear.

## Coding Standards & Tooling

- **Activate the venv first — this bites every time it's missed.** Shell state does not persist between commands, so chain `source .venv/bin/activate &&` into *every* Bash invocation that runs Python tooling (pytest, ruff, pip), not just the first one. A `PreToolUse` hook (`.claude/hooks/venv-guard.sh`) enforces this and blocks the un-activated call; genuine system-Python use opts out with a trailing `# no-venv`. Commands run through `docker`/`docker compose` are exempt — they run inside the image.
- **Python:** Ruff (lint + format), config in `pyproject.toml` (line length 120, rules E/W/F/I/UP). Run `ruff check src/` and `ruff format --check src/` before considering a Python change complete.
- **JS/HTML/CSS:** Prettier, config in `.prettierrc.json` (4-space indent, print width 120).
- **Tests:** pytest suite in `tests/`, run on the host (`pip install -r requirements-dev.txt` into the venv, then `pytest`; config in `pyproject.toml` sets `pythonpath = ["src"]`, and `tests/conftest.py` redirects `CONFIG_DIR`/`DATABASE_DIR` to temp dirs). Run `pytest` before considering any task complete, and add regression tests when fixing bugs in testable logic. Runtime behavior is still verified manually via the dev container — and at the UI surface via the `verify` skill.
- **User documentation:** lives in `docs/` (screenshots in `docs/images/`) plus the root `README.md` landing page. Docker Compose is presented as *the* install path; alternatives sit in collapsed sections and developer content stays in `docs/development.md`. Every release plan carries a docs sweep of both README and `docs/` — pages whose described behavior changed get updated, and screenshots invalidated by UI changes get recaptured.
- **Changelog:** maintained in `CHANGELOG.md` (Keep a Changelog format). Add an entry under the `[Unreleased]` heading when completing a feature or fix. **Write it for a user reading release notes, not for the implementer** — plain language, only the main changes, minimal jargon, and no internal identifiers (table/column/function names, file paths, review-finding IDs). Keep breaking changes, but state them plainly. The commit history and `ref-docs/` reports hold the fine-grained detail; the changelog is the readable highlight reel. Preserve the `## [X.Y.Z] - DATE` heading format exactly.
- **Licensing:** `LICENSE.txt` carries attributions (audible-convert.sh, Immich). If code is adapted from another project, add the attribution comment in-file and update `LICENSE.txt`.

## Development Workflow

### Environment
- Development happens on an Ubuntu Server VM over SSH (VS Code remote). The app **only runs inside the container** — paths like `/config`, `/database`, `/data` don't exist on the host, so Python modules can't be meaningfully executed outside Docker.
- Dev testing: `docker compose -f docker-compose.dev.yml up -d --build` (builds from source; `docker-compose.dev.yml` and `docker-compose.override.yml` are local-only and untracked — a tracked `.template` exists for the dev file). Source is baked into the image, so **every** change needs a rebuild to be visible.
- The dev container's volumes hold the real dev library and real Audible auth — never wipe `/database` to get a "clean slate", and remember SYNC/DOWNLOAD jobs hit the real Audible API.
- `docker-compose.yml` is the **user-facing production example** that pulls from GHCR — treat its contents as documentation for end users.

### Verifying at the real UI surface
- Passing tests are not evidence that a UI change works — the suite never touches a container. When frontend or backend work changes what the user sees, drive it end-to-end through the **`verify` skill** rather than improvising a browser session — it owns the rebuild-staleness check, the flows worth driving, and the live-data cautions. It runs **before** the review pass, so its findings land in the reviewed diff.

### Planning before implementation
- For any non-trivial change (new functionality, structural changes, anything touching more than one file), enter Plan Mode and produce a plan before writing code; save it to `PLAN.md` in the project root and wait for explicit approval before implementing.
- **Scoping a release is owned by the `release-plan` skill** (run on Fable, in Plan Mode) — invoke it rather than reconstructing the skeleton. It owns the roadmap check, the backlog sweep, the mandatory Phase 0 opener and close-out final phase, and the two traps: **`PLAN.md` is a single gitignored slot** — never overwrite an in-flight milestone plan with a plan for a small side task, and archive a finished one into its version folder (`ref-docs/reports/vX.Y.Z/vX.Y.Z-plan.md`) before the next plan destroys it; and **every phase must carry writing its report as that phase's final task, with the target path resolved at planning time** — a report that is merely *remembered* at the end of a phase is a report that eventually isn't written.
- Route high-level planning (project scope, phase and version implementation plans) to Fable, per the model-routing rule below.

### Implementing a plan phase
- **Executing one phase of `PLAN.md` is owned by the `implement-phase` skill** — invoke it in a fresh session rather than driving the phase by hand. It reads the phase *and the numbered decisions it cites*, front-loads the phase's tail-end obligations (review tier, backlog closes, report path, docs touched) into a checklist **before any code is written**, then delegates the build, runs `review-pass`, commits, and calls `phase-report`.
- The front-loading is the whole point: the review, the backlog close and the report all fall due at the *end* of a phase, by which time the plan text carrying them is hundreds of lines back in a context full of implementation work. That is the failure mode, not forgetting the rules.

### Operator evaluation sessions
- When a shipping decision turns on **which of several competing approaches the user actually prefers across a body of real samples** — naming templates, chapter-cleanup variants, conversion strategies — run it as a blind evaluation via the **`blind-eval` skill** rather than improvising a comparison. It owns the protocol (pre-registered decision rule, stratified sample, seeded blinding, frozen instrument, resumable ratings) and ships the judging server and analysis, so each session writes only its candidate generation.
- **Scope, so it isn't stretched:** blind-eval is for *systematic* evaluation — many samples × a few arms, aggregate preference as the evidence, blinded because knowing the arm would bias the rating. It is **not** the tool for picking a favorite among a handful of options on one or two examples (two page layouts, two card designs): there is nothing to blind and nothing to aggregate, so just present the options.

### Subagent delegation and model routing
- **Delegate by default.** For any substantial code, analysis, or documentation task, dispatch subagents rather than doing the work inline — this is a standing request, so no per-prompt authorization is needed. Trivial, obviously-scoped work (a one-line edit, a single file read, answering a question already in context) stays inline; delegating it costs more than it saves.
- **Pick the subagent's model by task type** rather than inheriting the default:
  - **Opus** — writing or modifying code, and analysis work (investigation, debugging, code reading, verification).
  - **Sonnet** — document writing and updating (readme, CHANGELOG, BACKLOG, ROADMAP, plan and report write-ups).
  - **Fable** — the heavyweight tier, reserved for two things: high-level planning (overall project planning, phase and version implementation planning) and the dedicated review pass at major checkpoints. Don't draw it for routine code, analysis, or doc work. Note the one deliberate exception: the `adversarial-review` gate that *follows* Fable's review pass runs on **Opus**, because Fable may have written the review being cross-examined.
- **Dispatch in parallel** — one message, multiple tool calls — whenever tasks have no inter-task dependencies. Sequence them only when one subagent's output genuinely feeds another's input.
- **Give subagents the constraints, not just the task** — the standing rules a fresh agent can't infer (the conversion core's fallback chains, process_registry discipline, settings compatibility, schema-in-start.sh, no secrets in logs).
- **Be conservative with token usage:** use the fewest subagents that cover the work, scope each prompt tightly, and have them return conclusions rather than file dumps.

### Review before committing
- Before committing a completed phase/feature, do a dedicated review pass against `PLAN.md` and `CLAUDE.md` — ideally in a fresh session/context (not the implementation session) so the review isn't colored by how the code was built.
- **The review pass is owned by the `review-pass` skill** — invoke it rather than driving the steps by hand. It owns the ordering that matters most: **archive the existing `REVIEW.md` into `ref-docs/reports/` first** (it is gitignored *and* overwritten by convention, so an unarchived review is destroyed by the next one), then clear `ruff`/`pytest`/Prettier so the pass isn't spent on lint-catchable trivia, then review.
- Model strategy for review: route routine/minor review (a single task, a small fix) to Opus. Reserve Fable for major review after big checkpoints (a full phase/feature wrap-up, a significant milestone). Which tier a given review counts as is a judgment call, not a fixed rule.
- The reviewer only reports findings, not fixes — results go to `REVIEW.md` in the project root (overwrite existing; same convention as `PLAN.md`), organized by severity (blocking / worth fixing / minor). After review, address blocking and worth-fixing items; anything the implementer disagrees with is flagged back rather than silently skipped or silently applied. Real findings outside the current scope go to `BACKLOG.md` via the `backlog` skill.
- **At a release, that review is only the first of two passes.** Fable's findings get fixed and committed, then the multi-provider **`adversarial-review`** gate runs on Opus before the tag — a single reviewer's clean verdict is only a *hypothesis*. The full sequence, its ordering constraints, and the release mechanics live in the **`release-closeout` skill** — run it rather than driving the steps by hand.

`PLAN.md` and `REVIEW.md` are working documents, **gitignored and local-only** (like `ref-docs/`) — never commit them.

### Commit strategy
- Commit at natural checkpoints — each coherent, working, reviewable unit — rather than one big commit per feature. Small commits keep rollback useful; the review fix pass gets its own commit so it can be reverted independently.
- Stage the specific tracked files you changed, never the whole tree — no `git add -A`/`git add .`/`git commit -a`, which risk sweeping in ignored working files. A `PreToolUse` hook (`.claude/hooks/git-add-guard.sh`) blocks whole-tree adds, `git add -f`, and anything touching `ref-docs/`.
- **Never push without being explicitly told to.** Commit locally freely; push only on request.
- Commits are authored as **ishbuggy** (the repo's configured git user) only — no Claude co-author trailer, no AI attribution lines in commit messages.

### Release process (only when explicitly requested)
- **The close-out gate is owned by the `release-closeout` skill** — the standing, non-optional final phase of every release plan. It sequences: verification (lint/tests/dev-stack rebuild), the Fable review pass, archival of `REVIEW.md`/`PLAN.md` into the version folder, the `adversarial-review` gate on Opus, then the release mechanics (finalize the `CHANGELOG.md` section, update `ROADMAP.md`, close backlog items, release commit `Release vX.Y.Z: <summary>`).
- The mechanics that matter: pushing the `vX.Y.Z` tag fires `.github/workflows/docker-publish.yml`, which builds the multi-arch image (passing `APP_VERSION` from the tag) and pushes it to `ghcr.io/ishbuggy/audiobookup` — **the tag push is the release**; real users pull the result. A GitHub Release, if wanted, is published by hand from the tag afterwards — title `vX.Y.Z: <short highlight>` and succinct notes condensed from the CHANGELOG section (no em dashes, no emojis, link to the CHANGELOG for detail); the full style spec lives in the `release-closeout` skill.
- Reaching a good stopping point — or even a fully green close-out gate — is **not** permission to tag, push, or publish images. Explicit confirmation is required at the tag step, every time.

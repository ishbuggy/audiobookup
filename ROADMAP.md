# AudioBookup — Roadmap

High-level direction for the project: what's planned, why, and roughly when. This is **context, not a commitment** — priorities shift, and the detailed step-by-step implementation plan for the version currently in flight lives in `PLAN.md` (local-only, gitignored).

- **Detailed step plans:** `PLAN.md` (working document, local-only)
- **Known issues & technical-debt inventory:** `REVIEW.md` (local-only)
- **Shipped history:** `CHANGELOG.md`

---

## Release history & near-term plan

| Version | Theme | Status |
|---|---|---|
| v0.17.0 | Library curation & file management | Shipped (2026-01-21) |
| v0.18.0 | Stability & security hardening (all REVIEW.md findings) | Shipped (2026-07-20) |
| **v0.19.0** | **Backend: correctness fixes + capabilities** | Planned |
| **v0.20.0** | **Frontend: library views, editing, and card actions** | Planned |
| v0.21.0+ | Architectural features (see "Longer horizon") | Ideas |

The v0.19 / v0.20 work is driven primarily by real user feedback (see `ref-docs/user-reports/`). The two releases are split **backend-first, frontend-second** on purpose: several requested UI features (in-place metadata editing, bulk rename, duplicate resolution) need backend columns, APIs, and data guarantees that v0.19 establishes, so v0.20 can build UI against a stable surface rather than moving both at once.

---

## v0.19.0 — Backend correctness & capabilities

Ordered correctness-first (data-loss and "lied about success" bugs) before new capabilities.

- **Duplicate / collision handling for bulk ingestion.** Same author+title across different books (common with public-domain classics) can still error or overwrite during bulk/automated processing. Implements the ASIN-suffix uniqueness strategy from the design discussion and flags collisions in the DB. *(Feedback: bug 1, design doc.)*
- **Truthful download outcomes.** Detect "ghost" books reported successful but absent on disk; verify downloaded duration against Audible's reported length; replace the opaque `"Failed during asset download/preparation."` with the real underlying cause (surface audible-cli stderr). *(Bugs 2, 7; FR3.)*
- **Output asset handling.** Embed cover art into the finished `.m4b`; download and store companion PDFs where Audible ships them. *(Bug 5, FR11.)*
- **Naming improvements.** Optional auto-truncate / strip of long native subtitles on ingestion. *(FR6, backend portion.)*
- **Custom metadata backend.** `custom_title` / `custom_author` (and cover-override) columns plus `/api/book/<asin>/update`. Unblocks the v0.20 editing UI. *(FR7; long-deferred from v0.17.)*
- **Manual file import.** Ingest audiobooks the user already owns as files into the managed library/structure — via **both** dropping files under `/data` and browser upload. *(FR2.)*
- **Job history maintenance.** API to clear old/completed jobs. *(FR10, backend portion.)*
- **Optional lossless / no-re-encode mode.** A power-user setting (default off) to strip DRM only and skip the re-encode, for byte-for-byte-ish originals and faster processing. Additive path alongside the existing conversion pipeline. *(FR12.)*

## v0.20.0 — Frontend library, editing & actions

Consumes the v0.19 backend surface.

- **Card-level download button** — download/re-download from the library card without opening the detail modal. *(FR4.)*
- **Deep-linked status blocks** — clicking a Library Status count on the dashboard opens the grid filtered to those books. *(FR9.)*
- **Clear old jobs** — UI for the v0.19 job-cleanup API. *(FR10.)*
- **Alternate library views** — Plex-style List and Table layouts, with actions exposed inline. *(FR8.)*
- **In-place metadata editing** — edit egregious title/author errors and upload a replacement cover, against the v0.19 API. Deliberately *not* full metadata scraping. *(FR7.)*
- **Bulk rename tools** — clean up ugly native titles and long subtitles in batches. *(FR6, frontend portion.)*
- **Duplicate resolution UI** — surface flagged duplicates and let the user choose the final naming convention (narrator, year, or leave the ASIN suffix). *(Design doc.)*
- **Large-library import warning** — warn that the always-re-encode design means large imports take a while. *(FR13.)*

---

## Longer horizon (v0.21.0+ / ideas)

Deliberately deferred from v0.19–v0.20 because they're architectural, low-priority, or need investigation rather than a known fix:

- **Multiple Audible accounts / profiles** — concurrent auth chains, per-account libraries. Significant architecture change. *(FR5; also a standing "future idea.")*
- **Faster / event-driven library sync** — closer to real-time without hammering the API. Manual sync already exists as a workaround, so this is an enhancement, not a fix. *(FR1.)*
- **Chapter-timestamp parity with Libation** — timestamps drift a few seconds per chapter vs. Libation. Touches the load-bearing `chunked_conversion_logic.py` core, so it needs a careful investigation (not a casual change) before it's schedulable. *(Bug 6.)*
- **Libation settings-parity review** — walk through Libation's options for features worth adopting; a research task that feeds future roadmap items. *(FR14.)*
- **Frontend framework migration** — TypeScript and/or Svelte, as deliberate planned work, not a drive-by refactor.

---

## Reference

Detailed, current implementation steps and their status are tracked in `PLAN.md`. Technical debt and audit findings are in `REVIEW.md`. Both are local-only working documents and are not committed.

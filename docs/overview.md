[← Docs index](README.md)

# Overview

## What AudioBookup does

AudioBookup is a self-hosted, single-user web application that manages, downloads, and converts your personal Audible audiobook library into standard, DRM-free files. It runs as a single Docker container, uses your own Audible account and your own purchases, and gives you a clean web dashboard for keeping a local, organized copy of the books you already own.

You choose an output format on the Settings page — a re-encoded AAC `.m4b` (the default), a lossless "Original" remux, or an `.mp3` — and every converted file keeps its chapters, cover art, and metadata (author, narrator, series, and more) intact. Finished books are written to your output folder in a tidy structure you control, using a naming template built from placeholders like author, title, series, and narrator.

> **Not supported:** Widevine DRM, xHE-AAC, and Spatial Audio titles. These require a Widevine license path that the community command-line tooling AudioBookup builds on can't request, so AudioBookup can't download or convert them — this is a hard limitation of the toolchain, not a missing feature.

## The core workflow

AudioBookup is built around one simple loop: **Sync → Review → Download & Convert → an organized library in your output folder.**

### Sync

A sync brings your AudioBookup database up to date with what's actually in your Audible account (and, optionally, what's actually on disk). There are two modes:

- **Fast Sync (API-only):** Quickly checks Audible for new or changed books. Lightweight, safe to run often.
- **Deep Sync (full scan):** Does everything Fast Sync does, plus scans your local files so the app notices anything that changed outside of AudioBookup — a file moved, renamed, or deleted.

### Review

Once synced, your library dashboard shows every book you own along with its current status. You can search, sort, and filter the grid — for example, to see only books that still need downloading — before deciding what to process next.

### Download & Convert

Selecting books to process (or letting automation do it) starts a background job. AudioBookup downloads each book from Audible, decrypts it, converts it to your chosen output format, embeds chapters and metadata, and writes it to disk — all while showing granular, real-time progress in the job panel. You can safely close the browser; the job keeps running on the server.

### Result

Finished books land in your output folder, organized into a folder/file structure built from your naming template — no manual filing required.

## Library statuses

Every book in your library shows one of four statuses:

- **Downloaded:** The book has been successfully converted and is on disk.
- **New:** The book is in your Audible library but hasn't been downloaded yet.
- **Missing:** The book was downloaded previously, but its file can no longer be found (for example, it was moved or deleted outside the app). A Deep Sync or the Verify Files tool will catch this.
- **Error:** The most recent download or conversion attempt failed. Details are available in the book's detail view and the activity log.

## The automation loop

The whole workflow above can run hands-free. Scheduled Fast Sync, Deep Sync, and Automatic Processing jobs — each on their own independent cron-based schedule — keep your database current and automatically download anything new, without you needing to click through the dashboard yourself. See [configuration.md#scheduled-tasks](configuration.md#scheduled-tasks) for how to set this up.

## Key concepts

A few terms that come up throughout the app and the rest of these docs:

- **Jobs:** AudioBookup runs one job at a time — a Sync, a Download, or a Verify (library-integrity check) — tracked with live progress and kept in a searchable history.
- **Sidecar files:** Optional extra files saved alongside a converted audiobook, sharing its filename: a cover image, a `metadata.json`, a `.cue` chapter sheet, and/or the original undecrypted source file. All are off by default.
- **Advanced Mode:** A single toggle on the Settings page that reveals the full set of expert options — download-quality requests, MP3 encoder tuning, chapter/metadata cleanups, sidecar files, separate folder/file naming templates, and more — without cluttering the page for everyday use.
- **Output formats:** **AAC `.m4b`** re-encodes for precision and universal player compatibility; **Original** is a lossless remux with no re-encoding step; **MP3** encodes a single file per book, readable by effectively any player.

## Where to go next

- [installation.md](installation.md) — get the container running.
- [setup.md](setup.md) — first-time login and connecting your Audible account.
- [usage.md](usage.md) — day-to-day use of the dashboard.
- [configuration.md](configuration.md) — every setting, explained.

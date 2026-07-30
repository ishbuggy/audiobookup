<p align="center">
  <img src="src/static/img/AudioBookup_Icon.png" width="250" title="AudioBookup Logo">
</p>
<h1 align="center">AudioBookup</h1>
<h3 align="center">A self-hosted application with a modern web interface for managing and downloading your personal Audible audiobook library.</h3>

<br/>

<p align="center">
  <img src="docs/images/dashboard-grid.png" title="AudioBookup Dashboard">
</p>

AudioBookup is a self-hosted, single-container web app that syncs your personal Audible library, downloads what you already own, and converts it into standard, DRM-free audiobook files — a re-encoded AAC `.m4b`, a lossless "Original" remux, or an `.mp3`.

Every converted file keeps its chapters, cover art, and metadata intact, and lands in an organized folder structure you control, ready for players like Plex, Audiobookshelf, or Apple Books. It runs entirely on your own hardware against your own Audible account — your library data and credentials never leave your server.

> **Not supported:** Widevine DRM, xHE-AAC, and Spatial Audio titles — see [docs/overview.md](docs/overview.md) for why.

---

## Features

- **Library management** — a searchable, sortable, filterable dashboard, with grid, list, and table views plus a detailed per-book view for overriding a book's title and author and uploading your own cover art.

- **DRM-free conversion in three formats** — a re-encoded AAC `.m4b` (frame-perfect chapters, universal player compatibility), a lossless "Original" remux (no re-encoding, least processing time), or a single-pass `.mp3`.

- **Smart download & decrypt fallbacks** — resilient fallback chains for fetching and decrypting Audible's AAX/AAXC formats, so a hiccup in one path doesn't sink the download.

- **Chapter & metadata cleanups** — optional nested-chapter flattening, opening/end-credits merging, title cleanup, custom chapter-title templates, and Audible intro/outro trimming.

- **Flexible naming templates** — organize your library with placeholders like `{author}`, `{title}`, `{series}`, `{narrator}`, `{year}`, and more, with separate folder and file templates for advanced control.

- **Scheduled automation** — independent cron-based schedules for Fast Sync, Deep Sync, and automatic downloading, so the library keeps itself current without manual clicks.

- **Live job progress & history** — granular, real-time progress for every sync/download job, plus a searchable, filterable history of everything that's run.

- **Integrity verification & maintenance** — a built-in file-integrity audit that catches corrupt or truncated downloads, plus one-click cache clearing and Audible reconnection tools.

- **Single Docker container** — one multi-arch image (amd64/arm64), one clean data layout, no external services to run.

- **Secure, themeable interface** — the whole UI sits behind session-based login, with a light/dark theme that remembers your preference.

---

## Quick Start

**Requirements:** Docker and Docker Compose, and an Audible account with some purchased titles.

1. Grab [`docker-compose.yml`](docker-compose.yml) from this repository.

2. Edit it for your system:
   - The three volume paths (config, database, and finished-audiobook storage).
   - `PUID` / `PGID` to match your host user.
   - `TZ` to your local timezone, so scheduled jobs run at the right time.

3. Start the container:

   ```bash
   docker compose up -d
   ```

4. Open `http://<your-server-ip>:13300`, log in with the default credentials (`admin` / `changeme`), then follow the first-time setup to set your own password and connect your Audible account — see [docs/setup.md](docs/setup.md).

Full installation details, environment variables, and updating: [docs/installation.md](docs/installation.md).

---

## Documentation

The full documentation lives in [`docs/`](docs/README.md). New here? Start with the [Overview](docs/overview.md), then follow [Installation](docs/installation.md) → [First-Time Setup](docs/setup.md) → [Using AudioBookup](docs/usage.md).

| Doc | What it answers |
| --- | --- |
| [docs/overview.md](docs/overview.md) | What is AudioBookup, and how does the whole workflow fit together? |
| [docs/installation.md](docs/installation.md) | How do I install and update it with Docker Compose? What do the environment variables and volumes mean? |
| [docs/setup.md](docs/setup.md) | How do I log in for the first time, set my password, and connect AudioBookup to Audible? |
| [docs/usage.md](docs/usage.md) | How do I sync, browse, download, and manage my library day to day? |
| [docs/configuration.md](docs/configuration.md) | What does every setting on the Settings page do, including the advanced options? |
| [docs/troubleshooting.md](docs/troubleshooting.md) | Something isn't working — how do I fix it? How do I do routine upkeep? |
| [docs/development.md](docs/development.md) | How do I build AudioBookup from source, run the dev stack, and use the project tooling? (For contributors.) |

---

## License

AudioBookup is licensed under the GNU Affero General Public License v3.0 — see [LICENSE.txt](LICENSE.txt) for the full text.

It incorporates code adapted from [immich-app/immich](https://github.com/immich-app/immich) (AGPL v3.0) and [audible-convert.sh](https://github.com/jvanbruegge/nix-config/blob/master/scripts/audible-convert.sh) (MIT); full attributions are in `LICENSE.txt`.

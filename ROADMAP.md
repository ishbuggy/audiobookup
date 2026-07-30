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
| v0.19.0 / v0.20.0 | Backend correctness + frontend library views, editing & card actions | Shipped (2026-07-22, as v0.20.0) |
| v0.21.0 | Image hardening (Debian trixie base, GPG-verified gosu, apt upgrade) | Shipped (2026-07-22) |
| v0.22.0 | Libation-parity download & processing options + settings IA | Shipped (2026-07-29) |
| v0.23.0+ | Architectural features (see "Longer horizon") | Ideas |

The v0.19 backend and v0.20 frontend tracks were developed backend-first and released together as **v0.20.0**; **v0.21.0** followed with container-image hardening. **v0.22.0** shipped 2026-07-29, driven by a settings-parity review of Libation (see `ref-docs/reports/v0.22.0/` and `ref-docs/libation/`). A **documentation pass** followed on 2026-07-30 (no version bump; it ships with the next release's notes): the README was split into a screenshot-illustrated `docs/` folder, and a docs sweep is now a standing part of every release plan. The next milestone is not yet scoped; candidates live under "Longer horizon" below.

---

## v0.22.0 — Libation-parity download & processing options (shipped 2026-07-29)

Broadened how much control users have over *how* books are downloaded and processed, toward rough
parity with Libation's option set, and re-organized the Settings page so the larger surface stays
approachable: a simple **Standard** view with the full set revealed by **Advanced Mode**, and
settings re-categorized into clearer groups. Detailed step plan and settings schema live in
`PLAN.md`. Reference material: `ref-docs/reports/v0.22.0/` (Libation settings screenshots) and
`ref-docs/libation/` (docs + source).

- **Settings information-architecture rework.** Re-categorize into Downloading / Audio & Output
  Format / Chapters & Metadata / Sidecar Files / Naming (plus the existing Job, Connection,
  Scheduled Tasks, Authentication groups). Standard mode surfaces an opinionated subset; Advanced
  Mode reveals everything. Surfaces the existing **lossless / no-re-encode** mode in the UI for
  the first time (currently settings-file only).
- **MP3 output with LAME controls.** A third output format alongside AAC `.m4b` and Original /
  Lossless: transcode to MP3 with target quality/bitrate, VBR/CBR, downsample-to-mono, max sample
  rate, and encoder quality.
- **Download-quality request.** Ask Audible for Normal/High/Best via audible-cli — a distinct axis
  from the output re-encode bitrate the two settings were previously conflated into.
- **Sidecar outputs.** Optionally save the cover image, a `metadata.json`, and/or a `.cue` sheet
  alongside the audiobook, and retain the raw AAX/AAXC after decryption.
- **Chapter & metadata cleanups.** Combine nested chapter titles, merge opening/end-credit
  chapters, strip Audible "This is Audible" branding (via `brand_intro/outro_duration_ms`), strip
  "(Unabridged)" from tags, and a chapter-title metadata template.
- **Extended naming placeholders.** Add `{series}`/`{series_part}`/`{year}`/`{language}` to the
  file/folder template (also fixing the currently-advertised-but-unimplemented `{series}` tags),
  with an optional advanced folder/file template split.
- **Explicitly excluded (toolchain-hard-gated):** Widevine DRM, xHE-AAC codec, and Spatial/Atmos
  requests all depend on Libation's own Widevine license fetching, which audible-cli cannot do;
  these will be documented as unsupported rather than attempted.

---

## Longer horizon (v0.23.0+ / ideas)

Deliberately deferred because they're architectural, low-priority, or need investigation rather than a known fix:

- **Split books into per-chapter files.** Libation's "split my books into multiple files by chapter" (+ minimum-file-duration merge). Deferred out of v0.22.0 because it breaks the load-bearing one-book/one-`filepath` model — it ripples through the DB schema, collision handling, output verification, and every library view — so it deserves its own dedicated release.
- **Full Libation `<tag>` naming engine.** The complete formatter + conditional DSL (`<title short[U]>`, `<if series->…<-if series>`, name/date/number formatters). v0.22.0 only extends the simpler `{tag}` template; the full engine is a large parser to build and support.
- **Multiple Audible accounts / profiles** — concurrent auth chains, per-account libraries. Significant architecture change. *(FR5; also a standing "future idea.")*
- **Faster / event-driven library sync** — closer to real-time without hammering the API. Manual sync already exists as a workaround, so this is an enhancement, not a fix. *(FR1.)*
- **Chapter-timestamp parity with Libation** — timestamps drift a few seconds per chapter vs. Libation. Touches the load-bearing `chunked_conversion_logic.py` core, so it needs a careful investigation (not a casual change) before it's schedulable. *(Bug 6.)*
- **Clips / notes / bookmarks export** — Libation downloads annotations (`audible download --annotation`) as CSV/JSON; achievable but out of v0.22.0 scope. *(FR14 follow-on.)*
- **Frontend framework migration** — TypeScript and/or Svelte, as deliberate planned work, not a drive-by refactor.

---

## Reference

Detailed, current implementation steps and their status are tracked in `PLAN.md`. Technical debt and audit findings are in `REVIEW.md`. Both are local-only working documents and are not committed.

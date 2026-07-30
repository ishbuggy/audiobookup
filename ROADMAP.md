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
| v0.23.0 | Quality release: full backlog sweep + clips/notes/bookmarks export | Shipped (2026-07-30) |
| v0.24.0 | Split books into per-chapter files | Planned |
| v0.25.0 | Full Libation `<tag>` naming engine + chapter-timestamp parity | Planned |
| v0.26.0 | Multiple Audible accounts / profiles | Planned |
| v0.27.0 | Faster / event-driven library sync | Planned |

The v0.19 backend and v0.20 frontend tracks were developed backend-first and released together as **v0.20.0**; **v0.21.0** followed with container-image hardening. **v0.22.0** shipped 2026-07-29, driven by a settings-parity review of Libation (see `ref-docs/reports/v0.22.0/` and `ref-docs/libation/`). A **documentation pass** followed on 2026-07-30 (no version bump; it ships with the next release's notes): the README was split into a screenshot-illustrated `docs/` folder, and a docs sweep is now a standing part of every release plan. **v0.23.0** shipped 2026-07-30 (plan and reports archived in `ref-docs/reports/v0.23.0/`); the headline sequence through v0.27.0 was laid out the same day it was scoped — see "Planned headline features" below. **v0.24.0** (per-chapter file splitting) is next up.

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

## v0.23.0 — Quality release: full backlog sweep + clips/notes/bookmarks export (shipped 2026-07-30)

Swept all twelve open backlog items left by the v0.22.0 release reviews and the docs pass —
correctness fixes in the path/sidecar/conversion machinery plus three user-hurting settings/auth
bugs (the ERROR auto-retry loop, the password confirm-field mismatch, the credential hash in the
settings export) — and shipped one modest headline: **clips / notes / bookmarks export** *(FR14
follow-on)*. Annotations are fetched via `audible download --annotation` and saved as a raw-JSON
sidecar (`<book>.annotations.json`), both automatically at download time (opt-in sidecar setting)
and on demand per book from the detail modal. Plan, per-phase reports, and the double release
review (Fable + adversarial multi-provider) are archived in `ref-docs/reports/v0.23.0/`.

---

## Planned headline features (v0.24.0+)

Sequence decided 2026-07-30 — one headline per release. Still context, not commitment: each release
gets its own detailed `PLAN.md` when it opens, and priorities can shift.

- **v0.24.0 — Split books into per-chapter files.** Libation's "split my books into multiple files
  by chapter" (+ minimum-file-duration merge). The big one: it breaks the load-bearing
  one-book/one-`filepath` model — rippling through the DB schema, collision handling, output
  verification, and every library view — which is why it gets its own dedicated release.
- **v0.25.0 — Full Libation `<tag>` naming engine + chapter-timestamp parity.** The complete
  formatter + conditional DSL (`<title short[U]>`, `<if series->…<-if series>`, name/date/number
  formatters) — a large parser to build and support; v0.22.0 only extended the simpler `{tag}`
  template. Paired with the careful investigation of the few-seconds-per-chapter timestamp drift
  vs. Libation, which touches the load-bearing `chunked_conversion_logic.py` core and needs
  investigation, not a casual change. *(Bug 6.)*
- **v0.26.0 — Multiple Audible accounts / profiles.** Concurrent auth chains, per-account
  libraries. Significant architecture change, deliberately sequenced after the storage/path model
  settles post-splitting. *(FR5; also a standing "future idea.")*
- **v0.27.0 — Faster / event-driven library sync.** Closer to real-time without hammering the API.
  Manual sync already exists as a workaround, so this is an enhancement, not a fix. *(FR1.)*

## Unscheduled ideas

- **Frontend framework migration** — TypeScript and/or Svelte, as deliberate planned work once the
  feature roadmap quiets down — not a drive-by refactor, and not slotted to a version.

---

## Reference

Detailed, current implementation steps and their status are tracked in `PLAN.md`. Technical debt and audit findings are in `REVIEW.md`. Both are local-only working documents and are not committed.

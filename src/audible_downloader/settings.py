# audible_downloader/settings.py

import copy
import json
import os
from threading import Lock

from werkzeug.security import generate_password_hash  # type: ignore

# Import the centralized path for the settings file
from audible_downloader import SETTINGS_FILE

# --- Settings Configuration (Centralized) ---
# This module now owns all logic related to loading, saving, and
# managing the application's settings.

DEFAULT_SETTINGS = {
    "username": "admin",
    "password_hash": generate_password_hash("changeme"),
    "initial_setup_complete": False,
    "advanced_mode_enabled": False,
    "job": {
        "download": {
            "max_parallel_downloads": 2,
            "total_processing_cores": 2,
            # A re-download re-derives its output path from the current settings,
            # so a changed output format or naming template lands the new file
            # beside the old one, which then sits untracked forever. When true,
            # that previous file (and its sidecars) is deleted automatically at
            # the end of the conversion; when false — the default — the UI asks
            # per re-download instead.
            "cleanup_stale_files": False,
        }
    },
    "naming": {
        "template": "{author}/{title}/{author} - {title}",
        # When true, drop a trailing "Main Title: Subtitle" subtitle from the
        # {title} used in filenames (e.g. "999: The Extraordinary..." -> "999").
        # Affects filenames only; embedded metadata keeps the full title.
        "truncate_subtitle": False,
        # When true, custom title/author overrides also drive the on-disk
        # file/folder names: new downloads are named from them, and editing a
        # book's metadata renames its existing file. Default off (overrides
        # affect only displayed metadata and embedded tags).
        "apply_custom_to_filenames": False,
        # Stretch (Phase 9) only. When BOTH are non-empty they compose as
        # "<folder_template>/<file_template>" and win over `template`. Empty by
        # default so the single `template` above stays authoritative.
        "folder_template": "",
        "file_template": "",
        # Names the individual files produced when per-chapter splitting is on
        # (v0.24.0). Tags: {title} {ch} {ch_title} — the book title, the 1-based
        # chapter number, and the chapter's own title. Unused while splitting is
        # off, which is the default.
        "chapter_file_template": "{title} - {ch} - {ch_title}",
    },
    "conversion": {
        # Primary output control (Phase 5 consumer). Enum: "original" | "m4b" |
        # "mp3". "original" = DRM-strip + remux Audible's AAC untouched; "m4b" =
        # per-chapter AAC re-encode at `quality`; "mp3" = single-pass LAME encode
        # using the `mp3` block below. Mirrored to/from the legacy `no_reencode`
        # flag via resolve_output_format() so old settings.json files keep working.
        "output_format": "m4b",
        "quality": "High",
        "is_chunked_conversion_enabled": False,
        # Requested download quality tier passed to `audible download --quality`
        # ("best"|"high"|"normal"). Distinct axis from the output encode above:
        # this is what we ask Audible for, `output_format`/`quality` is what we
        # produce locally. Phase 1 consumer.
        "download_quality": "best",
        # Download a companion PDF (booklet/supplementary material) alongside
        # the audiobook when Audible ships one, placed next to the .m4b.
        "download_supplementary_pdf": True,
        # Sidecar outputs written next to the finished audiobook (Phase 2). Each
        # is best-effort and off by default so today's single-file output is
        # unchanged: cover image, curated metadata.json, .cue chapter sheet, and
        # retaining the raw AAX/AAXC (+voucher) that would otherwise be deleted.
        "save_cover_alongside": False,
        "save_metadata_json": False,
        "create_cue_sheet": False,
        "retain_aax": False,
        # Save the listener's own annotations (clips, notes, bookmarks) as a raw
        # Audible JSON sidecar next to the book. Fetched with a separate
        # audible-cli call during download; titles with no annotations simply
        # produce no sidecar, which is the common case and not an error.
        "save_annotations": False,
        # Timestamp stamped onto the finished file (and its sidecars) at finalize
        # time (Phase 9 consumer). Enum: "none" | "release_date" | "purchase_date".
        # "none" is today's behavior — the file keeps its real creation mtime.
        "file_timestamp_source": "none",
        # When true, strip DRM only and keep Audible's original audio: mux
        # chapters/metadata/cover onto the decrypted AAC master with -c copy,
        # skipping the per-chapter re-encode (much faster, no quality loss).
        # Default off preserves the always-re-encode behavior. The `quality`
        # setting above is ignored while this is on (no encode happens). If the
        # fast AAC-copy decrypt fails and a title falls back to FLAC, that one
        # book quietly re-encodes since FLAC can't be copied into an .m4b.
        # KEPT as a derived legacy mirror of output_format == "original".
        "no_reencode": False,
        # LAME/MP3 encode options (Phase 5 consumer). Used only when
        # output_format == "mp3"; ignored otherwise.
        "mp3": {
            # "quality" = VBR via -q:a; "bitrate" = CBR/ABR via -b:a.
            "target": "quality",
            # ffmpeg -q:a 0..9 (0 = best quality, 9 = smallest file).
            "vbr_quality": 2,
            # CBR/ABR target in kbps when target == "bitrate".
            "bitrate_kbps": 128,
            # True = CBR; False = ABR (adds -abr 1).
            "constant_bitrate": False,
            # When target == "bitrate": derive kbps from the source master rather
            # than the fixed bitrate_kbps above.
            "match_source_bitrate": True,
            # Force mono (adds -ac 1).
            "downsample_mono": False,
            # Cap the sample rate (adds -ar N only when the source exceeds it).
            "max_sample_rate": 44100,
            # LAME effort: High|Standard|Fast -> -compression_level 0|2|7.
            "encoder_quality": "High",
        },
        # Chapter/metadata processing (Phase 4 & 6 consumers). All default off/
        # identity so the shipped chapter output is byte-for-byte unchanged.
        "chapters": {
            # Flatten nested chapter trees, joining parent/child titles with ": ".
            "combine_nested_titles": False,
            # Fold Opening/End Credits chapters into their neighbors.
            "merge_credit_chapters": False,
            # Trim Audible brand intro/outro (AAC/MP3 re-encodes only, never
            # Original remux).
            "strip_audible_branding": False,
            # Drop "(Unabridged)" from the title/album tags.
            "strip_unabridged": False,
            # Per-chapter title template: {ch} {ch_total} {ch_title} {title}.
            # The default reproduces today's output exactly.
            "chapter_title_template": "{ch_title}",
            # Split the finished book into one output file PER CHAPTER instead of
            # a single audiobook file (v0.24.0). Default off keeps today's
            # single-file output; read by the conversion pipeline's split gate,
            # which also declines to split an auto-chunked book.
            "split_by_chapter": False,
            # Minimum length, IN SECONDS, of a per-chapter output file. A chapter
            # shorter than this is merged forward into the chapter that follows
            # it, so splitting can't emit a pile of two-second fragments. 0
            # disables the merge entirely (every chapter gets its own file).
            # Only meaningful while split_by_chapter is on.
            "minimum_file_duration": 3,
        },
    },
    "import": {
        # Upper bound (in GB) on a single manual-import upload. Enforced as the
        # request body streams to disk, so an oversize upload is rejected without
        # buffering it in memory.
        "max_upload_gb": 2,
    },
    "tasks": {
        "timezone": "UTC",
        "audible_auth_check_interval_hours": 6,
        # Rename for clarity: This is the "Fast" (API-only) sync.
        "is_auto_fast_sync_enabled": False,
        "fast_sync_schedule": {
            "cron": "0 */4 * * *",  # Default: Run every 4 hours.
        },
        # Add a new, separate schedule for the "Deep" (full filesystem scan) sync.
        "is_auto_deep_sync_enabled": False,
        "deep_sync_schedule": {
            "cron": "0 3 * * *",  # Default: Run once a day at 3:00 AM
        },
        "is_auto_process_enabled": False,
        "process_schedule": {
            "cron": "0 4 * * *",  # Default: Run once a day at 4:00 AM
        },
        "auto_process_new": True,
        "auto_process_missing": True,
        "auto_process_error": False,
        "process_new_on_sync": True,
    },
}
settings_lock = Lock()


def deep_update(source, overrides):
    """Recursively update a dictionary."""
    for key, value in overrides.items():
        if isinstance(value, dict) and key in source:
            source[key] = deep_update(source.get(key, {}), value)
        else:
            source[key] = value
    return source


def normalize_output_format(settings, loaded_settings):
    """Back-fill conversion.output_format from the legacy no_reencode flag.

    Old settings.json files predate the output_format enum and only carry
    no_reencode. When the *raw loaded file* has no output_format key but does
    carry no_reencode, that flag is the only statement of the user's intent:
    truthy means "keep Audible's original audio" ("original"), falsy means
    "convert it" ("m4b", the pre-enum default). We inspect loaded_settings (the
    raw file) rather than the merged dict, because the merged dict always
    carries the default "m4b" and would hide the omission.

    Used both when loading settings.json and — with the same dict passed as both
    arguments — on an uploaded settings file POSTed to /api/settings, which is
    the same legacy shape arriving through a different door.
    """
    conv_loaded = loaded_settings.get("conversion", {})
    if not isinstance(conv_loaded, dict):
        return
    conv_target = settings.get("conversion")
    if not isinstance(conv_target, dict):
        return
    if "output_format" not in conv_loaded and "no_reencode" in conv_loaded:
        conv_target["output_format"] = "original" if conv_loaded.get("no_reencode") else "m4b"


def resolve_output_format(settings):
    """The one place that decides the output format, honoring the legacy flag."""
    # `or {}` rather than a plain default: a hand-edited file can carry an
    # explicit "conversion": null, which .get() would hand straight back.
    conv = settings.get("conversion") or {}
    fmt = conv.get("output_format")
    if fmt in ("original", "m4b", "mp3"):
        return fmt
    return "original" if conv.get("no_reencode") else "m4b"


def load_settings():
    """Securely loads settings from settings.json, falling back to defaults."""
    # Always hand out a deep copy: callers mutate the returned dict (e.g. the
    # settings API merges request data into it), and a shallow .copy() shares
    # the nested dicts with DEFAULT_SETTINGS, letting those edits corrupt the
    # defaults for the rest of the process lifetime.
    if not os.path.exists(SETTINGS_FILE):
        return copy.deepcopy(DEFAULT_SETTINGS)
    with settings_lock:
        try:
            with open(SETTINGS_FILE) as f:
                loaded_settings = json.load(f)
            # Start with defaults and layer the loaded settings on top
            # to ensure all keys are present.
            settings = copy.deepcopy(DEFAULT_SETTINGS)
            deep_update(settings, loaded_settings)
            normalize_output_format(settings, loaded_settings)
            return settings
        except (OSError, json.JSONDecodeError) as e:
            print(f"Error loading settings.json: {e}. Using default settings.")
            return copy.deepcopy(DEFAULT_SETTINGS)


def save_settings(settings_dict):
    """Performs a safe save of the settings dictionary to settings.json."""
    with settings_lock:
        temp_file = SETTINGS_FILE + ".tmp"
        try:
            with open(temp_file, "w") as f:
                json.dump(settings_dict, f, indent=4)
            # Use os.rename for an atomic operation to prevent corruption
            os.rename(temp_file, SETTINGS_FILE)
            return True
        except (OSError, TypeError) as e:
            print(f"Error saving settings: {e}")
            return False

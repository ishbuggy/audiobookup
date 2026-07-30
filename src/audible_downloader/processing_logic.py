# audible_downloader/processing_logic.py

# --- Attribution ---
# The logic for determining the final sanitized filename is adapted from
# the work of Jan van Brügge in the original audible-convert.sh script.
# Original Source: https://github.com/jvanbruegge/nix-config/blob/master/scripts/audible-convert.sh
# License: MIT (included in the project's LICENSE.txt file)
# --- End Attribution ---

import json
import os
import re
import shutil
import subprocess
import tempfile
import time
from datetime import datetime
from threading import Event, Lock

from . import TEMP_DIR
from .chapter_transforms import strip_unabridged

# Import the task-oriented functions and the global announcer
from .chunked_conversion_logic import (
    _yield_progress,
    encode_book_mp3,
    encode_chapter_chunk,
    merge_book_chunks,
    prepare_book_assets,
    remux_book_lossless,
)
from .db import get_db_connection
from .eta_estimator import estimate_conversion_time, record_conversion_time
from .logger import log
from .process_registry import process_registry
from .settings import load_settings, resolve_output_format

# Import the task runner and task objects
from .task_runner import Task, TaskPriority, task_runner

# Output names claimed by in-flight books, guarded by a lock. The on-disk/DB
# collision check only sees files that already exist; in a bulk job two
# different books with the same author+title both run PREPARE before either
# has written its file, so without this the loser's merge would silently
# overwrite the winner. Each book reserves its chosen name here for the
# duration of its run and releases it when finished.
#
# The currency is the EXTENSION-STRIPPED base, not the full path: every sidecar
# below hangs off that base, so "/data/X/T.m4b" and "/data/X/T.mp3" are the same
# claim even though the audio files differ — treating them as distinct would let
# one book's .pdf/.cue/.metadata.json silently overwrite the other's.
_reserved_output_paths: set[str] = set()
_reservation_lock = Lock()

# A finished .m4b is far larger than this floor; it only catches an absent or
# empty/stub output — the "ghost book" that reported success but isn't on disk.
_MIN_OUTPUT_BYTES = 64 * 1024

# Floor for the "don't wait forever" completion timeout: short books and books
# with no known runtime get two hours regardless of any model below.
_COMPLETION_TIMEOUT_FLOOR_SEC = 7200

# The MP3 path's completion budget, as a multiple of the book's own runtime.
# Three times real time comfortably covers the download, the decrypt and a
# single-pass LAME encode even on slow hardware; see _completion_timeout for
# why that path cannot use the AAC estimator's rate.
_MP3_TIMEOUT_RUNTIME_MULTIPLE = 3

# Every file that can end up sharing a finished audiobook's base name: the
# companion PDF, cover image, cue sheet, metadata JSON, and a retained raw
# master (+ its voucher). Anything that follows the audiobook — a rename moving
# it, a timestamp stamp — walks this list so the two can't drift apart.
_SIDECAR_SUFFIXES = (".pdf", ".jpg", ".png", ".cue", ".metadata.json", ".aax", ".aaxc", ".voucher")

# Every audio extension a tracked book's file can carry. Two books at the same
# base under DIFFERENT audio extensions share one set of sidecars, so any of
# these occupying our base is a collision — not just the format we happen to be
# writing. ".m4a" is in the list because import_logic keeps an upload's real
# container extension (see IMPORTABLE_EXTS), so real libraries contain .m4a
# books, and rename_book_to_match_metadata preserves whatever it finds on disk.
_AUDIO_EXTENSIONS = (".m4b", ".mp3", ".m4a")


def _sibling_audio_paths(base, ext):
    """
    Every path that would share the sidecar base `base` under an audio extension
    OTHER than `ext`. An unrecognized `ext` (not one of _AUDIO_EXTENSIONS) yields
    all of them, so an unusual container still gets the full check.
    """
    return [f"{base}{other}" for other in _AUDIO_EXTENSIONS if other.lower() != ext.lower()]


def _existing_sidecar_suffixes(base):
    """
    Every sidecar suffix that actually exists on disk for the extension-stripped
    `base`, each in the spelling the file itself uses (so a caller can move or
    delete it verbatim). Sorted, so logs and moves are deterministic.

    _SIDECAR_SUFFIXES is lowercase but the files are not always: the cover keeps
    whatever extension Audible handed us (".JPG" happens), and a user can drop a
    hand-made "Book.PDF" beside a book. Matching only the lowercase spelling left
    those behind — a rename moved the audiobook and orphaned its cover.

    Two passes, deliberately: the exact lowercase names are probed directly, which
    keeps the common case a handful of stats, and then the containing directory is
    listed to catch any other casing. A listing that fails (the directory is gone,
    or unreadable) just leaves the direct probes standing.
    """
    found = {suffix for suffix in _SIDECAR_SUFFIXES if os.path.exists(f"{base}{suffix}")}

    directory = os.path.dirname(base)
    prefix = os.path.basename(base)
    try:
        entries = os.listdir(directory or ".")
    except OSError:
        entries = []
    for entry in entries:
        # The base name itself is matched exactly (it comes from our own tracked
        # path); only the suffix is case-insensitive. The exact-suffix membership
        # test is what keeps "Title 2.jpg" from being read as Title's sidecar.
        if not entry.startswith(prefix):
            continue
        suffix = entry[len(prefix) :]
        if suffix.lower() in _SIDECAR_SUFFIXES:
            found.add(suffix)

    return sorted(found)


def _probe_duration_seconds(filepath, job_id=None):
    """Return the media duration of `filepath` in seconds via ffprobe, or None
    if it can't be determined (missing or unreadable/corrupt file). Registered
    with process_registry (house rule) so a job cancel can SIGTERM the probe;
    job_id may be None, in which case register/unregister are no-ops."""
    cmd = [
        "ffprobe",
        "-v",
        "error",
        "-show_entries",
        "format=duration",
        "-of",
        "default=noprint_wrappers=1:nokey=1",
        filepath,
    ]
    try:
        process = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        process_registry.register(job_id, process)
        try:
            stdout, _stderr = process.communicate()
        finally:
            process_registry.unregister(job_id, process)
    except OSError:
        return None
    if process.returncode != 0:
        return None
    try:
        return float(stdout.strip())
    except ValueError:
        return None


def _sanitize_filename(name):
    """Sanitizes a string to be used as a valid filename."""
    name = re.sub(r'[\\/:\*\?"<>|]', "_", name)
    name = name.strip(" .")
    name = re.sub(r"\s+", " ", name).strip()
    return name


def _strip_subtitle(title):
    """
    Drop a trailing "Main Title: Subtitle" subtitle for cleaner filenames, e.g.
    "999: The Extraordinary Young Women..." -> "999". Splits on the first
    colon-space, so ratios/times like "12:00" are left intact. Returns the
    original title unchanged when there's no subtitle or when stripping would
    leave nothing. Must run BEFORE _sanitize_filename, which rewrites ':' to '_'.
    """
    if not title:
        return title
    main = title.split(": ", 1)[0].strip()
    return main or title


def _resolve_optional_tag(value):
    """
    Missing-value rule for the *new* naming placeholders ({series}, {series_part},
    {language}). Sync stores the literal string "N/A" when Audible omits a field,
    so None, "", and "N/A" all count as missing and render as the empty string;
    a present value is sanitized like every other tag. The existing five tags
    keep their "Unknown ..." fallbacks and do NOT go through here.
    """
    if value is None:
        return ""
    text = str(value).strip()
    if text == "" or text == "N/A":
        return ""
    return _sanitize_filename(text)


def _parse_timestamp_date(value):
    """
    Parse one of Audible's date fields into epoch seconds for os.utime, or None
    when it can't be used. Only the leading "YYYY-MM-DD" is read, which covers
    both shapes the API returns: `release_date` is already a bare date, while
    `purchase_date` is a full ISO timestamp ("2023-04-05T06:07:08.000Z"). Sync's
    "N/A" placeholder and anything unparseable return None so the caller can skip
    silently rather than stamping a bogus time.
    """
    if not value:
        return None
    text = str(value)[:10]
    if text == "N/A":
        return None
    try:
        return datetime.strptime(text, "%Y-%m-%d").timestamp()
    except (ValueError, OverflowError, OSError):
        return None


def build_base_output_path(
    settings,
    asin,
    author,
    title,
    narrator,
    publisher,
    ext=".m4b",
    series=None,
    series_sequence=None,
    release_date=None,
    language=None,
):
    """
    Compute the base output path (`/data/.../Name.m4b`) for a book from the
    naming template and settings, applying optional subtitle trimming and
    filename sanitization. Collision handling is layered on top by the caller.
    The author/title passed in are already the effective values (native or the
    custom override) — this function does not decide which to use.

    `ext` defaults to '.m4b' (every conversion output); the import path passes the
    uploaded file's real extension so an adopted `.m4a` keeps its true extension
    instead of being mislabeled `.m4b`.

    Placeholders: the original {author} {title} {narrator} {publisher} {asin} keep
    their "Unknown ..." fallbacks; the new {series} {series_part} {year} {language}
    render empty when the value is missing (None/""/"N/A"). After substitution the
    rendered path is cleaned segment-by-segment so a missing tag drops its folder
    level cleanly instead of leaving an "N/A" or dangling-separator directory.

    Template composition: `naming.folder_template` and `naming.file_template` are
    an optional split of the single `naming.template`. They take effect only when
    BOTH are non-empty, in which case the effective template is
    "<folder_template>/<file_template>" and `naming.template` is ignored. Any other
    combination (one side blank, both blank — the shipped default) falls back to
    `naming.template`, so a default install renders exactly as it always has. The
    composed string then goes through the same substitution and cleanup below.
    """
    naming = settings.get("naming", {})
    template = naming.get("template", "{author}/{title}/{author} - {title}")

    # The split pair overrides `template` only as a complete pair; a half-filled
    # split would otherwise silently drop the user's folders or filename.
    folder_template = (naming.get("folder_template", "") or "").strip()
    file_template = (naming.get("file_template", "") or "").strip()
    if folder_template and file_template:
        template = f"{folder_template}/{file_template}"

    # Trim a long subtitle before sanitization (which rewrites the ':' separator).
    raw_title = title or "Unknown Title"
    if naming.get("truncate_subtitle", False):
        raw_title = _strip_subtitle(raw_title)

    # Existing tags: always present, always sanitized, "Unknown ..." on missing.
    author_val = _sanitize_filename(author or "Unknown Author")
    title_val = _sanitize_filename(raw_title)

    # {year}: first four characters of release_date, but only when they are all
    # digits (sync's "N/A" fallback and malformed dates render empty).
    year = ""
    if release_date:
        candidate = str(release_date)[:4]
        if len(candidate) == 4 and candidate.isdigit():
            year = candidate

    relative_path = (
        template.replace("{author}", author_val)
        .replace("{title}", title_val)
        .replace("{narrator}", _sanitize_filename(narrator or "Unknown Narrator"))
        .replace("{publisher}", _sanitize_filename(publisher or "Unknown Publisher"))
        .replace("{asin}", _sanitize_filename(asin))
        .replace("{series}", _resolve_optional_tag(series))
        .replace("{series_part}", _resolve_optional_tag(series_sequence))
        .replace("{year}", year)
        .replace("{language}", _resolve_optional_tag(language))
    )

    # Drop-segment cleanup. Split on '/'; the last segment is the filename and the
    # rest are directory levels. For each segment collapse whitespace runs and
    # strip leading/trailing spaces, dots, hyphens, underscores, and commas — so
    # "Author - " left by a missing trailing tag becomes "Author". Directory
    # segments that collapse to empty are dropped entirely (no "N/A" folders); if
    # the filename segment collapses to empty, fall back to "<author> - <title>".
    segments = relative_path.split("/")
    filename = segments[-1]
    directories = segments[:-1]

    cleaned_dirs = []
    for seg in directories:
        seg = re.sub(r"\s+", " ", seg).strip(" .-_,")
        if seg:
            cleaned_dirs.append(seg)

    filename = re.sub(r"\s+", " ", filename).strip(" .-_,")
    if not filename:
        # The fallback's own halves can be empty too: a value of " . " or "..." is
        # truthy (so it never took the "Unknown ..." branch above) but sanitizes
        # away to nothing, which used to leave the file literally named " - ".
        # Re-apply the same fallbacks the tags use so the name always says something.
        filename = f"{author_val or 'Unknown Author'} - {title_val or 'Unknown Title'}"

    relative_path = os.path.join(*cleaned_dirs, filename) if cleaned_dirs else filename
    return os.path.join("/data", f"{relative_path}{ext}")


def _cleanup_empty_dirs(directory):
    """Remove `directory` and any now-empty parents up to (not including)
    /data. Best-effort: stops at the first non-empty or unremovable dir."""
    data_root = os.path.abspath("/data")
    directory = os.path.abspath(directory)
    while directory.startswith(data_root + os.sep) and directory != data_root:
        try:
            os.rmdir(directory)  # only succeeds when empty
        except OSError:
            break
        directory = os.path.dirname(directory)


def rename_book_to_match_metadata(asin):
    """
    When the apply_custom_to_filenames setting is on, rename a downloaded book's
    file (and its companion PDF) to match its current effective metadata.

    Returns the new path if a rename happened, else None. Collision-safe (never
    overwrites a different book — it appends the ASIN instead), and best-effort:
    any problem is logged rather than raised, so the metadata edit still stands.
    """
    settings = load_settings()
    if not settings.get("naming", {}).get("apply_custom_to_filenames", False):
        return None

    with get_db_connection() as con:
        row = con.execute(
            "SELECT author, title, narrator, publisher, custom_title, custom_author, filepath, status, "
            "series, series_sequence, release_date, language "
            "FROM audiobooks WHERE asin = ?",
            (asin,),
        ).fetchone()
    if not row or row["status"] != "DOWNLOADED" or not row["filepath"]:
        return None

    current_path = row["filepath"]
    if not os.path.exists(current_path):
        log.warning(f"RENAME ({asin}): Tracked file '{current_path}' is missing; skipping rename.")
        return None

    author = row["custom_author"] or row["author"] or "Unknown Author"
    title = row["custom_title"] or row["title"] or "Unknown Title"
    # Preserve the file's real extension (an MP3 book must stay ".mp3", not be
    # relabeled ".m4b" by build_base_output_path's default).
    ext = os.path.splitext(current_path)[1] or ".m4b"
    target = build_base_output_path(
        settings,
        asin,
        author,
        title,
        row["narrator"],
        row["publisher"],
        ext=ext,
        series=row["series"],
        series_sequence=row["series_sequence"],
        release_date=row["release_date"],
        language=row["language"],
    )

    if os.path.abspath(target) == os.path.abspath(current_path):
        return None  # Name already matches.

    # Collision-safe: if the target name is taken by a different book, suffix the
    # ASIN. "Taken" is judged on the extension-stripped base, not the full path,
    # because the sidecars hang off that base — a foreign book sitting at the same
    # base under ANY other audio extension would have its .pdf/.cue/.metadata.json
    # overwritten by ours, so every sibling extension is probed. Filesystem and DB
    # reads happen outside the reservation lock (same discipline as
    # _reserve_output_path).
    target_root, target_ext = os.path.splitext(target)
    occupied_candidates = [target] + _sibling_audio_paths(target_root, target_ext)

    collision = False
    for candidate in occupied_candidates:
        if not os.path.exists(candidate):
            continue
        with get_db_connection() as con:
            other = con.execute("SELECT asin FROM audiobooks WHERE filepath = ?", (candidate,)).fetchone()
        if not other or other["asin"] != asin:
            collision = True
            break

    # An in-flight DOWNLOAD reserves its output base at PREPARE time, long before
    # the file exists on disk, so neither check above can see it — a metadata edit
    # made mid-job would otherwise move this book onto a name another book is
    # about to write. Check the reservation set and claim our own (possibly
    # suffixed) base atomically, then hold that claim across the move so the race
    # can't run the other way either.
    with _reservation_lock:
        if target_root in _reserved_output_paths:
            log.info(f"RENAME ({asin}): Target name is claimed by an in-flight book. Appending unique ID.")
            collision = True
        if collision:
            target = f"{target_root}_{_sanitize_filename(asin)}{target_ext}"
            target_root = os.path.splitext(target)[0]
        reserved_base = target_root
        _reserved_output_paths.add(reserved_base)

    try:
        os.makedirs(os.path.dirname(target), exist_ok=True)
        shutil.move(current_path, target)
        # Move every sidecar sharing the old base name alongside the audiobook,
        # so a rename keeps the companion PDF, cover, cue sheet, metadata JSON,
        # and any retained raw master (+voucher) matched to the new file name.
        # Each sidecar keeps its own extension spelling (an uppercase ".JPG" stays
        # uppercase); only the base name changes.
        old_base = os.path.splitext(current_path)[0]
        new_base = os.path.splitext(target)[0]
        for suffix in _existing_sidecar_suffixes(old_base):
            old_sidecar = f"{old_base}{suffix}"
            try:
                shutil.move(old_sidecar, f"{new_base}{suffix}")
            except OSError as e:
                log.warning(f"RENAME ({asin}): Could not move sidecar '{old_sidecar}': {e}")
        with get_db_connection() as con:
            # is_duplicate is written explicitly, exactly as the download path's
            # _finalize_success writes it: `collision` is the live answer to "did
            # this name need the ASIN suffix", so a rename onto a taken name flags
            # the book and a rename onto a free one clears a stale flag.
            con.execute(
                "UPDATE audiobooks SET filepath = ?, is_duplicate = ? WHERE asin = ?",
                (target, int(collision), asin),
            )
            con.commit()
        log.info(f"RENAME ({asin}): Moved file to '{target}'.")
        _cleanup_empty_dirs(os.path.dirname(current_path))
        return target
    except (OSError, ValueError) as e:
        # ValueError as well as OSError: a control character (a NUL byte in a
        # custom title survives _sanitize_filename) makes os.makedirs raise
        # "embedded null byte", and this is called after the metadata edit has
        # already been committed — it must never escape as a 500.
        log.warning(f"RENAME ({asin}): Could not rename file(s): {e}")
        return None
    finally:
        # The claim only had to survive the move and the DB update; release it
        # either way so the name is available again immediately.
        with _reservation_lock:
            _reserved_output_paths.discard(reserved_base)


def build_metadata_json(book_info, title_override=None):
    """
    Curated, JSON-serializable subset of Audible's API `item` for the optional
    metadata.json sidecar. Pure (no I/O) so it's unit-testable without a
    processor. Every field is pulled defensively with .get() because the API
    omits keys for some titles; missing values become None (or an empty list).

    The `description` cleanup mirrors the FFMETADATA writer in
    chunked_conversion_logic.prepare_book_assets so the sidecar text matches the
    embedded tag. `title_override`, when given, does the same for the title: the
    caller resolves the effective title (custom title / "(Unabridged)" cleanup)
    exactly as that writer does and passes the result in. None means "no
    override" — the API title is used verbatim.
    """
    book_info = book_info or {}

    authors = [a.get("name") for a in book_info.get("authors") or [] if a.get("name")]
    narrators = [n.get("name") for n in book_info.get("narrators") or [] if n.get("name")]

    series = None
    series_list = book_info.get("series")
    if series_list:
        first = series_list[0]
        series = {"title": first.get("title"), "sequence": first.get("sequence")}

    # Last (most specific) rung of each category ladder, dropping empties.
    genres = []
    for ladder in book_info.get("category_ladders") or []:
        rungs = ladder.get("ladder")
        if rungs:
            name = rungs[-1].get("name")
            if name:
                genres.append(name)

    description = (
        (book_info.get("merchandising_summary") or "")
        .replace("</p>", "\n")
        .replace("<p>", "")
        .replace("<br />", "\n")
        .strip()
    )

    return {
        "asin": book_info.get("asin"),
        "title": book_info.get("title") if title_override is None else title_override,
        "subtitle": book_info.get("subtitle"),
        "authors": authors,
        "narrators": narrators,
        "series": series,
        "release_date": book_info.get("release_date"),
        "purchase_date": book_info.get("purchase_date"),
        "publisher": book_info.get("publisher_name"),
        "language": book_info.get("language"),
        "genres": genres,
        "runtime_length_min": book_info.get("runtime_length_min"),
        "description": description,
        "copyright": book_info.get("copy_right"),
    }


def generate_cue_sheet(chapters, audio_filename, title, author, asin):
    """
    Render a .cue sheet for the finished audiobook from its (post-transform,
    output-timeline) chapter list. Pure so it's unit-testable.

    CUE INDEX times are MM:SS:FF, where FF counts 1/75-second frames. MM is the
    total minute count and may exceed 99 on long books; SS is 0..59; FF is
    derived from the millisecond remainder. Track numbers are zero-padded to at
    least two digits (three past track 99). Embedded double quotes in any quoted
    field are escaped so a stray quote in a title can't break the CUE syntax.
    """

    def _q(text):
        return (text or "").replace('"', '\\"')

    # MP3 output declares FILE ... MP3; every mp4-family container is WAVE.
    ext = os.path.splitext(audio_filename)[1].lower()
    file_type = "MP3" if ext == ".mp3" else "WAVE"

    lines = [
        f"REM ASIN {asin}",
        f'PERFORMER "{_q(author)}"',
        f'TITLE "{_q(title)}"',
        f'FILE "{_q(audio_filename)}" {file_type}',
    ]

    for i, chapter in enumerate(chapters, start=1):
        start_ms = chapter.get("start_offset_ms", 0)
        total_seconds, ms = divmod(int(start_ms), 1000)
        minutes, seconds = divmod(total_seconds, 60)
        frames = int(ms * 75 / 1000)
        lines.append(f"  TRACK {i:02d} AUDIO")
        lines.append(f'    TITLE "{_q(chapter.get("title", "Chapter"))}"')
        lines.append(f"    INDEX 01 {minutes:02d}:{seconds:02d}:{frames:02d}")

    return "\n".join(lines) + "\n"


class BookProcessor:
    """
    Manages the state and task submission for a single book's conversion process.
    This acts as the "General Contractor" for one book.
    """

    def __init__(self, asin, job_id, download_complete_event=None, stop_event=None, cleanup_stale_files=None):
        self.asin = asin
        self.job_id = job_id
        self.download_complete_event = download_complete_event
        # This job's answer to the stale-file prompt: when a re-download lands at a
        # different path than the tracked one, delete the file left behind. THREE
        # states, and the difference matters:
        #   None  -> no answer was given (bulk/card download, scheduled job), so
        #            finalize defers to the saved setting.
        #   False -> the user was asked and DECLINED; the saved setting must not
        #            override that, so nothing is ever deleted.
        #   True  -> the user consented for this job, whatever the setting says.
        self.cleanup_stale_files = cleanup_stale_files
        # The job's cancellation signal. The process_registry kills subprocesses
        # that are already running, but tasks still sitting in the queue would
        # otherwise start fresh work after a cancel — each task checks this
        # event before doing anything.
        self.stop_event = stop_event
        self.temp_dir = None
        self.final_output_path = None
        # Set True when a same-author+title collision forced an ASIN suffix onto
        # our filename; persisted to the DB on success so the UI can flag it.
        self.is_duplicate = False
        # The user's custom title for this book, read once during PREPARE. The
        # sidecar writers need it to match the embedded tags, which always
        # prefer it over the Audible title.
        self.custom_title = None
        self.context = {}
        self.total_chunks = 0
        self.completed_chunks = 0
        self.encoded_chunk_paths = []
        self._lock = Lock()
        self._completion_event = Event()

    def _cancelled(self):
        """
        Returns True (and unblocks `run`) if the job has been cancelled.
        Task functions call this on entry so queued tasks become no-ops after a
        cancel instead of working against a soon-deleted temp dir.
        """
        if self.stop_event is not None and self.stop_event.is_set():
            log.info(f"PROCESSOR ({self.asin}): Job cancelled. Skipping remaining work.")
            self._completion_event.set()
            return True
        return False

    def _probe_file_asin(self, filepath):
        """
        Reads the embedded ASIN tag from an audio file using the same ffprobe
        invocation the deep filesystem scan uses. Returns the ASIN string, or
        None if the tag is absent or the file can't be probed.
        """
        ffprobe_cmd = [
            "ffprobe",
            "-v",
            "quiet",
            "-show_entries",
            "format_tags=asin",
            "-of",
            "default=noprint_wrappers=1:nokey=1",
            filepath,
        ]
        try:
            process = subprocess.Popen(ffprobe_cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
            process_registry.register(self.job_id, process)
            try:
                stdout, stderr = process.communicate()
            finally:
                process_registry.unregister(self.job_id, process)

            if process.returncode != 0:
                # Exit code -15 (SIGTERM) means the job was cancelled — not a real failure.
                if process.returncode != -15:
                    log.warning(f"TASK-PREPARE ({self.asin}): ffprobe failed for '{filepath}': {stderr}")
                return None

            return stdout.strip() or None
        except OSError as e:
            log.warning(f"TASK-PREPARE ({self.asin}): Could not probe '{filepath}': {e}")
            return None

    def _reserve_output_path(self, base_output_path, safe_asin):
        """
        Choose this book's final output path and reserve its base name in-process
        so two books with the same author+title can't both claim it.

        Collision cases, in order:
          1. Another in-flight book has already reserved this base -> rename ours.
          2. A file already exists at the path, or at ANY SIBLING audio extension
             sharing the same base -> keep it only if it verifiably belongs to
             this same book (see _existing_file_is_foreign).
        On collision we inject the ASIN ("Title.m4b" -> "Title_B00XYZ.m4b"),
        which is unique per book, and mark this book as a duplicate.
        """
        # Decide whether an on-disk file at the target forces a collision BEFORE
        # taking the lock: _existing_file_is_foreign may run an ffprobe subprocess
        # (to read an untracked file's embedded ASIN), and holding the global
        # reservation lock across a subprocess would serialize every other book's
        # PREPARE reservation. This check only reads the filesystem/DB, not the
        # in-memory reservation set, so it's safe outside the lock; the atomic
        # check-and-reserve against _reserved_output_paths still happens under it.
        file_is_foreign = os.path.exists(base_output_path) and self._existing_file_is_foreign(base_output_path)

        # The sidecars hang off the extension-stripped base, so a foreign book
        # already occupying the same base under ANY other audio extension is a
        # collision too — both would write the same .pdf/.cue/.metadata.json. Every
        # sibling extension is probed, not just the other output format: an imported
        # ".m4a" book can occupy the base as easily as an ".mp3" one. A sibling
        # belonging to this same ASIN (our own earlier download in the previous
        # format) is NOT foreign and NOT a collision; the stale-file cleanup at
        # finalize time is what removes it.
        base, ext = os.path.splitext(base_output_path)
        sibling_is_foreign = False
        for sibling_path in _sibling_audio_paths(base, ext):
            if os.path.exists(sibling_path) and self._existing_file_is_foreign(sibling_path):
                sibling_is_foreign = True
                break

        with _reservation_lock:
            collision = False
            if base in _reserved_output_paths:
                log.info(
                    f"TASK-PREPARE ({self.asin}): Target name is already claimed by another "
                    f"in-flight book. Appending unique ID."
                )
                collision = True
            elif file_is_foreign or sibling_is_foreign:
                collision = True

            if collision:
                final_path = f"{base}_{safe_asin}{ext}"
            else:
                final_path = base_output_path

            _reserved_output_paths.add(os.path.splitext(final_path)[0])

        self.is_duplicate = collision
        return final_path

    def _existing_file_is_foreign(self, filepath):
        """
        A file already exists at `filepath`. Return True if it belongs to a
        different book (so we must not overwrite it), False if it's safe to
        overwrite as this book's own prior download.

          1. Tracked in the DB under a different ASIN -> foreign.
          2. Tracked under our own ASIN -> ours, overwrite.
          3. Untracked -> probe the embedded ASIN tag; only a matching tag
             proves it's an old copy of this book.
        """
        with get_db_connection() as con:
            existing_entry = con.execute("SELECT asin FROM audiobooks WHERE filepath = ?", (filepath,)).fetchone()

        if existing_entry:
            if existing_entry["asin"] != self.asin:
                log.info(
                    f"TASK-PREPARE ({self.asin}): Filename collision with tracked ASIN "
                    f"{existing_entry['asin']}. Appending unique ID."
                )
                return True
            log.info(f"TASK-PREPARE ({self.asin}): Existing file '{filepath}' belongs to this book; not a collision.")
            return False

        embedded_asin = self._probe_file_asin(filepath)
        if embedded_asin == self.asin:
            log.info(f"TASK-PREPARE ({self.asin}): Untracked file has this book's embedded ASIN tag. Overwriting.")
            return False
        log.info(
            f"TASK-PREPARE ({self.asin}): Untracked file with foreign or missing ASIN tag "
            f"({embedded_asin or 'none'}) occupies the target path. Appending unique ID."
        )
        return True

    def _completion_timeout(self):
        """
        How long `run` waits for this book's final task before giving up. Purely
        a "don't wait forever" backstop, so both branches are deliberately
        generous: killing a healthy conversion is far worse than waiting too long
        on a wedged one.

        Two models, because the two encode paths are not comparable:

        - The chunked AAC re-encode is the ONLY path that feeds the conversion
          estimator (the remux and MP3 paths both finalize with record_eta=False),
          so for that path the historical rate is a real measurement of this
          machine and 4x it is a fair ceiling. Unchanged.
        - MP3 output therefore gets a budget derived from work it never does — a
          parallelized AAC chunk encode. On slow hardware (arm64 SBCs) a
          single-threaded LAME pass over a very long book runs well past that
          ceiling, and the wait would abort an encode that is progressing fine.
          Scale off the book's own runtime instead, which is the one quantity
          that actually bounds a single-pass encode.

        A missing or zero runtime leaves nothing to scale, so both paths fall back
        to the floor.
        """
        with get_db_connection() as con:
            runtime_row = con.execute("SELECT runtime_min FROM audiobooks WHERE asin = ?", (self.asin,)).fetchone()
        runtime_min = runtime_row["runtime_min"] if runtime_row else None
        if not runtime_min or runtime_min <= 0:
            return _COMPLETION_TIMEOUT_FLOOR_SEC

        if resolve_output_format(load_settings()) == "mp3":
            budget = int(runtime_min * 60 * _MP3_TIMEOUT_RUNTIME_MULTIPLE)
        else:
            budget = 4 * estimate_conversion_time(runtime_min)
        return max(_COMPLETION_TIMEOUT_FLOOR_SEC, budget)

    def run(self):
        """Starts the processing for this book and waits for it to complete."""
        try:
            # Create a temporary directory that will be automatically cleaned up
            with tempfile.TemporaryDirectory(prefix=f"{self.asin}_", dir=TEMP_DIR) as temp_dir:
                self.temp_dir = temp_dir
                log.info(f"PROCESSOR ({self.asin}): Created temp dir: {self.temp_dir}")

                # Submit the first task: preparing the book's assets.
                prepare_task = Task(
                    priority=TaskPriority.PREPARE_BOOK,
                    job_id=self.job_id,
                    func=self._prepare_and_spawn_encode_tasks,
                )
                task_runner.submit_task(prepare_task)

                # Block and wait for the final MERGE task to signal completion.
                # The timeout only exists to prevent waiting forever; see
                # _completion_timeout for how long it is and why that differs
                # per output format.
                completed = self._completion_event.wait(timeout=self._completion_timeout())
                if not completed:
                    # Nothing is coming, and the temp dir this book's tasks are
                    # working in is about to be deleted by the context manager
                    # above. Stop the subprocesses first: an ffmpeg left running
                    # would keep burning CPU (and holding the unlinked temp files'
                    # disk space) for hours with nothing to deliver.
                    #
                    # The registry is keyed by job, not by book, so in a bulk job
                    # this can also cut short the NEXT book's in-flight download.
                    # That is the better trade: it fails cleanly and is retried,
                    # whereas an orphaned encode is invisible and unkillable from
                    # the UI.
                    process_registry.kill_job_processes(self.job_id)
                    raise RuntimeError("Processing timed out.")
        except Exception as e:
            log.error(f"PROCESSOR ({self.asin}): A critical error occurred in the processor run: {e}", exc_info=True)
            self._update_db_on_failure(f"A critical error occurred: {e}")
        finally:
            # Release our claimed output name so it is available again (e.g. for
            # a later re-download of this same book). Reservations are keyed by
            # the extension-stripped base, so release the same way.
            if self.final_output_path:
                with _reservation_lock:
                    _reserved_output_paths.discard(os.path.splitext(self.final_output_path)[0])
            log.info(f"PROCESSOR ({self.asin}): Finished run method.")

    def _prepare_and_spawn_encode_tasks(self):
        """The actual function for the PREPARE_BOOK task."""
        if self._cancelled():
            # Unblock the worker's download slot too, or the head-start
            # pipeline in job_manager would keep waiting on it.
            if self.download_complete_event:
                self.download_complete_event.set()
            return
        log.info(f"TASK-PREPARE ({self.asin}): Starting.")
        # --- 1. Fetch book details and determine final path ---
        try:
            settings = load_settings()

            with get_db_connection() as con:
                # Fetch metadata columns for the naming template plus the custom overrides.
                book_details = con.execute(
                    "SELECT author, title, narrator, publisher, custom_title, custom_author, "
                    "series, series_sequence, release_date, language "
                    "FROM audiobooks WHERE asin = ?",
                    (self.asin,),
                ).fetchone()

            if not book_details:
                raise ValueError(f"Could not find ASIN {self.asin} in the database.")

            # Carried to finalize time for the sidecar titles, which follow the
            # embedded tags rather than the filename rules below.
            self.custom_title = book_details["custom_title"]

            # The custom title/author drive the filename only when the user has
            # opted in; otherwise names come from the native Audible values.
            author = book_details["author"] or "Unknown Author"
            title = book_details["title"] or "Unknown Title"
            if settings.get("naming", {}).get("apply_custom_to_filenames", False):
                author = book_details["custom_author"] or author
                title = book_details["custom_title"] or title

            # The output format is the single axis that decides the container:
            # "mp3" writes a .mp3, everything else ("original" remux / "m4b"
            # re-encode) writes a .m4b. resolve_output_format() honors the legacy
            # no_reencode flag, so old settings.json files still route correctly.
            fmt = resolve_output_format(settings)
            ext = ".mp3" if fmt == "mp3" else ".m4b"

            base_output_path = build_base_output_path(
                settings,
                self.asin,
                author,
                title,
                book_details["narrator"],
                book_details["publisher"],
                ext=ext,
                series=book_details["series"],
                series_sequence=book_details["series_sequence"],
                release_date=book_details["release_date"],
                language=book_details["language"],
            )

            # Collision Detection Logic ("The Dracula Problem"). Reserve a
            # unique output path, guarding against both files already on disk
            # and other in-flight books racing for the same name.
            self.final_output_path = self._reserve_output_path(base_output_path, _sanitize_filename(self.asin))

            os.makedirs(os.path.dirname(self.final_output_path), exist_ok=True)
        except Exception as e:
            log.error(f"TASK-PREPARE ({self.asin}): Failed to get details or create path: {e}")
            self._update_db_on_failure("Failed to prepare file path.")
            self._completion_event.set()
            return

        # --- 2. Call the asset preparation logic ---
        # Only the "original" (lossless remux) format skips the synthetic
        # single-chapter auto-chunking, since that chunking exists purely to
        # parallelize the re-encode it doesn't do. AAC and MP3 both re-encode, so
        # they keep the chunking. (MP3 encodes in a single pass but still wants
        # the synthetic "Part N" navigation markers on a chapterless title.)
        lossless = fmt == "original"
        self.context, prepare_error = prepare_book_assets(self.asin, self.job_id, self.temp_dir, lossless=lossless)

        # Signal that the download/prepare phase is complete.
        # This will unblock the main worker in job_manager.py, allowing it
        # to start the next book's download.
        if self.download_complete_event:
            self.download_complete_event.set()

        if not self.context:
            # prepare_book_assets returns (None, reason) on a genuine failure and
            # (None, None) on cancellation. Only a real failure marks the book
            # ERROR (with the underlying cause, e.g. audible-cli reporting a title
            # is no longer available). A cancel leaves the book's status untouched
            # — it stays NEW/MISSING and is retried — instead of stranding it in
            # ERROR with a misleading message.
            if prepare_error is None:
                log.info(f"TASK-PREPARE ({self.asin}): Preparation cancelled; leaving book status unchanged.")
            else:
                self._update_db_on_failure(prepare_error)
            self._completion_event.set()
            return

        # --- 3. Choose the conversion path ---
        # The output format picks the finalize path:
        #   original -> lossless remux (requires the fast AAC-copy ".m4b" master;
        #               a FLAC fallback can't be -c copy'd into .m4b, so that one
        #               book quietly re-encodes to .m4b via the chunk path below)
        #   mp3      -> single-pass LAME encode (one task, no chunk/merge)
        #   m4b      -> the per-chapter AAC re-encode + merge (default)
        master_is_aac = str(self.context.get("audio_file", "")).lower().endswith(".m4b")
        if fmt == "original" and master_is_aac:
            log.info(f"TASK-PREPARE ({self.asin}): Original format — skipping encode, submitting remux task.")
            _yield_progress(self.asin, "Finalizing (lossless)...", 90, self.job_id)
            remux_task = Task(
                priority=TaskPriority.MERGE_BOOK,
                job_id=self.job_id,
                func=self._remux_and_finalize,
            )
            task_runner.submit_task(remux_task)
            return
        if fmt == "mp3":
            log.info(f"TASK-PREPARE ({self.asin}): MP3 format — submitting single-pass MP3 encode task.")
            _yield_progress(self.asin, "Encoding MP3...", 30, self.job_id)
            # One task per book at ENCODE_CHAPTER priority: it *is* the encode
            # work, and per-book parallelism across a bulk job emerges from having
            # one such task per book competing in the shared worker pool.
            mp3_task = Task(
                priority=TaskPriority.ENCODE_CHAPTER,
                job_id=self.job_id,
                func=self._encode_mp3_and_finalize,
            )
            task_runner.submit_task(mp3_task)
            return
        if fmt == "original":
            log.info(
                f"TASK-PREPARE ({self.asin}): Original format requested but the fast decrypt fell back to FLAC; "
                f"re-encoding this title to .m4b."
            )

        # --- Spawn all the ENCODE_CHAPTER tasks ---
        chapters = self.context.get("chapters", [])
        self.total_chunks = len(chapters)

        _yield_progress(self.asin, f"Preparing to process {self.total_chunks} chunk(s)", 30, self.job_id)

        if self.total_chunks == 0:
            # Two different causes land here and the old message ("Book has no
            # chapter information.") only described the first: the title really
            # arrived without chapters, OR it had them and the zero-length cleanup
            # in prepare_book_assets dropped every one (chapters sharing a start
            # offset, or early starts the branding trim clamped to 0). Prepare
            # reports the count it dropped to app.log but hands back only the final
            # list, so name both causes rather than assert the wrong one.
            log.warning(f"TASK-PREPARE ({self.asin}): No usable chapters after chapter processing. Cannot process.")
            self._update_db_on_failure(
                "Book has no usable chapters: the title reported none, or every chapter was empty "
                "and dropped during chapter cleanup (see the log for which)."
            )
            self._completion_event.set()
            return

        for i, chapter in enumerate(chapters):
            chunk_info = {
                "index": i,
                "total_chunks": self.total_chunks,
                "start": chapter.get("start_offset_ms", 0) / 1000.0,
                "duration": chapter.get("length_ms", 0) / 1000.0,
            }
            encode_task = Task(
                priority=TaskPriority.ENCODE_CHAPTER,
                job_id=self.job_id,
                func=self._encode_and_track_chunk,
                chunk_info=chunk_info,
            )
            task_runner.submit_task(encode_task)
        log.info(f"TASK-PREPARE ({self.asin}): Submitted {self.total_chunks} encoding tasks to the queue.")

    def _encode_and_track_chunk(self, chunk_info):
        """The actual function for the ENCODE_CHAPTER task."""
        if self._cancelled():
            return
        encoded_path = encode_chapter_chunk(self.asin, self.job_id, self.temp_dir, chunk_info, self.context)

        with self._lock:
            if encoded_path:
                self.completed_chunks += 1
                self.encoded_chunk_paths.append(encoded_path)
                progress = 30 + int((self.completed_chunks / self.total_chunks) * 60)
                _yield_progress(
                    self.asin, f"Processing chunk {self.completed_chunks}/{self.total_chunks}", progress, self.job_id
                )
            else:
                # If a chunk fails, we can't proceed.
                log.error(f"PROCESSOR ({self.asin}): A chunk failed to encode. Aborting merge.")
                self._fail_or_cancel("A chapter chunk failed to encode.")
                self._completion_event.set()  # Signal completion to unblock the main thread
                return  # Stop processing further

            # If this was the last chunk to be processed, spawn the final MERGE task.
            if self.completed_chunks == self.total_chunks:
                log.info(f"PROCESSOR ({self.asin}): All chunks encoded. Submitting final merge task.")
                merge_task = Task(
                    priority=TaskPriority.MERGE_BOOK,
                    job_id=self.job_id,
                    func=self._merge_and_finalize,
                )
                task_runner.submit_task(merge_task)

    def _verify_output_file(self):
        """
        Validate the finished file before we claim success, so a book is never
        marked DOWNLOADED while its file is missing, empty, or truncated (the
        "ghost book" and silent-truncation cases). Returns (ok, reason).
        """
        path = self.final_output_path
        if not path or not os.path.exists(path):
            return False, "Conversion reported success but no output file was found on disk."

        size = os.path.getsize(path)
        if size < _MIN_OUTPUT_BYTES:
            return False, f"Output file is implausibly small ({size} bytes); the conversion likely failed."

        with get_db_connection() as con:
            row = con.execute("SELECT runtime_min FROM audiobooks WHERE asin = ?", (self.asin,)).fetchone()
        expected_min = row["runtime_min"] if row else None
        if expected_min and expected_min > 0:
            actual_sec = _probe_duration_seconds(path, self.job_id)
            if actual_sec is None:
                return False, "Output file could not be read back (corrupt or unreadable)."
            expected_sec = expected_min * 60
            # Mirror the library Verify job's tolerance: only flag a file that
            # is significantly SHORTER than expected (under 95% and >10 minutes).
            if actual_sec < expected_sec * 0.95 and (expected_sec - actual_sec) > 600:
                return False, f"Output file is truncated (expected ~{expected_min}m, got {int(actual_sec / 60)}m)."

        return True, None

    def _place_supplementary_pdf(self):
        """
        Copy the companion PDF (if the download produced one) next to the
        finished audiobook, sharing its base name. Best-effort: a failure is
        logged, never fatal, and titles without a PDF are a silent no-op.
        """
        pdf_file = (self.context or {}).get("pdf_file")
        if not pdf_file or not os.path.exists(pdf_file):
            return
        pdf_target = f"{os.path.splitext(self.final_output_path)[0]}.pdf"
        try:
            shutil.copy2(pdf_file, pdf_target)
            log.info(f"PROCESSOR ({self.asin}): Saved companion PDF to {pdf_target}")
        except OSError as e:
            log.warning(f"PROCESSOR ({self.asin}): Could not save companion PDF: {e}")

    def _place_sidecar_files(self):
        """
        Write the optional sidecar files next to the finished audiobook, each
        sharing its base name. Best-effort like _place_supplementary_pdf: every
        item is independently guarded so one failure never blocks the others or
        the book's success, and all are off by default (Phase 2 settings) so a
        default install still produces exactly today's single-file output.
        """
        context = self.context or {}
        conv = load_settings().get("conversion", {})
        base = os.path.splitext(self.final_output_path)[0]

        # The title the two title-bearing sidecars carry, resolved exactly as the
        # FFMETADATA tag writer in chunked_conversion_logic.prepare_book_assets
        # resolves it, so a book's sidecars can never disagree with its embedded
        # tags: a user's custom_title wins outright and is never transformed, and
        # only the Audible-derived title gets the "(Unabridged)" cleanup.
        raw_title = (context.get("book_info") or {}).get("title")
        if self.custom_title:
            sidecar_title = self.custom_title
        elif conv.get("chapters", {}).get("strip_unabridged", False):
            sidecar_title = strip_unabridged(raw_title)
        else:
            sidecar_title = raw_title

        # 1. Cover image alongside the audiobook, keeping the cover's real ext.
        if conv.get("save_cover_alongside", False):
            cover_file = context.get("cover_file")
            if cover_file and os.path.exists(cover_file):
                cover_target = base + os.path.splitext(cover_file)[1]
                try:
                    shutil.copy2(cover_file, cover_target)
                    log.info(f"PROCESSOR ({self.asin}): Saved cover image to {cover_target}")
                except OSError as e:
                    log.warning(f"PROCESSOR ({self.asin}): Could not save cover image: {e}")

        # 2. Curated metadata.json.
        if conv.get("save_metadata_json", False):
            book_info = context.get("book_info")
            if book_info:
                json_target = base + ".metadata.json"
                try:
                    with open(json_target, "w", encoding="utf-8") as f:
                        json.dump(
                            build_metadata_json(book_info, title_override=sidecar_title),
                            f,
                            indent=2,
                            ensure_ascii=False,
                        )
                    log.info(f"PROCESSOR ({self.asin}): Saved metadata JSON to {json_target}")
                except OSError as e:
                    log.warning(f"PROCESSOR ({self.asin}): Could not save metadata JSON: {e}")

        # 3. .cue chapter sheet from the post-transform, output-timeline chapters.
        if conv.get("create_cue_sheet", False):
            chapters = context.get("chapters")
            if chapters:
                book_info = context.get("book_info") or {}
                author = (
                    ", ".join(a.get("name", "") for a in book_info.get("authors", []) if a.get("name"))
                    or "Unknown Author"
                )
                title = sidecar_title or "Unknown Title"
                cue_target = base + ".cue"
                try:
                    cue_text = generate_cue_sheet(
                        chapters, os.path.basename(self.final_output_path), title, author, self.asin
                    )
                    with open(cue_target, "w", encoding="utf-8") as f:
                        f.write(cue_text)
                    log.info(f"PROCESSOR ({self.asin}): Saved cue sheet to {cue_target}")
                except OSError as e:
                    log.warning(f"PROCESSOR ({self.asin}): Could not save cue sheet: {e}")

        # 4. Retain the raw AAX/AAXC master (+ voucher) prepare would have deleted.
        if conv.get("retain_aax", False):
            raw_audio_file = context.get("raw_audio_file")
            if raw_audio_file and os.path.exists(raw_audio_file):
                raw_target = base + os.path.splitext(raw_audio_file)[1]
                try:
                    shutil.copy2(raw_audio_file, raw_target)
                    log.info(f"PROCESSOR ({self.asin}): Retained raw audio to {raw_target}")
                except OSError as e:
                    log.warning(f"PROCESSOR ({self.asin}): Could not retain raw audio: {e}")
            # The voucher holds the AAXC decryption keys; retained beside the raw
            # AAXC so the pair stays usable. AAX titles have no voucher (None).
            voucher_file = context.get("voucher_file")
            if voucher_file and os.path.exists(voucher_file):
                voucher_target = base + ".voucher"
                try:
                    shutil.copy2(voucher_file, voucher_target)
                    log.info(f"PROCESSOR ({self.asin}): Retained voucher to {voucher_target}")
                except OSError as e:
                    log.warning(f"PROCESSOR ({self.asin}): Could not retain voucher: {e}")

    def _apply_file_timestamps(self):
        """
        Stamp the finished audiobook and its sidecars with the book's release or
        purchase date, when `conversion.file_timestamp_source` asks for it. Off
        ("none") by default, so a default install leaves the real creation time
        alone. Best-effort like _place_sidecar_files: a missing or unparseable
        date is skipped silently and a utime failure is logged, never fatal —
        a cosmetic timestamp must not turn a finished book into an error.
        """
        source = load_settings().get("conversion", {}).get("file_timestamp_source", "none")
        if source not in ("release_date", "purchase_date"):
            return

        book_info = (self.context or {}).get("book_info") or {}
        timestamp = _parse_timestamp_date(book_info.get(source))
        if timestamp is None:
            log.debug(f"PROCESSOR ({self.asin}): No usable {source} for file timestamps; leaving them as-is.")
            return

        # Both atime and mtime, so the pair stays consistent for tools that sort
        # on either. Sidecars only exist when their setting produced them, and are
        # matched however they are spelled on disk (an Audible cover saved as
        # ".JPG" must be stamped like a ".jpg" one).
        base = os.path.splitext(self.final_output_path)[0]
        targets = [self.final_output_path] + [f"{base}{suffix}" for suffix in _existing_sidecar_suffixes(base)]
        for path in targets:
            if not os.path.exists(path):
                continue
            try:
                os.utime(path, (timestamp, timestamp))
            except OSError as e:
                log.warning(f"PROCESSOR ({self.asin}): Could not set timestamp on '{path}': {e}")

    def _finalize_success(self, conversion_start_time, record_eta=True):
        """
        Shared post-conversion handling for both the re-encode merge and the
        lossless remux: verify the finished file, and on pass record the
        DOWNLOADED row (plus companion PDF). A failed verification marks the book
        ERROR instead. The caller owns the completion event.

        `record_eta` feeds this run's duration into the conversion-time estimator.
        The re-encode path sets it; the lossless remux does NOT, because a remux
        takes seconds and its rate isn't comparable to a re-encode's — mixing the
        two into the shared rolling average would skew the estimate (and the
        timeout derived from it) for later re-encode jobs.
        """
        # Never trust the conversion's exit code alone: confirm the file is
        # actually on disk and complete before declaring the book DOWNLOADED.
        output_ok, reason = self._verify_output_file()
        if not output_ok:
            # A cancel firing during the final verification probe surfaces here as
            # a verification failure: SIGTERM (-15) makes the registered ffprobe
            # return no duration, so _verify_output_file reports the file "could
            # not be read back." That is a cancellation, not corruption — the file
            # on disk is a valid, just-produced audiobook. Treat it like the other
            # cancel paths (_fail_or_cancel): leave the finished file in place and
            # the book's status untouched (it stays NEW/MISSING and is retried)
            # rather than deleting a good file and marking it ERROR.
            if self.stop_event is not None and self.stop_event.is_set():
                log.info(
                    f"PROCESSOR ({self.asin}): Verification cancelled; leaving finished file and status unchanged."
                )
                return
            log.error(f"PROCESSOR ({self.asin}): Output verification failed: {reason}")
            # Delete the failed artifact so a truncated/corrupt file isn't left
            # sitting at the final path masquerading as a real book. It would
            # otherwise linger until a later retry's embedded-ASIN check chose to
            # overwrite it; removing it now keeps /data honest in the meantime.
            if self.final_output_path and os.path.exists(self.final_output_path):
                try:
                    os.remove(self.final_output_path)
                    log.info(f"PROCESSOR ({self.asin}): Removed failed output file {self.final_output_path}.")
                except OSError as e:
                    log.warning(f"PROCESSOR ({self.asin}): Could not remove failed output file: {e}")
            self._update_db_on_failure(reason)
            return

        if record_eta:
            conversion_duration_sec = time.time() - conversion_start_time
            with get_db_connection() as con:
                runtime_row = con.execute("SELECT runtime_min FROM audiobooks WHERE asin = ?", (self.asin,)).fetchone()
                if runtime_row:
                    record_conversion_time(runtime_row["runtime_min"], conversion_duration_sec)

        # On Success, update the database. is_duplicate records whether a
        # same-author+title collision forced an ASIN suffix onto our name;
        # it is written explicitly (0 when clean) so a later re-download
        # that resolves without a collision clears a stale flag.
        #
        # The path this book was tracked at is read FIRST: the UPDATE below
        # overwrites it, and it's the only record of where a re-download's
        # previous file lives (see _cleanup_stale_files).
        with get_db_connection() as con:
            previous_row = con.execute("SELECT filepath FROM audiobooks WHERE asin = ?", (self.asin,)).fetchone()
            previous_path = previous_row["filepath"] if previous_row else None
            con.execute(
                "UPDATE audiobooks SET status = 'DOWNLOADED', filepath = ?, "
                "error_message = '', retry_count = 0, is_duplicate = ? WHERE asin = ?",
                (self.final_output_path, int(self.is_duplicate), self.asin),
            )
        # Place any companion PDF and optional sidecars before the temp dir is
        # torn down (the raw master, cover, and metadata all live there).
        self._place_supplementary_pdf()
        self._place_sidecar_files()
        # Stamp timestamps last, once every file that shares the base name exists.
        self._apply_file_timestamps()
        # Only now that this run's own output is fully in place is it safe to
        # remove what the previous download left somewhere else.
        self._cleanup_stale_files(previous_path)
        _yield_progress(self.asin, "Complete!", 100, self.job_id)

    def _cleanup_stale_files(self, previous_path):
        """
        Remove the file a re-download left behind. A re-download re-derives its
        output path from the *current* settings, so a changed output format or
        naming template writes the new file somewhere else entirely and the old
        one stops being referenced by anything — it just sits in /data forever.

        Gated on this job's answer to the UI prompt OR the saved setting, since a
        scheduled job carries no params and the setting is the only thing that can
        speak for it. Every guard below is load-bearing: this is the finalizer's
        only destructive step, so it refuses to act unless the tracked previous
        path is a real, different file inside the output root. Each unlink is
        independently best-effort — a finished book is never failed over cleanup.
        """
        # Tri-state per-job flag: an explicit False is the user DECLINING the
        # prompt, which vetoes the saved setting. Only None ("never asked") falls
        # back to it. See BookProcessor.__init__.
        if self.cleanup_stale_files is not None:
            consented = self.cleanup_stale_files
        else:
            consented = load_settings().get("job", {}).get("download", {}).get("cleanup_stale_files", False)
        if not consented:
            return

        if not previous_path:
            return
        # Every destructive comparison below resolves symlinks: os.path.abspath only
        # collapses "."/".." , so a symlinked alias of the output tree (say
        # /data/Author -> /data/library/Author) makes the old and new paths compare
        # unequal even when they are the same file, and this run's own freshly
        # written output would be deleted.
        previous_real = os.path.realpath(previous_path)
        new_real = os.path.realpath(self.final_output_path)
        if previous_real == new_real:
            return  # The re-download overwrote its own file; nothing was left behind.
        # This exists() guard is also what implements the plan's "never for a
        # MISSING book" rule: a MISSING row's tracked file is gone from disk, so
        # cleanup returns here before touching anything.
        if not os.path.exists(previous_path):
            return
        # Belt-and-braces on top of the realpath comparison — a hard link (or a
        # symlink realpath could not resolve) still makes two different-looking
        # paths the same inode. An OSError means the stat failed, so we cannot
        # prove they are the same file and fall through to the remaining guards.
        try:
            if os.path.exists(self.final_output_path) and os.path.samefile(previous_path, self.final_output_path):
                return
        except OSError:
            pass
        # Whatever the DB row claims, never delete outside the output directory.
        # Both sides are resolved so a symlinked /data still compares as inside it.
        data_root = os.path.realpath("/data")
        if not previous_real.startswith(data_root + os.sep):
            log.warning(f"PROCESSOR ({self.asin}): Refusing to clean up '{previous_path}' — it is outside {data_root}.")
            return

        try:
            os.remove(previous_path)
            log.info(f"PROCESSOR ({self.asin}): Removed stale file from the previous download: {previous_path}")
        except OSError as e:
            log.warning(f"PROCESSOR ({self.asin}): Could not remove stale file '{previous_path}': {e}")

        # Sidecars come off only when the extension-stripped BASE actually moved.
        # On a format-only change ("Title.m4b" -> "Title.mp3") the old base IS the
        # new base, so the "old" sidecars are the ones _place_sidecar_files wrote
        # moments ago for this very run — deleting them would destroy this
        # download's own output.
        old_base = os.path.splitext(previous_real)[0]
        new_base = os.path.splitext(new_real)[0]
        if old_base != new_base:
            # ...and only when nothing ELSE still lives at the old base. Sidecars
            # are keyed by the base while audio files are not, so a second book
            # sitting there under a different audio extension shares these exact
            # files — they may be its only cover/PDF/cue/metadata/raw master.
            if self._output_base_is_shared(old_base, previous_real):
                log.info(
                    f"PROCESSOR ({self.asin}): Skipped the stale-sidecar sweep at '{old_base}' — "
                    f"the base is still in use by another book."
                )
            else:
                # Matched however they are spelled on disk, same as the rename and
                # timestamp sweeps — a leftover ".JPG" is as stale as a ".jpg".
                for suffix in _existing_sidecar_suffixes(old_base):
                    stale_sidecar = f"{old_base}{suffix}"
                    try:
                        os.remove(stale_sidecar)
                        log.info(f"PROCESSOR ({self.asin}): Removed stale sidecar: {stale_sidecar}")
                    except OSError as e:
                        log.warning(f"PROCESSOR ({self.asin}): Could not remove stale sidecar '{stale_sidecar}': {e}")

        # The old folder may now be empty (a naming-template change moves whole
        # directory levels); the existing helper stops at the first non-empty one.
        _cleanup_empty_dirs(os.path.dirname(previous_path))

    def _output_base_is_shared(self, old_base, previous_real):
        """
        True when something OTHER than this book's previous download still occupies
        `old_base`, which makes the sidecars there jointly owned and unsafe to
        delete. Libraries created before same-base collisions were prevented can
        hold two books at one base under different audio extensions.

        Two independent signals, either of which is enough:
          1. Another audio file is still on disk at the base — anything from
             _AUDIO_EXTENSIONS other than the previous file we just removed.
          2. Another audiobooks row is tracked at the same base, even if its file
             is temporarily absent (a MISSING book still owns its cover/PDF).

        The DB half reads every non-null filepath and compares bases in Python
        rather than with a LIKE pattern: libraries are small, and a base name can
        contain LIKE wildcards that would need escaping.
        """
        for ext in _AUDIO_EXTENSIONS:
            candidate = f"{old_base}{ext}"
            if os.path.realpath(candidate) == previous_real:
                continue  # The previous download's own file, not a second book.
            if os.path.exists(candidate):
                return True

        with get_db_connection() as con:
            rows = con.execute("SELECT asin, filepath FROM audiobooks WHERE filepath IS NOT NULL").fetchall()
        for row in rows:
            if row["asin"] == self.asin:
                continue
            if os.path.splitext(os.path.realpath(row["filepath"]))[0] == old_base:
                return True
        return False

    def _merge_and_finalize(self):
        """The actual function for the MERGE_BOOK task (re-encode path)."""
        if self._cancelled():
            return
        log.info(f"TASK-MERGE ({self.asin}): Starting.")
        conversion_start_time = time.time()

        success = merge_book_chunks(
            self.asin, self.job_id, self.temp_dir, self.final_output_path, self.context, self.encoded_chunk_paths
        )

        if not success:
            self._fail_or_cancel("Final merge of chapter chunks failed.")
        else:
            self._finalize_success(conversion_start_time)

        # This is the final step, so we signal the main `run` method to unblock.
        self._completion_event.set()
        log.info(f"TASK-MERGE ({self.asin}): Finalization complete.")

    def _remux_and_finalize(self):
        """
        MERGE_BOOK task for no-re-encode mode: mux chapters/metadata/cover onto
        the decrypted AAC master with -c copy (no transcode), then run the same
        output verification and success finalization as the re-encode path.
        """
        if self._cancelled():
            return
        log.info(f"TASK-REMUX ({self.asin}): Starting.")
        conversion_start_time = time.time()

        success = remux_book_lossless(self.asin, self.job_id, self.temp_dir, self.final_output_path, self.context)

        if not success:
            self._fail_or_cancel("Lossless remux failed.")
        else:
            # Don't feed the remux's (much faster) duration into the shared ETA
            # model — see _finalize_success's record_eta note.
            self._finalize_success(conversion_start_time, record_eta=False)

        # This is the final step, so we signal the main `run` method to unblock.
        self._completion_event.set()
        log.info(f"TASK-REMUX ({self.asin}): Finalization complete.")

    def _encode_mp3_and_finalize(self):
        """
        ENCODE_CHAPTER task for MP3 output: single-pass LAME encode of the whole
        book, then the same output verification and success finalization as the
        other paths. Mirrors _remux_and_finalize (one task, no merge stage).
        """
        if self._cancelled():
            return
        log.info(f"TASK-MP3 ({self.asin}): Starting.")
        conversion_start_time = time.time()

        # The stop event goes along so the encode can recheck it right before the
        # long ffmpeg run: a cancel landing during the probe that precedes the
        # spawn is otherwise spent before there is a process to kill.
        success = encode_book_mp3(
            self.asin, self.job_id, self.temp_dir, self.final_output_path, self.context, stop_event=self.stop_event
        )

        if not success:
            self._fail_or_cancel("MP3 encode failed.")
        else:
            # Single-threaded LAME rates aren't comparable to the parallel
            # chunked-AAC encode, so keep them out of the shared ETA model
            # (same reasoning as the remux path's record_eta=False).
            self._finalize_success(conversion_start_time, record_eta=False)

        # This is the final step, so we signal the main `run` method to unblock.
        self._completion_event.set()
        log.info(f"TASK-MP3 ({self.asin}): Finalization complete.")

    def _fail_or_cancel(self, error_message):
        """
        Record a step failure — unless the job is being cancelled.

        Cancellation sends SIGTERM to the running ffmpeg, and the merge/encode/
        remux helpers report that -15 as a plain False/None without distinguishing
        it from a genuine error. When the stop_event is set we treat that as the
        cancellation it is and leave the book's status untouched (it stays
        NEW/MISSING and is retried) instead of stranding it in ERROR with a
        misleading "... failed." message. Mirrors the (None, None) cancel handling
        on the prepare path.
        """
        if self.stop_event is not None and self.stop_event.is_set():
            log.info(f"PROCESSOR ({self.asin}): Step cancelled; leaving book status unchanged.")
            return
        self._update_db_on_failure(error_message)

    def _update_db_on_failure(self, error_message):
        """
        Centralized method to update the database when any step fails.

        The retry counter is bumped here, and this is the ONLY place that raises
        it — without the bump, a permanently failing title is re-downloaded on
        every scheduled run forever, hammering the Audible API. Read together
        with the auto-process ERROR gate (`retry_count <= 1` in db.py
        `_get_books_by_status`) and the two resets, the full lifecycle is:

          0 --fail--> 1   still selected: this is the ONE automatic retry the
                          settings UI promises, and an automatic job does not
                          reset the counter, so the retry is genuinely the last
          1 --fail--> 2   above the gate; never selected automatically again
          success -> 0    a working book starts over with a clean slate
          manual  -> 0    enqueuing by hand re-arms exactly one future automatic
                          attempt, even if the manual attempt itself fails (0 ->
                          1 is still inside the gate)
          cancel  -> unchanged (see below); a cancel is not an attempt

        The bump is done inside the UPDATE (rather than read-modify-write) so two
        book processors failing at once can't clobber each other's count; the
        COALESCE mirrors bin/start.sh, which treats the column as possibly NULL
        on rows that predate it.

        Callers must not route cancellations here — see `_fail_or_cancel` and the
        (None, None) prepare-path check: a user-cancelled download leaves the
        book's status untouched, so it must not consume the automatic retry.
        """
        log.error(f"PROCESSOR ({self.asin}):   -> ERROR: {error_message}")
        with get_db_connection() as con:
            con.execute(
                "UPDATE audiobooks SET status = 'ERROR', error_message = ?, "
                "retry_count = COALESCE(retry_count, 0) + 1 WHERE asin = ?",
                (error_message, self.asin),
            )
        _yield_progress(self.asin, "Failed!", 100, self.job_id)


def run_book_processing_logic(asin, job_id, download_complete_event=None, stop_event=None):
    """
    Main entry point called by the download_worker.
    Creates a BookProcessor instance and runs it.
    """
    # Pass the events down to the BookProcessor instance.
    processor = BookProcessor(
        asin=asin, job_id=job_id, download_complete_event=download_complete_event, stop_event=stop_event
    )
    processor.run()

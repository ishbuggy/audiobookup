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
import sqlite3
import subprocess
import tempfile
import time
from datetime import datetime
from threading import Event, Lock

from . import TEMP_DIR
from .chapter_transforms import strip_unabridged

# Import the task-oriented functions and the global announcer
from .chunked_conversion_logic import (
    _embed_cover_art,
    _yield_progress,
    encode_book_mp3,
    encode_chapter_chunk,
    merge_book_chunks,
    prepare_book_assets,
    remux_book_lossless,
)
from .db import get_db_connection, replace_book_files
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

# The same floor, applied to one chapter file of a SPLIT book, only makes sense
# once the chapter is long enough to plausibly reach it: three seconds of
# 128 kbps AAC is roughly 48 KB, and the minimum-duration merge lets a chapter
# that short become its own file. Parts under _FULL_SIZE_FLOOR_PART_MS are held
# to _MIN_PART_BYTES instead — small enough that a genuinely short chapter
# passes, large enough that a header-only file with no audio stream (a few
# hundred bytes) still fails. See _verify_split_output_files.
_MIN_PART_BYTES = 4 * 1024
_FULL_SIZE_FLOOR_PART_MS = 60_000

# Floor for the "don't wait forever" completion timeout: short books and books
# with no known runtime get two hours regardless of any model below.
_COMPLETION_TIMEOUT_FLOOR_SEC = 7200

# The MP3 path's completion budget, as a multiple of the book's own runtime.
# Three times real time comfortably covers the download, the decrypt and a
# single-pass LAME encode even on slow hardware; see _completion_timeout for
# why that path cannot use the AAC estimator's rate. A SPLIT MP3 book (v0.24.0)
# takes the same budget while doing N parallel LAME encodes instead of one pass
# — which is strictly less wall-clock work for the same audio, so the budget
# stays conservative rather than becoming tight.
_MP3_TIMEOUT_RUNTIME_MULTIPLE = 3

# Every file that can end up sharing a finished audiobook's base name: the
# companion PDF, cover image, cue sheet, metadata JSON, the annotations dump,
# and a retained raw master (+ its voucher). Anything that follows the
# audiobook — a rename moving it, a timestamp stamp — walks this list so the two
# can't drift apart.
_SIDECAR_SUFFIXES = (
    ".pdf",
    ".jpg",
    ".png",
    ".cue",
    ".metadata.json",
    ".annotations.json",
    ".aax",
    ".aaxc",
    ".voucher",
)

# The subset of the above that ONLY this app writes at a book's own base name.
# It is the corroborating evidence _owned_sidecar_base needs: a folder's sidecar
# files are read backwards to guess a split book's stem, and an external library
# manager sharing the folder writes files that look exactly like the guess —
# Audiobookshelf drops a "cover.jpg" (plus a bare "metadata.json", which matches
# no suffix at all) into every book folder, so a folder whose real sidecars are
# gone reads back as the base "cover", and the stale sweep then deletes another
# program's file. A retained ".aax"/".aaxc" master and its ".voucher" are
# audible-cli artifacts no external library manager produces, so they corroborate
# as strongly as a curated ".metadata.json" — and for a SPLIT book they are
# usually the ONLY corroborator on offer, since a split book never gets a cue
# sheet (D9) and both JSON dumps are opt-in settings. Cover images and PDFs are
# exactly what other tools also leave lying around, so they corroborate nothing.
# Deliberately narrow, and the asymmetry is the point: a suffix missing from here
# costs a skipped sweep — the files just stay where they are, stranded in a
# folder no row points at — while a wrong one costs a user's data.
_APP_WRITTEN_SIDECAR_SUFFIXES = (".cue", ".metadata.json", ".annotations.json", ".aax", ".aaxc", ".voucher")

# The same list for a SPLIT book, minus the cue sheet. A split book never gets
# one (D9 refuses to write it), so a ".cue" in a split book's folder is not weak
# evidence that the base is ours — it is affirmative evidence that it is somebody
# else's, since cue sheets beside audiobooks are ordinary output from CD rippers
# and other taggers. Corroborating on it would hand a foreign base to the sweep
# that deletes and the rename that moves.
_SPLIT_APP_WRITTEN_SIDECAR_SUFFIXES = tuple(suffix for suffix in _APP_WRITTEN_SIDECAR_SUFFIXES if suffix != ".cue")

# Every audio extension a tracked book's file can carry. Two books at the same
# base under DIFFERENT audio extensions share one set of sidecars, so any of
# these occupying our base is a collision — not just the format we happen to be
# writing. ".m4a" is in the list because import_logic keeps an upload's real
# container extension (see IMPORTABLE_EXTS), so real libraries contain .m4a
# books, and rename_book_to_match_metadata preserves whatever it finds on disk.
_AUDIO_EXTENSIONS = (".m4b", ".mp3", ".m4a")

# How many ASIN-suffixed candidates an allocator will try before giving up and
# using the last one it built. The first candidate is free in every realistic
# library; the walk exists only so a taken suffixed name is never assumed free
# (backlog #28), and the cap is there so a pathological directory can't spin.
_SUFFIX_WALK_LIMIT = 100

# The sidecar suffixes, longest first. Reading a file NAME back to the base it
# hangs off has to match the longest suffix that fits, or "Title.metadata.json"
# would be read as a base of "Title.metadata" under a ".json" that isn't even in
# the list. Only used by _unique_sidecar_base; the forward direction (base ->
# suffixes) has no such ambiguity.
_SIDECAR_SUFFIXES_LONGEST_FIRST = tuple(sorted(_SIDECAR_SUFFIXES, key=len, reverse=True))


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


def _unique_sidecar_base(folder):
    """
    The one extension-stripped base inside `folder` that existing sidecar files
    hang off, or None when there is nothing to go on.

    A split book's sidecars keep the single-file-EQUIVALENT name (D9) inside the
    book's folder, and that name is stored nowhere: `audiobooks.filepath` holds
    the folder and `book_files` holds the parts, whose names come from a
    different template entirely. Reading it back off the sidecars themselves is
    what lets a rename move them and the annotations button find them.

    None is returned both when the folder holds no sidecar at all and when it
    holds sidecars under MORE than one base — two books sharing a folder is
    exactly the ambiguity #20's shared-base guard exists for, and guessing which
    base is ours would move (or delete) the other book's cover.

    Only the exact suffixes in _SIDECAR_SUFFIXES count, so a chapter PART file is
    never read as a sidecar however closely its name resembles the base.

    This is a GUESS and nothing more: it reads whatever is in the folder, which
    on a real library includes files other programs put there. Nothing that
    moves or deletes may call it directly — `_owned_sidecar_base` is the guarded
    form those callers use.
    """
    try:
        entries = os.listdir(folder)
    except OSError:
        return None

    bases = set()
    for entry in entries:
        for suffix in _SIDECAR_SUFFIXES_LONGEST_FIRST:
            # `len(entry) > len(suffix)` keeps a file that is ONLY a suffix
            # (".pdf" with no name in front of it) from claiming an empty base.
            if len(entry) > len(suffix) and entry.lower().endswith(suffix):
                bases.add(os.path.join(folder, entry[: -len(suffix)]))
                break

    if len(bases) == 1:
        return bases.pop()
    if bases:
        log.info(
            f"SIDECARS: '{folder}' holds sidecar files under {len(bases)} different names; "
            f"not guessing which of them belongs to the book."
        )
    return None


def _owned_sidecar_base(folder, expected_stem=None, quiet=False, split=False):
    """
    The sidecar base inside `folder` that this book can be shown to OWN, or None
    when the folder's one candidate cannot be corroborated.

    `_unique_sidecar_base` infers a base from whatever sidecar-shaped files are
    lying in the folder, which is the only way to recover a split book's
    single-file-equivalent stem (it is stored nowhere — see that function). But
    an inference is not ownership, and every caller here either moves or deletes
    what it is handed: a book folder shared with an external library manager
    yields a perfectly unambiguous base that belongs to somebody else, and the
    stale sweep dutifully deleted that program's cover image.

    So the guess has to be corroborated, by either:

    1. **The name.** `expected_stem` is the single-file-equivalent stem this book
       renders TODAY (the run's own sidecar stem, the rename's target stem, the
       stem re-rendered from the naming template). A folder whose base is spelled
       the same is this book's, and that covers every case where the naming of
       the stem itself didn't change between runs — the overwhelming majority.
    2. **The files.** A base carrying at least one _APP_WRITTEN_SIDECAR_SUFFIXES
       file was written by this app whatever it is called, so it can still be
       swept or moved after a rename has left its stem behind. This arm is a
       backstop, not a guarantee: for a split book the cue sheet is never written
       (D9) and both JSON dumps are opt-in, so what usually answers here is a
       retained ".aax"/".aaxc" master and its ".voucher" — and a renamed split
       book with none of those on disk is simply left alone.

    `split` says the book being asked about is a split one, which SHRINKS the
    corroborating set: since a split book never gets a cue sheet, a ".cue" there
    can only have come from somewhere else, and reading it as proof of ownership
    inverts the whole point of this function.

    Neither answered means we cannot prove the files are ours, so we do nothing
    with them: an abandoned cover left behind in a folder is a mess, deleting a
    file this app never wrote is data loss.

    `quiet` silences the "leaving them alone" line for callers that are only
    ASKING where a book's sidecars are rather than moving or deleting any — the
    same affordance, and for the same reason, as `_split_folder_and_stem`: the
    annotations button runs this on every press, and a book folder shared with
    another library manager would announce its refusal into a user-downloadable
    app.log every time. The destructive callers (the stale sweep, the rename)
    stay loud, because there the refusal explains a sweep that didn't happen.
    """
    base = _unique_sidecar_base(folder)
    if base is None:
        return None

    if expected_stem and os.path.basename(base) == expected_stem:
        return base

    # Matched case-insensitively for the same reason the sweeps are: the files
    # on disk are not always spelled the way the suffix list is ('.Metadata.JSON'
    # is a real thing a user's filesystem hands back).
    corroborating = _SPLIT_APP_WRITTEN_SIDECAR_SUFFIXES if split else _APP_WRITTEN_SIDECAR_SUFFIXES
    if any(suffix.lower() in corroborating for suffix in _existing_sidecar_suffixes(base)):
        return base

    if not quiet:
        log.info(
            f"SIDECARS: '{folder}' holds sidecar files at '{os.path.basename(base)}', which is neither this "
            f"book's name nor a file this app wrote; leaving them alone."
        )
    return None


def _tracked_part_paths(asin):
    """
    A book's split-part file paths in playback order, or an empty list when the
    book isn't split. Reads through this module's own `get_db_connection` (rather
    than db.get_book_files) so it follows the same connection the rest of the
    file uses, and tolerates a database with no `book_files` table at all — a
    hand-restored library.db is a real thing, and a missing child table must
    never break a rename or a cleanup.
    """
    try:
        with get_db_connection() as con:
            rows = con.execute("SELECT filepath FROM book_files WHERE asin = ? ORDER BY part_index", (asin,)).fetchall()
        return [row["filepath"] for row in rows if row["filepath"]]
    except sqlite3.Error as e:
        log.warning(f"Could not read the per-chapter file rows for {asin}: {e}")
        return []


def _tracked_path_owners():
    """
    Every audio path the database tracks, as `realpath -> {owning ASINs}`: the
    `audiobooks.filepath` of each book PLUS every split book's part rows.

    One book can now own many files, so "is this file still claimed by someone"
    can no longer be answered from the parent table alone — the stale-file
    cleanup consults this before deleting anything (#30), and the split-folder
    allocator consults it before planning parts into a folder.

    A SET of owners per path, not one owner, because the whole reason this exists
    is the case where two rows name one file: an arbitrary last-one-wins answer
    could name the asking book itself and wave the delete through.

    Realpath-keyed so a symlinked alias of the output tree compares equal, and
    read in one pass because libraries are small (the same reasoning as
    _output_base_is_shared's row scan).
    """
    owners = {}
    with get_db_connection() as con:
        rows = list(con.execute("SELECT asin, filepath FROM audiobooks WHERE filepath IS NOT NULL").fetchall())
        try:
            rows += list(con.execute("SELECT asin, filepath FROM book_files").fetchall())
        except sqlite3.Error as e:
            log.warning(f"Could not read the per-chapter file rows while checking path ownership: {e}")
    for row in rows:
        if row["filepath"]:
            owners.setdefault(os.path.realpath(row["filepath"]), set()).add(row["asin"])
    return owners


def _base_claimed_by_another_book(base, asin):
    """
    True when an audio file already sits at the extension-stripped `base` and is
    not this book's own — the re-validation an ASIN-suffixed collision candidate
    never used to get (#28): "<base>_<asin>" was built and used on the assumption
    that nothing could possibly be there.

    Deliberately cheap: filesystem existence plus one ownership lookup per file
    that IS there, and no ffprobe. Callers hold the global reservation lock while
    walking candidates, and the untracked case is judged conservatively (an
    untracked file occupying the candidate counts as taken) rather than by
    spawning a subprocess under that lock.
    """
    for ext in _AUDIO_EXTENSIONS:
        candidate = f"{base}{ext}"
        if not os.path.exists(candidate):
            continue
        if _path_owner(candidate) != asin:
            return True
    return False


def _path_owner(path):
    """
    Which book tracks `path` — the `audiobooks` row first, then the split-part
    rows — or None when nothing does. Compared by the stored path, not realpath:
    this answers "is the row that names this exact file ours", which is how the
    existing collision checks have always asked it.
    """
    try:
        with get_db_connection() as con:
            row = con.execute("SELECT asin FROM audiobooks WHERE filepath = ?", (path,)).fetchone()
            if row:
                return row["asin"]
            row = con.execute("SELECT asin FROM book_files WHERE filepath = ?", (path,)).fetchone()
        return row["asin"] if row else None
    except sqlite3.Error as e:
        log.warning(f"Could not check which book owns '{path}': {e}")
        return None


def _output_base_is_shared(asin, old_base, previous_reals):
    """
    True when something OTHER than this book's previous download still occupies
    `old_base`, which makes the sidecars there jointly owned and unsafe to
    delete or move. Libraries created before same-base collisions were prevented
    can hold two books at one base under different audio extensions.

    Three independent signals, any of which is enough:
      1. Another audio file is still on disk at the base — anything from
         _AUDIO_EXTENSIONS other than the previous file(s) this book just left.
      2. Another audiobooks row is tracked at the same base, even if its file
         is temporarily absent (a MISSING book still owns its cover/PDF).
      3. Another book's split PART sits at that exact base — a chapter file can
         legitimately render to the single-file-equivalent name, and it is as
         much a claim on the base as a whole-book file is.

    `previous_reals` is the realpath (or realpaths, for a split book's part set)
    of the files this book itself had there, which are not a second owner.

    The DB halves read every non-null filepath and compare bases in Python
    rather than with a LIKE pattern: libraries are small, and a base name can
    contain LIKE wildcards that would need escaping.
    """
    previous_reals = set(previous_reals or ())
    for ext in _AUDIO_EXTENSIONS:
        candidate = f"{old_base}{ext}"
        if os.path.realpath(candidate) in previous_reals:
            continue  # The previous download's own file, not a second book.
        if os.path.exists(candidate):
            return True

    with get_db_connection() as con:
        rows = con.execute("SELECT asin, filepath FROM audiobooks WHERE filepath IS NOT NULL").fetchall()
        try:
            rows = list(rows) + list(con.execute("SELECT asin, filepath FROM book_files").fetchall())
        except sqlite3.Error as e:
            log.warning(f"Could not read the per-chapter file rows while checking '{old_base}': {e}")
    for row in rows:
        if row["asin"] == asin or not row["filepath"]:
            continue
        if os.path.splitext(os.path.realpath(row["filepath"]))[0] == old_base:
            return True
    return False


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


def resolve_configured_file_timestamp(book_fields):
    """
    Resolve the epoch seconds a book's files should carry under the current
    `conversion.file_timestamp_source` setting, as `(source, timestamp)`.

    `source` is None when the setting is off ("none", the default, or anything
    unrecognized in an old settings.json) — that is the "leave real file times
    alone" answer. Otherwise `source` names the field that was consulted and
    `timestamp` is either the parsed value or None when the book has no usable
    date, which the caller reports however suits it.

    `book_fields` is any mapping carrying Audible's `release_date` /
    `purchase_date` fields under those names — the processor's in-memory
    book_info during a download, or an `audiobooks` row for a book that
    finished long ago. Both spellings match, so the same policy answers for
    both and the on-demand sidecars stamp like the download-time ones.
    """
    source = load_settings().get("conversion", {}).get("file_timestamp_source", "none")
    if source not in ("release_date", "purchase_date"):
        return None, None
    return source, _parse_timestamp_date((book_fields or {}).get(source))


def _build_naming_values(
    asin,
    author,
    title,
    narrator,
    publisher,
    series=None,
    series_sequence=None,
    release_date=None,
    language=None,
    truncate_subtitle=False,
):
    """
    Build the placeholder -> value map for the *book-level* naming tags, with
    every value already sanitized for use in a filename. This is the single
    definition of what each tag means; both the book path (`build_base_output_path`)
    and the per-part chapter filename (`render_chapter_filename`) render from it,
    so the two can never drift apart.

    The two missing-value rules are preserved exactly: the original five tags
    ({author} {title} {narrator} {publisher} {asin}) fall back to "Unknown ...",
    while the newer optional tags ({series} {series_part} {year} {language})
    render as the empty string when the value is missing (None/""/"N/A").

    `truncate_subtitle` is passed in rather than read from settings so this stays
    a pure function; the caller decides whether the user asked for it.
    """
    # Trim a long subtitle before sanitization (which rewrites the ':' separator).
    raw_title = title or "Unknown Title"
    if truncate_subtitle:
        raw_title = _strip_subtitle(raw_title)

    # {year}: first four characters of release_date, but only when they are all
    # digits (sync's "N/A" fallback and malformed dates render empty).
    year = ""
    if release_date:
        candidate = str(release_date)[:4]
        if len(candidate) == 4 and candidate.isdigit():
            year = candidate

    return {
        "{author}": _sanitize_filename(author or "Unknown Author"),
        "{title}": _sanitize_filename(raw_title),
        "{narrator}": _sanitize_filename(narrator or "Unknown Narrator"),
        "{publisher}": _sanitize_filename(publisher or "Unknown Publisher"),
        "{asin}": _sanitize_filename(asin),
        "{series}": _resolve_optional_tag(series),
        "{series_part}": _resolve_optional_tag(series_sequence),
        "{year}": year,
        "{language}": _resolve_optional_tag(language),
    }


def _apply_naming_values(template, values):
    """
    Substitute a placeholder -> value map into a naming template. Plain literal
    replacement, in map order.

    The invariant that makes the map order safe *for placeholder names* is that
    every key carries its closing brace, so no key is a prefix of another:
    "{series}" cannot match inside "{series_part}", nor "{ch}" inside "{ch_title}".
    Order is still load-bearing for *values*, though — a value substituted early
    is part of the string later placeholders are searched in, so a book whose
    title literally contained "{ch}" would have that text replaced by the part
    number. That is preserved behaviour from the chained .replace() this came
    from, not something to rely on.
    """
    rendered = template
    for placeholder, value in values.items():
        rendered = rendered.replace(placeholder, value)
    return rendered


def _clean_name_segment(segment):
    """
    Drop-segment cleanup for one path segment: collapse whitespace runs and strip
    leading/trailing spaces, dots, hyphens, underscores and commas — so the
    "Author - " left behind by a missing trailing tag becomes "Author". Returns
    the empty string when nothing survives, which callers treat as "this segment
    should be dropped" (directories) or "fall back to a generic name" (filenames).
    """
    return re.sub(r"\s+", " ", segment).strip(" .-_,")


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

    # Tag values (subtitle trimming, sanitization and the two missing-value
    # rules) all live in _build_naming_values; author/title are pulled back out
    # for the empty-filename fallback below.
    values = _build_naming_values(
        asin,
        author,
        title,
        narrator,
        publisher,
        series=series,
        series_sequence=series_sequence,
        release_date=release_date,
        language=language,
        truncate_subtitle=naming.get("truncate_subtitle", False),
    )
    author_val = values["{author}"]
    title_val = values["{title}"]

    relative_path = _apply_naming_values(template, values)

    # Drop-segment cleanup. Split on '/'; the last segment is the filename and the
    # rest are directory levels. Each segment goes through _clean_name_segment, so
    # "Author - " left by a missing trailing tag becomes "Author". Directory
    # segments that collapse to empty are dropped entirely (no "N/A" folders); if
    # the filename segment collapses to empty, fall back to "<author> - <title>".
    segments = relative_path.split("/")
    filename = segments[-1]
    directories = segments[:-1]

    cleaned_dirs = []
    for seg in directories:
        seg = _clean_name_segment(seg)
        if seg:
            cleaned_dirs.append(seg)

    filename = _clean_name_segment(filename)
    if not filename:
        # The fallback's own halves can be empty too: a value of " . " or "..." is
        # truthy (so it never took the "Unknown ..." branch above) but sanitizes
        # away to nothing, which used to leave the file literally named " - ".
        # Re-apply the same fallbacks the tags use so the name always says something.
        filename = f"{author_val or 'Unknown Author'} - {title_val or 'Unknown Title'}"
        # ...and check the rebuilt name the same way, because it can strip away
        # too: author "-" and title "-" are both truthy (neither took the
        # "Unknown ..." branch above) yet the assembled "- - -" reduces to nothing
        # under this strip set, which would name the file "- - -.m4b". Only the
        # fully generic name is guaranteed to survive.
        if not _clean_name_segment(filename):
            filename = "Unknown Author - Unknown Title"

    relative_path = os.path.join(*cleaned_dirs, filename) if cleaned_dirs else filename
    return os.path.join("/data", f"{relative_path}{ext}")


def normalize_chapter_file_template(template, asin):
    """
    Reduce a chapter filename template to the single filename segment
    `render_chapter_filename` renders, guaranteeing it contains `{ch}`.

    Two jobs, both of which must happen exactly once per book:

    1. A template carrying folder levels loses everything before the last '/',
       because this renders ONE filename segment and not a path. The strip is
       done up front rather than after rendering, which is what makes the `{ch}`
       guard below honest: checked against the whole template, "{ch}/{title}"
       would satisfy the guard and then have its `{ch}` thrown away with the
       directory level, rendering the identical name for every part of the book.
       Splitting the template is equivalent to splitting the rendered string
       because every substituted value has been through `_sanitize_filename`,
       which rewrites '/' to '_' — no value can introduce a separator.
    2. `{ch}` is mandatory in spirit: without it every part of a book renders the
       same name. A template that omits it gets " - {ch}" appended, with a
       warning naming the template the user actually set.

    Idempotent, which is the point of it being separate: a caller splitting a
    book normalizes once, logs the warning once, and passes the result to
    `render_chapter_filename` for each of the N parts — where this runs again,
    finds `{ch}` already present, and says nothing (backlog #37).
    """
    configured_template = template  # what the user set, for a legible warning
    template = template.split("/")[-1]

    if "{ch}" not in template:
        log.warning(
            f"NAMING ({asin}): Chapter filename template '{configured_template}' has no {{ch}} placeholder "
            "in its filename segment; appending ' - {ch}' so part filenames stay unique and sortable."
        )
        template = f"{template} - {{ch}}"

    return template


def render_chapter_filename(
    template,
    part_number,
    part_total,
    chapter_title,
    asin,
    author,
    title,
    narrator,
    publisher,
    series=None,
    series_sequence=None,
    release_date=None,
    language=None,
    truncate_subtitle=False,
):
    """
    Render ONE part's filename for a book split into per-chapter files.

    Returns a bare filename **stem**: no directory, no extension. That is the
    deliberate split of responsibilities with `build_base_output_path`, which
    returns the full "/data/.../Name.m4b" path for the single-file output — a
    caller that splits a book takes the directory and the extension from the base
    path and asks this function only for the name in between, so both halves of
    the name keep coming from one place.

    `part_number` is **1-BASED**: part 1 of 12 renders "01", part 12 renders "12".
    Note the deliberate contrast with `db.py`'s `replace_book_files`, whose
    `part_index` column is ZERO-BASED — a caller holding one list of parts must
    not feed the same loop variable to both.

    Placeholders: every book-level tag the book naming template supports
    ({author} {title} {narrator} {publisher} {asin} {series} {series_part} {year}
    {language}, with the same missing-value rules), plus three part-level ones:
      {ch}       the part's 1-based number, zero-padded to the width of the part
                 count — 9 parts render "1".."9", 10 parts render "01".."10",
                 150 parts render "001".."150".
      {ch_total} the part count, never padded.
      {ch_title} the chapter's own title, sanitized like any other tag.

    {ch} is mandatory in spirit: without it every part of a book would render the
    same name, so a template that omits it gets " - {ch}" appended (with a warning)
    rather than producing a collision. The result is cleaned by the same
    drop-segment pass the book filename uses, so illegal characters, missing tags
    and dangling separators are handled identically — and a '/' can never escape
    into a directory level, since only the final segment is kept.

    Pure function: no settings, no filesystem, no database. The template and the
    subtitle-trimming flag are passed in by the caller.
    """
    # Part numbering. A part count of 0/None would make the padding width
    # nonsensical, so normalize to at least one part; part_number is rendered as
    # given (1-based) so a caller that mis-numbers gets a visible wrong number
    # rather than a silently reordered file.
    total = max(int(part_total or 0), 1)
    width = len(str(total))
    index = int(part_number)

    # Reduce the template to its filename segment and guarantee {ch} is in it.
    # A caller rendering a whole book's parts normalizes once and passes the
    # result in, in which case this call is a no-op that logs nothing; a caller
    # handing over a raw template still gets the guard (and its warning) here.
    template = normalize_chapter_file_template(template, asin)

    values = _build_naming_values(
        asin,
        author,
        title,
        narrator,
        publisher,
        series=series,
        series_sequence=series_sequence,
        release_date=release_date,
        language=language,
        truncate_subtitle=truncate_subtitle,
    )
    values["{ch}"] = str(index).zfill(width)
    values["{ch_total}"] = str(total)
    values["{ch_title}"] = _sanitize_filename(chapter_title) if chapter_title else ""

    # The template was reduced to its filename segment above, so what comes back
    # is already one segment; the drop-segment cleanup then handles the same
    # illegal characters and dangling separators the book filename does.
    filename = _clean_name_segment(_apply_naming_values(template, values))

    if not filename:
        # Belt and braces. The guard above leaves a literal "{ch}" in every
        # template, and {ch} always renders at least one digit, which survives
        # _clean_name_segment — so this branch should be unreachable. It stays as
        # the last line of defence: emitting a nameless part file would be far
        # worse than an ugly one.
        filename = _clean_name_segment(f"{values['{title}']} - {values['{ch}']}")
        if not filename:
            filename = f"Unknown Title - {values['{ch}']}"

    return filename


def _effective_naming_names(row, settings):
    """
    The (author, title) the naming templates render for a book: the user's custom
    overrides when `naming.apply_custom_to_filenames` is on, the native Audible
    values otherwise, each with its "Unknown ..." fallback.

    One definition, three callers — PREPARE choosing the download's path, the
    rename that reconciles a metadata edit, and the sidecar-base lookup that has
    to re-derive a split book's single-file-equivalent name. They must agree, or
    a rename would move a book onto a name the downloader would never have
    chosen.
    """
    author = row["author"] or "Unknown Author"
    title = row["title"] or "Unknown Title"
    if settings.get("naming", {}).get("apply_custom_to_filenames", False):
        author = row["custom_author"] or author
        title = row["custom_title"] or title
    return author, title


def _split_folder_and_stem(base_root, collision_suffix, asin, quiet=False):
    """
    Where a split book's parts and sidecars go, from the extension-stripped root
    of its single-file-equivalent path. Returns `(folder, stem)` — the folder
    BEFORE any collision suffix is applied, and the single-file-equivalent stem
    the sidecars use (D9).

    Two rules, in this order:

    1. **D5 flat-template guard.** The parts normally share the folder the book
       naming template already produced, next to the sidecars. When that folder
       is the bare output root, a 40-chapter book would dump 40 files into
       /data, so it gets a subfolder named from the rendered single-file base.
    2. **D12 currency.** A split book reserves (and its sidecars use) the same
       single-file-equivalent base as an unsplit one, but it disambiguates a
       collision on its FOLDER rather than its filename: part names are rendered
       from the chapter template and carry none of the book base, so two
       same-title books splitting into one folder would collide file-for-file no
       matter what the reserved filename says. `collision_suffix` — the
       "_<asin>" the reservation appended, if any — is therefore stripped back
       off the stem here and applied to the folder by the caller, which is also
       the only place that can walk to the next candidate.

    `quiet` silences the guard's log line for callers that are only ASKING where
    a book's files would go rather than placing any: the annotations button's
    sidecar-base lookup runs this on every press, and announcing a placement
    that isn't happening into a user-downloadable app.log is just noise.

    Pure: no filesystem, no database, no settings.
    """
    stem = os.path.basename(base_root)
    folder = os.path.dirname(base_root)

    if collision_suffix and stem.endswith(collision_suffix):
        unsuffixed = stem[: -len(collision_suffix)]
        # A stem that is ONLY the suffix would leave the book nameless; keep the
        # suffixed spelling in that (unreachable in practice) case.
        if unsuffixed:
            stem = unsuffixed

    if os.path.abspath(folder) == os.path.abspath("/data"):
        folder = os.path.join(folder, stem)
        if not quiet:
            log.info(
                f"NAMING ({asin}): The naming template puts this book in the output root; "
                f"placing its chapter files in the '{stem}' subfolder instead."
            )

    return folder, stem


def sidecar_base_for_tracked_book(asin):
    """
    Where a tracked book's sidecars live, answered from the database and the
    disk rather than from a running conversion. Returns the extension-stripped
    base, or None when the book has no tracked path at all.

    The processor's own `_sidecar_base` can read this off the path it just
    reserved; anything that meets a book later (the on-demand annotations
    button) has only the DB row, where a split book's `filepath` is its FOLDER
    (D3) and the single-file-equivalent stem is not recorded anywhere. So for a
    split book the stem is re-rendered from the naming template exactly as the
    download would have rendered it, and the sidecars already in the folder win
    over that answer when they are both unambiguous and provably this book's
    (`_owned_sidecar_base`) — which is what keeps the button writing next to a
    book whose naming template changed after it was downloaded.

    An unrecognized base is NOT adopted: writing an annotations dump onto a base
    the folder's other occupant owns would both misfile the dump and make that
    foreign base look like ours forever after. The rendered answer is used
    instead, and only when even that can't be worked out does this return None —
    which the annotations route reports rather than guessing a path (M6).

    A single-file book is answered from its own path alone — no naming columns
    are even read — so the common case stays one narrow query.
    """
    with get_db_connection() as con:
        row = con.execute("SELECT filepath FROM audiobooks WHERE asin = ?", (asin,)).fetchone()
    if not row or not row["filepath"]:
        return None

    filepath = row["filepath"]
    parts = _tracked_part_paths(asin)
    if not parts:
        return os.path.splitext(filepath)[0]

    rendered_base = _rendered_split_sidecar_base(asin, filepath, parts)
    expected_stem = os.path.basename(rendered_base) if rendered_base else None
    # `quiet=True`: this is a read-only lookup behind the annotations button, so
    # a folder shared with another library manager must not announce its refusal
    # into app.log on every press (same rule as the D5 guard line above).
    return _owned_sidecar_base(filepath, expected_stem, quiet=True, split=True) or rendered_base


def _rendered_split_sidecar_base(asin, folder, part_paths):
    """
    The single-file-equivalent base for a split book with no sidecar on disk to
    read it off: re-render the book naming template exactly as the download
    would have, and take the stem into the folder the book is tracked at today
    (which is authoritative even if the template has changed since).

    Returns None if the book's metadata can't be read, leaving the caller to
    decide its own fallback — a cosmetic placement question must not raise.
    """
    settings = load_settings()
    try:
        with get_db_connection() as con:
            row = con.execute(
                "SELECT author, title, narrator, publisher, custom_title, custom_author, "
                "series, series_sequence, release_date, language "
                "FROM audiobooks WHERE asin = ?",
                (asin,),
            ).fetchone()
    except sqlite3.Error as e:
        log.warning(f"Could not read the naming metadata for {asin}: {e}")
        return None
    if not row:
        return None

    # The parts' own extension is the book's real format (an MP3 split book must
    # not be re-rendered as ".m4b" — the extension changes nothing about the stem
    # today, but it is what the template was rendered with).
    ext = os.path.splitext(part_paths[0])[1] or ".m4b"
    author, title = _effective_naming_names(row, settings)
    base_root = os.path.splitext(
        build_base_output_path(
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
    )[0]
    # quiet: this is a read-only lookup — it throws the folder away and keeps
    # only the stem, so the flat-template guard has nothing to announce.
    _folder, stem = _split_folder_and_stem(base_root, "", asin, quiet=True)
    return os.path.join(folder, stem)


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


def _first_free_suffixed_base(base, safe_asin, asin, own_base=None):
    """
    The first name in the collision sequence that nothing else holds:
    "<base>_<asin>", then "<base>_<asin>_2", "_3", ... — the same shape
    `import_logic._first_free_output_path` uses for uploads, in the
    extension-stripped currency the download/rename allocators reserve in.

    This is backlog #28: both allocators used to build "<base>_<asin>" and use it
    without re-checking anything, on the reasoning that an ASIN is unique. It
    isn't a name: a hand-crafted custom title, a second collision, or simply an
    existing file at that name would have been silently overwritten.

    "Held" means an in-flight reservation or an audio file on disk that belongs
    to another book. `own_base` is this book's current base, which never blocks
    it — a book already sitting at its suffixed name must re-derive that same
    name so the caller can recognize the no-op.

    MUST be called with `_reservation_lock` held (it reads the reservation set),
    which is also why the on-disk half never probes: see
    `_base_claimed_by_another_book`.
    """
    for attempt in range(1, _SUFFIX_WALK_LIMIT + 1):
        candidate = f"{base}_{safe_asin}" if attempt == 1 else f"{base}_{safe_asin}_{attempt}"
        if candidate == own_base:
            return candidate
        if candidate in _reserved_output_paths:
            continue
        if _base_claimed_by_another_book(candidate, asin):
            continue
        return candidate

    # Only reachable with 100 occupied "<base>_<asin>_N" names, i.e. a
    # deliberately hostile directory. Be honest about what this fallback is:
    # the FIRST candidate the walk rejected, handed back unprotected. Nothing
    # downstream re-checks it — `_reserve_output_path` ran `_existing_file_is_
    # foreign` against the UNsuffixed path before taking the lock and never
    # re-runs it here, and the promotion's `os.replace` is atomic but
    # destructive, not a refusal. So writing to this name can overwrite whatever
    # holds it, and the warning below is the only signal that happened.
    log.warning(
        f"NAMING ({asin}): Could not find a free name after {_SUFFIX_WALK_LIMIT} attempts at '{base}'; "
        f"falling back to the plain ASIN-suffixed name."
    )
    return f"{base}_{safe_asin}"


def _undo_moves(asin, moves):
    """
    Put a rename's moves back, best-effort: `moves` is the (source, destination)
    pairs that actually happened, and each is moved destination -> source again.

    Only used when the database write FAILS after the files have already moved.
    Leaving them where they landed would point the row at a location holding
    nothing, with no later step to reconcile it — the book reads as MISSING while
    its files sit intact somewhere else. So the files follow the database rather
    than the other way round. A put-back that itself fails is logged at ERROR and
    the remaining ones are still attempted: one stranded file beats all of them.
    """
    for source, destination in moves:
        try:
            shutil.move(destination, source)
        except OSError as e:
            log.error(f"RENAME ({asin}): Could not put '{destination}' back to '{source}': {e}")


def rename_book_to_match_metadata(asin):
    """
    When the apply_custom_to_filenames setting is on, rename a downloaded book's
    file (and its companion PDF) to match its current effective metadata.

    Returns the new path if a rename happened, else None. Collision-safe (never
    overwrites a different book — it appends the ASIN instead), and best-effort:
    any problem is logged rather than raised, so the metadata edit still stands.

    A book split into per-chapter files has no single file to rename, so it is
    handed to `_rename_split_book`, which moves the whole set.
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

    part_paths = _tracked_part_paths(asin)
    if part_paths:
        return _rename_split_book(asin, row, settings, part_paths)

    current_path = row["filepath"]
    if not os.path.exists(current_path):
        log.warning(f"RENAME ({asin}): Tracked file '{current_path}' is missing; skipping rename.")
        return None

    author, title = _effective_naming_names(row, settings)
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
        if _path_owner(candidate) != asin:
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
            # #28: the suffixed name is re-validated rather than assumed free —
            # "<base>_<asin>" can itself be taken (by a reservation, or by a file
            # on disk under any audio extension), in which case the walk moves on
            # to "_2", "_3", ... The book's OWN current path never blocks it.
            target_root = _first_free_suffixed_base(
                target_root, _sanitize_filename(asin), asin, own_base=os.path.splitext(current_path)[0]
            )
            target = f"{target_root}{target_ext}"
        reserved_base = target_root
        # Only release in the `finally` what this call actually claimed. The
        # suffixed target can already be reserved — by this same book's in-flight
        # force re-download, which hit its own collision and reserved the identical
        # "<base>_<asin>" — and a set add is silently a no-op there. Discarding it
        # afterwards would drop the downloader's live claim and re-open the window
        # for a third same-name book to take the base mid-download.
        claimed_reservation = reserved_base not in _reserved_output_paths
        if claimed_reservation:
            _reserved_output_paths.add(reserved_base)

    try:
        # The collision suffix can re-derive the name the book ALREADY has: a book
        # that collided at download time sits at "<base>_<asin>", and a later
        # metadata edit re-renders the same "<base>" target, finds the other book
        # still holding it, and appends the same ASIN. The equality check above
        # compared the UNsuffixed target, so only this one catches it. Bail before
        # touching anything: the move would be a self-rename, the log line would
        # claim a move that never happened, and — the reason this is a bug rather
        # than a cosmetic quirk — the is_duplicate write below would set the flag
        # again on a book whose duplicate the user just resolved with "Keep"
        # (routes.py clears the flag and then calls this function).
        if os.path.abspath(target) == os.path.abspath(current_path):
            return None
        # Move every sidecar sharing the old base name alongside the audiobook,
        # so a rename keeps the companion PDF, cover, cue sheet, metadata JSON,
        # and any retained raw master (+voucher) matched to the new file name.
        # Each sidecar keeps its own extension spelling (an uppercase ".JPG" stays
        # uppercase); only the base name changes.
        old_base = os.path.splitext(current_path)[0]
        new_base = os.path.splitext(target)[0]
        # ...but only when nothing ELSE still lives at the old base (#20). This is
        # the guard the stale-file cleanup has always had and this path never did:
        # two books can legitimately share one extension-stripped base under
        # different audio extensions (pre-existing libraries, or an upload), and
        # those sidecars may be the other book's ONLY cover/PDF/cue — walking off
        # with them is as destructive as deleting them.
        #
        # Asked BEFORE the first move, deliberately: it opens the database, and a
        # locked library.db raises out of a rename that has already moved files
        # — into the outer handler below, which logs and returns WITHOUT undoing
        # them, leaving the row naming a path that holds nothing. It reads
        # nothing the move changes (this book's own file is excluded either way),
        # so asking first costs nothing and takes the whole failure mode away.
        base_is_shared = _output_base_is_shared(asin, os.path.realpath(old_base), {os.path.realpath(current_path)})
        os.makedirs(os.path.dirname(target), exist_ok=True)
        shutil.move(current_path, target)
        moved_sidecars = []
        if base_is_shared:
            log.info(
                f"RENAME ({asin}): Left the sidecars at '{old_base}' where they are — "
                f"the base is still in use by another book."
            )
        else:
            for suffix in _existing_sidecar_suffixes(old_base):
                old_sidecar = f"{old_base}{suffix}"
                try:
                    shutil.move(old_sidecar, f"{new_base}{suffix}")
                    moved_sidecars.append((old_sidecar, f"{new_base}{suffix}"))
                except OSError as e:
                    log.warning(f"RENAME ({asin}): Could not move sidecar '{old_sidecar}': {e}")
        try:
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
        except sqlite3.Error as e:
            # The files are already at their new home and the row still names the
            # old one — the one state this function must never leave behind, since
            # nothing later reconciles it: the book reads as MISSING on the next
            # Verify while its files sit intact one directory away. Put the move
            # back so the database stays true, and say so at ERROR: unlike every
            # other warning here, this one describes work that was undone.
            log.error(
                f"RENAME ({asin}): Could not record the rename in the database: {e}. "
                f"Putting the file(s) back where the database says they are."
            )
            _undo_moves(asin, [(current_path, target), *moved_sidecars])
            return None
        log.info(f"RENAME ({asin}): Moved file to '{target}'.")
        _cleanup_empty_dirs(os.path.dirname(current_path))
        return target
    except (OSError, ValueError, sqlite3.Error) as e:
        # ValueError as well as OSError: a control character (a NUL byte in a
        # custom title survives _sanitize_filename) makes os.makedirs raise
        # "embedded null byte", and this is called after the metadata edit has
        # already been committed — it must never escape as a 500. A database
        # error (a locked library.db) is caught for the same reason.
        log.warning(f"RENAME ({asin}): Could not rename file(s): {e}")
        return None
    finally:
        # The claim only had to survive the move and the DB update; release it
        # either way so the name is available again immediately — but only if it
        # was ours to release (see the claim above).
        if claimed_reservation:
            with _reservation_lock:
                _reserved_output_paths.discard(reserved_base)


def _split_paths_are_claimed(paths, asin, owners, is_foreign=None):
    """
    True when any of `paths` is claimed by a book other than `asin`.

    `owners` is a `_tracked_path_owners()` map, so a path tracked under another
    ASIN — as that book's own file OR as one of its chapter parts — settles it
    immediately. An UNTRACKED file sitting exactly where one of these paths would
    go is judged by `is_foreign(path)` when the caller supplies one (the
    downloader can afford to read the embedded ASIN tag and recognize its own
    orphaned output); with no callable, an untracked occupant counts as claimed,
    which is the safe answer for a request thread that must not spawn ffprobe.
    """
    for path in paths:
        claimants = owners.get(os.path.realpath(path))
        if claimants:
            if claimants - {asin}:
                return True
            continue  # Our own file; not a claim against us.
        if not os.path.exists(path):
            continue
        if is_foreign is None or is_foreign(path):
            return True
    return False


def _rename_split_book(asin, row, settings, part_paths):
    """
    The multi-file form of `rename_book_to_match_metadata`: move a split book's
    whole set — folder, chapter files and sidecars — to match its current
    effective metadata, and update `audiobooks.filepath` and the `book_files`
    rows in ONE transaction so the two can never disagree.

    What is recomputed and what is not:

    - The **folder** and the **sidecar stem** are re-rendered from the naming
      template, exactly as a fresh download would render them (D5's flat-template
      guard included).
    - The **part filenames are kept as they are.** They were rendered from
      `naming.chapter_file_template` against each chapter's own title, and
      chapter titles are not persisted anywhere (D3 records paths, not chapters)
      — re-rendering them here would mean re-fetching the title's chapter list
      from Audible inside a metadata-edit request. Keeping the names is lossless:
      the book stays coherent, and a re-download re-renders everything.

    Collision handling follows D12: the reservation currency is still the
    single-file-equivalent base, but the disambiguating ASIN suffix goes on the
    FOLDER, because the part filenames carry none of that base and two same-title
    books would otherwise collide file-for-file.

    Best-effort like the single-file path: a failed move is rolled back, and a
    move that succeeds only for the database write to fail is rolled back too,
    so the rows and the files never end up describing different places.
    """
    current_folder = row["filepath"]
    if not os.path.isdir(current_folder):
        log.warning(f"RENAME ({asin}): Tracked folder '{current_folder}' is missing; skipping rename.")
        return None
    if not any(os.path.exists(path) for path in part_paths):
        log.warning(f"RENAME ({asin}): None of the tracked chapter files are on disk; skipping rename.")
        return None

    author, title = _effective_naming_names(row, settings)
    # The parts' own extension is the book's real format; build_base_output_path
    # would otherwise relabel an MP3 split book ".m4b".
    ext = os.path.splitext(part_paths[0])[1] or ".m4b"
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
    target_root, target_ext = os.path.splitext(target)
    folder_root, stem = _split_folder_and_stem(target_root, "", asin)

    # "Taken" is judged on the extension-stripped base for the same reason the
    # single-file path judges it there: that base is where this book's sidecars
    # will go, so a foreign book holding it under any audio extension collides.
    collision = False
    for candidate in [target] + _sibling_audio_paths(target_root, target_ext):
        if os.path.exists(candidate) and _path_owner(candidate) != asin:
            collision = True
            break

    owners = _tracked_path_owners()
    with _reservation_lock:
        if target_root in _reserved_output_paths:
            log.info(f"RENAME ({asin}): Target name is claimed by an in-flight book. Appending unique ID.")
            collision = True

        # D12: walk the FOLDER, not the filename. Attempt 0 is the plain folder,
        # which is skipped outright when the base already collided; every later
        # attempt is re-validated against the part paths it would produce (#28).
        safe_asin = _sanitize_filename(asin)
        new_folder = None
        for attempt in range(1 if collision else 0, _SUFFIX_WALK_LIMIT + 1):
            if attempt == 0:
                candidate_folder = folder_root
            elif attempt == 1:
                candidate_folder = f"{folder_root}_{safe_asin}"
            else:
                candidate_folder = f"{folder_root}_{safe_asin}_{attempt}"
            if os.path.abspath(candidate_folder) == os.path.abspath(current_folder):
                new_folder = candidate_folder  # Where the book already is.
                break
            # A folder an in-flight DOWNLOAD has already claimed is taken even
            # though nothing is on disk or in `book_files` yet. This is the
            # currency both allocators share: the download's own folder walk
            # (`_first_free_split_folder`) claims and checks the FOLDER path
            # too, because the filename base each of them reserves alongside it
            # spells a collision differently ("<folder>/<stem>_<asin>" during a
            # download, "<folder>_<asin>/<stem>" here) and so can never match.
            if candidate_folder in _reserved_output_paths:
                log.info(f"RENAME ({asin}): Folder '{candidate_folder}' is claimed by an in-flight book.")
                continue
            candidate_parts = [os.path.join(candidate_folder, os.path.basename(p)) for p in part_paths]
            if not _split_paths_are_claimed(candidate_parts, asin, owners):
                new_folder = candidate_folder
                if attempt:
                    collision = True
                break
        if new_folder is None:
            log.warning(f"RENAME ({asin}): Could not find a free folder for the chapter files; skipping rename.")
            return None

        # Two claims, released together: the FOLDER (the shared currency above)
        # and the single-file-equivalent base inside it, which is where this
        # book's sidecars land and what an unsplit book's reservation collides
        # with. Same rule as the single-file path — only release what we
        # actually took, since either can already be held by this book's own
        # in-flight re-download and a set add is silently a no-op there.
        reserved_base = os.path.join(new_folder, stem)
        claimed_reservations = [claim for claim in (new_folder, reserved_base) if claim not in _reserved_output_paths]
        _reserved_output_paths.update(claimed_reservations)

    try:
        # Only sidecars this book owns travel with it: `stem` is the name it
        # renders today, so an unchanged stem corroborates the folder's base
        # outright and a renamed one is corroborated by the app-written files
        # hanging off it. An uncorroborated base answers None and simply stays
        # where it is — the chapter files still move, and a foreign cover is left
        # in the old folder rather than walked off with.
        old_base = _owned_sidecar_base(current_folder, stem, split=True)
        new_base = os.path.join(new_folder, stem)
        folder_moved = os.path.abspath(new_folder) != os.path.abspath(current_folder)
        base_moved = old_base is not None and os.path.abspath(old_base) != os.path.abspath(new_base)
        if not folder_moved and not base_moved:
            return None  # Everything is already where the current metadata wants it.

        # The #20 guard's DB read, asked BEFORE the first move for the same
        # reason the single-file path asks it there: a locked library.db raises
        # out of this function's outer handler, which logs and returns without
        # undoing anything, so a read that sits BETWEEN the moves and the rows
        # can strand the whole set at the new location with the rows naming the
        # old one. Nothing it reads is changed by moving the files.
        base_is_shared = base_moved and _output_base_is_shared(
            asin, os.path.realpath(old_base), {os.path.realpath(p) for p in part_paths}
        )

        new_parts = list(part_paths)
        if folder_moved:
            os.makedirs(new_folder, exist_ok=True)
            new_parts = _move_split_parts(asin, part_paths, new_folder)
            if new_parts is None:
                return None  # The move failed and was rolled back; nothing changed.

        moved_sidecars = []
        if base_moved:
            # The #20 guard, same as the single-file path: another book's
            # sidecars may share this base, and they are not ours to move.
            if base_is_shared:
                log.info(
                    f"RENAME ({asin}): Left the sidecars at '{old_base}' where they are — "
                    f"the base is still in use by another book."
                )
            else:
                for suffix in _existing_sidecar_suffixes(old_base):
                    old_sidecar = f"{old_base}{suffix}"
                    try:
                        shutil.move(old_sidecar, f"{new_base}{suffix}")
                        moved_sidecars.append((old_sidecar, f"{new_base}{suffix}"))
                    except OSError as e:
                        log.warning(f"RENAME ({asin}): Could not move sidecar '{old_sidecar}': {e}")

        try:
            with get_db_connection() as con:
                # One transaction for the folder and the part rows (D3): a reader must
                # never see the book pointing at the new folder while its parts still
                # name the old one.
                con.execute(
                    "UPDATE audiobooks SET filepath = ?, is_duplicate = ? WHERE asin = ?",
                    (new_folder, int(collision), asin),
                )
                replace_book_files(asin, new_parts, con=con)
                con.commit()
        except sqlite3.Error as e:
            # Everything has already moved and the rows still name the old folder
            # — the divergence nothing downstream repairs (the book reads as
            # MISSING on the next Verify while N intact chapter files sit one
            # directory away). Undo the move so the database stays true; ERROR
            # rather than WARNING because this describes work being taken back.
            log.error(
                f"RENAME ({asin}): Could not record the move in the database: {e}. "
                f"Putting the chapter files and sidecars back where the database says they are."
            )
            _undo_moves(asin, moved_sidecars)
            if folder_moved:
                # The same rollback the move half already uses, run in reverse:
                # every part keeps its own name, so moving the new set back into
                # the old folder restores exactly what was there.
                _move_split_parts(asin, new_parts, current_folder)
                _cleanup_empty_dirs(new_folder)
            return None

        log.info(f"RENAME ({asin}): Moved {len(new_parts)} chapter file(s) to '{new_folder}'.")
        if folder_moved:
            _cleanup_empty_dirs(current_folder)
        return new_folder
    except (OSError, ValueError, sqlite3.Error) as e:
        log.warning(f"RENAME ({asin}): Could not rename the chapter files: {e}")
        return None
    finally:
        if claimed_reservations:
            with _reservation_lock:
                _reserved_output_paths.difference_update(claimed_reservations)


def _move_split_parts(asin, part_paths, new_folder):
    """
    Move every chapter file into `new_folder`, keeping each file's own name.
    Returns the new paths, or None when a move failed — in which case everything
    already moved is put back, so a partly-renamed book never reaches the
    database. A part that is missing from disk is carried over to the new list
    without a move: the row keeps pointing where the file WOULD be, which is
    where a repair download will write it.
    """
    moved = []
    new_parts = []
    try:
        for old_path in part_paths:
            new_path = os.path.join(new_folder, os.path.basename(old_path))
            if os.path.exists(old_path):
                shutil.move(old_path, new_path)
                moved.append((old_path, new_path))
            new_parts.append(new_path)
        return new_parts
    except OSError as e:
        log.warning(f"RENAME ({asin}): Could not move chapter file: {e}. Putting the moved files back.")
        for old_path, new_path in moved:
            try:
                shutil.move(new_path, old_path)
            except OSError as undo_error:
                log.error(f"RENAME ({asin}): Could not put '{new_path}' back to '{old_path}': {undo_error}")
        return None


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
    least two digits (three past track 99).

    The CUE format defines NO escape mechanism inside a quoted field, so unsafe
    characters can only be replaced, never escaped: a double quote becomes two
    single quotes (the convention other CUE writers use, and visually close to
    the original), and CR/LF — which would split a record in this line-oriented
    format — become a single space (a CRLF pair collapses to one space).
    """

    def _q(text):
        return (text or "").replace('"', "''").replace("\r\n", " ").replace("\r", " ").replace("\n", " ")

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
        # Per-chapter splitting (v0.24.0). Both stay empty/None for a normal
        # single-file book, and every finalize step below reads them as "is this
        # book split?" — so an unsplit run takes exactly the paths it always has.
        # `split_part_paths` is the book's output files in PLAYBACK order (which
        # is also the 0-based `book_files.part_index` order); `split_output_dir`
        # is the folder holding them, and what `audiobooks.filepath` records.
        self.split_part_paths = []
        self.split_output_dir = None
        # The single-file-equivalent base a split book's sidecars hang off (D9),
        # decided with the folder in _plan_split_output. Only the planner can
        # know it: a collision moves the ASIN suffix onto the folder, so the
        # reserved filename's stem is no longer the sidecar stem.
        self.split_sidecar_base = None
        # Which of this run's planned part paths were ALREADY on disk and already
        # tracked to this book when promotion started — i.e. the PREVIOUS
        # download's own chapter files, which a split->split re-download
        # deliberately overwrites in place (the folder walk subtracts this ASIN,
        # so a book's own parts never read as a collision). Filled in by
        # _promote_split_parts and read by every teardown below, which skip them:
        # an overwritten-but-valid file is strictly better than a deleted one,
        # and the `book_files` rows still name exactly those paths. Empty for a
        # first download, which is why a failed one still cleans up completely.
        self.preexisting_part_targets = set()
        # The folder claim this book took in `_reserved_output_paths` (D12/W3),
        # or None when it took none. A split book's parts are disambiguated by
        # their FOLDER, so the folder — not just the reserved filename base — is
        # what two concurrent allocators have to be able to see each other take;
        # recorded here so `run`'s release discards exactly what was added.
        self.split_folder_reservation = None
        # Set True when a same-author+title collision forced an ASIN suffix onto
        # our filename; persisted to the DB on success so the UI can flag it.
        self.is_duplicate = False
        # The suffix that collision actually appended ("_B00XYZ", or "_B00XYZ_2"
        # if even that was taken), empty when the name came out clean. A split
        # book moves it from the filename to the FOLDER (D12), which is the only
        # place a per-chapter set can be disambiguated.
        self.collision_suffix = ""
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
        # Set by `run`'s timeout handler BEFORE it reports the failure, so a late
        # step failure from the abandoned encode doesn't record its own ERROR on
        # top of it. A timed-out book's tasks are left running (see `run`), so
        # whenever they do fail they arrive with the stop_event unset — once for an
        # MP3 pass, once per in-flight chunk on the AAC path — and since the failure
        # write is also the only place retry_count is bumped, the book would burn
        # its whole automatic retry budget on a single timeout. An Event rather than
        # a bare bool because the writer is the waiting thread and the readers are
        # worker threads.
        self._timed_out = Event()
        # The general form of the same problem, covering every failure and not just
        # the timeout. A run has exactly one outcome, so it may write exactly one
        # ERROR row. When one chunk fails it reports and sets _completion_event;
        # `run` wakes immediately and its TemporaryDirectory context deletes the
        # book's temp dir while the other chunk tasks are still in flight (and more
        # sit queued in the global task runner). Each of those then fails too — its
        # ffmpeg can no longer read its input or write its output — and arrives at
        # _fail_or_cancel with BOTH guards above unset: nobody cancelled, nothing
        # timed out. Since the failure write is the only place retry_count is
        # bumped, a single failed attempt would land the counter at 2+ and push the
        # book past the `retry_count <= 1` auto-retry gate, while whichever late
        # chunk reported last decided the error_message the user sees. The first
        # report claims this latch; every later one logs and returns.
        #
        # Its own lock, not self._lock: a chunk reports its failure while already
        # holding self._lock (see _encode_and_track_chunk), and Lock is not
        # reentrant. A lock rather than an Event because this claim is a real
        # test-and-set raced between worker threads, not a one-writer flag.
        self._failure_reported = False
        self._failure_report_lock = Lock()

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
                # #28: the suffixed name is re-validated instead of assumed free.
                # "<base>_<asin>" can be reserved by this book's own in-flight run
                # or occupied on disk by something that isn't ours, in which case
                # the walk moves on to "_2", "_3", ...
                final_base = _first_free_suffixed_base(base, safe_asin, self.asin)
                final_path = f"{final_base}{ext}"
                # What the suffix actually is, for _plan_split_output: a SPLIT
                # book disambiguates on its folder, not its filename (D12), so it
                # has to be able to take this back off the stem.
                self.collision_suffix = final_base[len(base) :]
            else:
                final_path = base_output_path
                final_base = base

            _reserved_output_paths.add(final_base)

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
          that actually bounds a single-pass encode. A split MP3 book (v0.24.0)
          keys off the same format and so takes the same budget, even though its
          work is N parallel LAME encodes rather than one pass — it finishes
          sooner than the single pass the multiple was sized for, so the budget
          only gets more generous, never tighter.

        The MP3 budget takes the LARGER of the runtime model and the old estimator
        model, so the change can only ever lengthen the grace period. The runtime
        model alone is the tighter of the two once the recorded AAC rate passes
        45 s/min (0.75x real time) — exactly the slow arm64 hardware this timeout
        was widened for, which is the one machine class that must not lose time.

        A missing or zero runtime leaves nothing to scale, so both paths fall back
        to the floor.
        """
        with get_db_connection() as con:
            runtime_row = con.execute("SELECT runtime_min FROM audiobooks WHERE asin = ?", (self.asin,)).fetchone()
        runtime_min = runtime_row["runtime_min"] if runtime_row else None
        if not runtime_min or runtime_min <= 0:
            return _COMPLETION_TIMEOUT_FLOOR_SEC

        estimate_budget = 4 * estimate_conversion_time(runtime_min)
        if resolve_output_format(load_settings()) == "mp3":
            budget = max(int(runtime_min * 60 * _MP3_TIMEOUT_RUNTIME_MULTIPLE), estimate_budget)
        else:
            budget = estimate_budget
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
                    # above.
                    #
                    # This book's subprocesses are deliberately left running, so an
                    # abandoned ffmpeg keeps burning CPU (and holding the unlinked
                    # temp files' disk space) until it exits on its own. That is
                    # pre-existing behavior, and it stays that way because the
                    # process registry is keyed by JOB, not by book: SIGTERMing the
                    # job would also kill a concurrent book's perfectly healthy
                    # download (max_parallel_downloads defaults to 2). Narrowing
                    # the kill to this book's own processes needs per-book process
                    # tracking, deferred to backlog #19.
                    #
                    # Flag the timeout before raising, so a late step failure from
                    # that still-running encode doesn't also write an ERROR row
                    # (see _fail_or_cancel and self._timed_out). A step that
                    # failed just BEFORE this flag went up may already have
                    # claimed the report — that is fine, it names the real cause
                    # and the write still happens exactly once.
                    self._timed_out.set()
                    raise RuntimeError("Processing timed out.")
        except Exception as e:
            log.error(f"PROCESSOR ({self.asin}): A critical error occurred in the processor run: {e}", exc_info=True)
            self._update_db_on_failure(f"A critical error occurred: {e}")
        finally:
            # Release our claimed output name so it is available again (e.g. for
            # a later re-download of this same book). Reservations are keyed by
            # the extension-stripped base, so release the same way — plus the
            # split book's folder claim, which is a second entry in the same set
            # and would otherwise keep every re-download of this book walking to
            # a suffixed folder for the life of the process.
            with _reservation_lock:
                if self.final_output_path:
                    _reserved_output_paths.discard(os.path.splitext(self.final_output_path)[0])
                if self.split_folder_reservation:
                    _reserved_output_paths.discard(self.split_folder_reservation)
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
            # opted in; otherwise names come from the native Audible values. The
            # rule lives in _effective_naming_names so the rename path (and the
            # sidecar-base lookup) can never decide it differently.
            author, title = _effective_naming_names(book_details, settings)

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
        #
        # ...unless this book is being split (v0.24.0 Phase 3), which overrides
        # the first two: a split book has no single file to remux or to encode in
        # one pass, so all three formats route through the chunk fan-out below
        # and differ only in how each chunk is cut (the context's
        # "split_encode_mode"). The chunks then finalize in place rather than
        # merging, exactly as the AAC split already does.
        master_is_aac = str(self.context.get("audio_file", "")).lower().endswith(".m4b")
        split_output = bool(self.context.get("split_output"))
        if fmt == "original" and master_is_aac and not split_output:
            log.info(f"TASK-PREPARE ({self.asin}): Original format — skipping encode, submitting remux task.")
            _yield_progress(self.asin, "Finalizing (lossless)...", 90, self.job_id)
            remux_task = Task(
                priority=TaskPriority.MERGE_BOOK,
                job_id=self.job_id,
                func=self._remux_and_finalize,
            )
            task_runner.submit_task(remux_task)
            return
        if fmt == "mp3" and not split_output:
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
        # A lossless title reaching the fan-out has one of two reasons, and only
        # the FLAC one is a fallback worth reporting: a split lossless book takes
        # this path deliberately, cutting its parts with "-c copy" and never
        # re-encoding anything.
        if fmt == "original" and not master_is_aac:
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
            #
            # The message names the observable ("came out zero-length") rather than
            # the step: "chapter cleanups" is the UI's label for the OPTIONAL
            # transform toggles, and a message phrased around those sends the user
            # off to disable settings that may have had nothing to do with it — the
            # zero-length drop is unconditional.
            log.warning(f"TASK-PREPARE ({self.asin}): No usable chapters after chapter processing. Cannot process.")
            self._update_db_on_failure(
                "Book has no usable chapters: the title reported none, or every chapter came out "
                "zero-length and was dropped (see the log for which)."
            )
            self._completion_event.set()
            return

        # Per-chapter splitting (v0.24.0): prepare has already made the decision
        # (see its D6/D7 gate) and reshaped the chapter list accordingly; what is
        # left is turning that into concrete per-part output paths. It happens
        # here, once, BEFORE any chunk is queued — a book that has nowhere to put
        # its parts should fail now and not after N encodes.
        if self.context.get("split_output"):
            try:
                self._plan_split_output(settings, book_details, author, title, chapters)
            except Exception as e:
                # `Exception`, matching the reservation block above, because the
                # realistic escapes are not the filesystem ones: the folder walk
                # reads the ownership map and ffprobes each occupant, so a locked
                # library.db raises sqlite3.Error, and a hand-edited
                # `chapter_file_template` holding a JSON object raises
                # AttributeError. Neither is an OSError, and anything that
                # escapes here is swallowed by the task runner — leaving the
                # completion event unset and `run` blocked on this book for the
                # full two-hour completion timeout, holding a download slot, to
                # report a timeout for work that never started.
                log.error(
                    f"TASK-PREPARE ({self.asin}): Could not prepare the per-chapter file paths: {e}", exc_info=True
                )
                try:
                    self._update_db_on_failure("Failed to prepare the per-chapter file paths.")
                finally:
                    # The failure write re-raises on a locked database — the very
                    # cause that brought us here — so the event is set in a
                    # `finally`: an unreported failure is recoverable, a book that
                    # never unblocks `run` is not.
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

    def _plan_split_output(self, settings, book_details, author, title, chapters):
        """
        Turn prepare's split decision into this book's concrete per-part output
        paths, recorded on the processor for the encode and finalize steps.

        Placement (D5): the parts go in the folder the book naming template
        already produced — the directory of the reserved single-file path — so a
        split book sits exactly where the unsplit one would have, next to its
        sidecars. The one exception is the flat-template guard: when that folder
        resolves to the bare output root, a book with 40 chapters would dump 40
        files straight into /data, so the parts get a subfolder named from the
        rendered single-file base instead.

        Naming (D4): each part's name comes from `naming.chapter_file_template`,
        rendered by `render_chapter_filename` with this book's naming values. The
        template is normalized ONCE here rather than per part — its missing-{ch}
        guard logs a warning, and rendering N parts from a raw template would log
        it N times (backlog #37).

        Collisions (D12): the reserved single-file path may already carry an ASIN
        suffix on its FILENAME, which does nothing for a split book — the parts
        are named from the chapter template and carry none of that base, so two
        same-title books would still write the identical part names into the
        identical folder. `_split_folder_and_stem` therefore takes the suffix
        back off the stem, and the folder walk below puts it on the folder,
        re-validating each candidate against the part paths it would produce
        (#28) rather than assuming the first one is free.

        Note the two index bases in the loop below: `{ch}` is 1-based (part 1 of
        12 renders "01"), while the list position that becomes
        `book_files.part_index` is 0-based. They are deliberately not the same
        number, so they are not the same variable.
        """
        naming = settings.get("naming", {})
        # `or` rather than a plain .get default: a cleared UI field saves "" and a
        # hand-edited settings.json can hold null, and neither is a template.
        template = normalize_chapter_file_template(
            naming.get("chapter_file_template") or "{title} - {ch} - {ch_title}", self.asin
        )

        # The reserved single-file path decides WHERE the parts live — its
        # directory is the book's folder — but not what they are called at the
        # end. Its extension came from a load_settings() taken before the
        # download started, while prepare_book_assets took its own read once the
        # download and decrypt had finished and picked the container each part is
        # actually encoded into ("split_encode_mode"). A user flipping Output
        # Format mid-download would otherwise get LAME audio in files named
        # ".m4b" (or the reverse) — mislabeled files the scanner and the UI both
        # take at face value. So derive the extension from prepare's decision,
        # using the same expression encode_chapter_chunk names its chunks with:
        # container, chunk extension and part filename then agree by
        # construction, and _promote_split_parts' AtomicParsley skip (which keys
        # off the chunk extension) stays correct in both flip directions.
        base_root = os.path.splitext(self.final_output_path)[0]
        ext = ".mp3" if (self.context.get("split_encode_mode") or "aac") == "mp3" else ".m4b"
        folder_root, base_stem = _split_folder_and_stem(base_root, self.collision_suffix, self.asin)

        total = len(chapters)
        part_stems = []
        for index, chapter in enumerate(chapters):
            stem = render_chapter_filename(
                template,
                part_number=index + 1,
                part_total=total,
                chapter_title=chapter.get("title", ""),
                asin=self.asin,
                author=author,
                title=title,
                narrator=book_details["narrator"],
                publisher=book_details["publisher"],
                series=book_details["series"],
                series_sequence=book_details["series_sequence"],
                release_date=book_details["release_date"],
                language=book_details["language"],
                truncate_subtitle=naming.get("truncate_subtitle", False),
            )
            part_stems.append(stem)

        folder = self._first_free_split_folder(folder_root, part_stems, ext)
        paths = [os.path.join(folder, f"{stem}{ext}") for stem in part_stems]

        os.makedirs(folder, exist_ok=True)
        self.split_output_dir = folder
        self.split_part_paths = paths
        # The sidecars follow the parts into whichever folder was free, keeping
        # the single-file-equivalent stem (D9). Held on the processor because
        # nothing else can re-derive it: the folder may carry a collision suffix
        # the stem does not.
        self.split_sidecar_base = os.path.join(folder, base_stem)
        log.info(f"TASK-PREPARE ({self.asin}): Will write {total} chapter file(s) into '{folder}'.")

    def _first_free_split_folder(self, folder_root, part_stems, ext):
        """
        The folder this book's chapter files can safely be written into:
        `folder_root`, and if another book already claims the part paths that
        would produce, "<folder_root>_<asin>", "<folder_root>_<asin>_2", ...
        (D12 — a split book disambiguates on its folder).

        The walk starts one step in when the reservation already found a
        collision on the single-file-equivalent base, because that suffix is
        exactly what this is applying; otherwise the plain folder is tried first,
        which is what every ordinary book gets.

        "Claimed" is judged per planned part path, not per folder: a naming
        template that gives every book of an author the same folder is normal,
        and the parts of two DIFFERENT titles sitting in it collide with nothing.

        ...but "claimed" also means an in-flight reservation, and the FOLDER is
        the only currency that can carry it. Two books whose naming template
        renders different single-file bases can still render the SAME folder
        ("{author}/{title}/{title} - {narrator}" and two editions of one title),
        so neither one's filename reservation sees the other while their
        identically-named parts would land on top of each other. The chosen
        folder is therefore claimed in `_reserved_output_paths` alongside the
        filename base, in the same spelling `_rename_split_book` checks and
        claims — one currency, both allocators.
        """
        owners = _tracked_path_owners()
        safe_asin = _sanitize_filename(self.asin)
        # This book's OWN filename reservation, which is never a claim against
        # it. The two are the same string whenever D5's flat guard fires: the
        # reserved base is "/data/Dracula" and the invented folder is
        # "/data/Dracula" too, so without this every flat-template split book
        # would collide with itself and walk straight to a suffixed folder.
        own_reservation = os.path.splitext(self.final_output_path)[0]
        for attempt in range(1 if self.collision_suffix else 0, _SUFFIX_WALK_LIMIT + 1):
            if attempt == 0:
                candidate = folder_root
            elif attempt == 1:
                candidate = f"{folder_root}_{safe_asin}"
            else:
                candidate = f"{folder_root}_{safe_asin}_{attempt}"
            ours_already = candidate == own_reservation
            # The cheap test first, and on its own: `_split_paths_are_claimed`
            # below may run an ffprobe per untracked occupant, and holding the
            # global reservation lock across a subprocess would serialize every
            # other book's PREPARE (the same reasoning `_reserve_output_path`
            # states for doing its disk/DB work outside the lock).
            with _reservation_lock:
                already_claimed = not ours_already and candidate in _reserved_output_paths
            if already_claimed:
                log.info(
                    f"TASK-PREPARE ({self.asin}): Folder '{candidate}' is claimed by another in-flight "
                    f"book; trying the next folder name."
                )
                continue
            paths = [os.path.join(candidate, f"{stem}{ext}") for stem in part_stems]
            if _split_paths_are_claimed(paths, self.asin, owners, is_foreign=self._existing_file_is_foreign):
                log.info(
                    f"TASK-PREPARE ({self.asin}): Chapter files would collide with another book's in "
                    f"'{candidate}'; trying the next folder name."
                )
                continue
            # Nothing to take when the folder IS our filename reservation —
            # `run` already releases that one, and claiming it again would
            # record a second copy of the same string.
            if not ours_already and not self._claim_split_folder(candidate):
                # Another book took it while we were probing — the check above
                # is a fast path, this is the atomic one.
                log.info(
                    f"TASK-PREPARE ({self.asin}): Folder '{candidate}' was claimed while it was being "
                    f"checked; trying the next folder name."
                )
                continue
            if attempt:
                # The book needed a unique name after all, whatever the filename
                # reservation concluded — the UI's duplicate badge follows this.
                self.is_duplicate = True
            return candidate

        log.warning(
            f"TASK-PREPARE ({self.asin}): Could not find a free folder for the chapter files after "
            f"{_SUFFIX_WALK_LIMIT} attempts; using '{folder_root}_{safe_asin}'."
        )
        self.is_duplicate = True
        fallback = f"{folder_root}_{safe_asin}"
        # Best-effort: if the fallback is already someone's claim there is
        # nothing left to fall back TO, and this book writes there regardless.
        self._claim_split_folder(fallback)
        return fallback

    def _claim_split_folder(self, folder):
        """
        Take `folder` in the shared reservation set, atomically. True when the
        claim is now ours (and recorded for `run` to release), False when
        another book already held it — membership is tested before the add,
        because `set.add` on an existing entry is a silent no-op that would read
        as success and hand us someone else's folder.
        """
        with _reservation_lock:
            if folder in _reserved_output_paths:
                return False
            _reserved_output_paths.add(folder)
        self.split_folder_reservation = folder
        return True

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

            # If this was the last chunk to be processed, spawn the final task.
            # A split book takes the same slot at the same MERGE_BOOK priority,
            # but finalizes the chunks in place instead of merging them: they are
            # already the book's output files.
            if self.completed_chunks == self.total_chunks:
                if self.split_part_paths:
                    log.info(f"PROCESSOR ({self.asin}): All chapter files encoded. Submitting finalize task.")
                    final_func = self._finalize_split
                else:
                    log.info(f"PROCESSOR ({self.asin}): All chunks encoded. Submitting final merge task.")
                    final_func = self._merge_and_finalize
                merge_task = Task(
                    priority=TaskPriority.MERGE_BOOK,
                    job_id=self.job_id,
                    func=final_func,
                )
                task_runner.submit_task(merge_task)

    def _produced_output_paths(self):
        """
        Every audio file this run actually wrote into the library: the N chapter
        files of a split book, or the single audiobook otherwise. The one place
        the rest of the finalizer asks "what did we produce", so the failure
        cleanup and the timestamp sweep can't disagree about it.
        """
        if self.split_part_paths:
            return list(self.split_part_paths)
        return [self.final_output_path] if self.final_output_path else []

    def _prune_empty_split_dir(self):
        """
        Remove a split book's output folder once a failure has taken every part
        back out of it, so a run that produced nothing leaves nothing.

        _plan_split_output creates the folder before the first chunk is even
        queued, which means a failed encode, a cancelled one, a rolled-back
        promotion or a discarded post-timeout set all leave an empty directory
        sitting in the library — most visibly in D5's flat-template case, where
        that folder is a level that did not exist before this run. The four
        callers cover exactly those: _fail_or_cancel (every failed or cancelled
        step, including a chunk that never got to encode), the verification
        failure, the promotion rollback and the post-timeout discard.

        Exactly ONE directory, and deliberately not `_cleanup_empty_dirs`: that
        helper walks every empty parent up to /data, which is right when a naming
        change has emptied whole levels the app itself created, but wrong here.
        This runs on every cancelled split download, and the levels above the
        book's folder are the user's — a cancel in "/data/Author/Series/Book"
        must not take "Series" and "Author" with it just because they happened to
        hold nothing else yet. A run only ever creates this one folder, so this
        one folder is all it may remove.

        rmdir rather than any recursive removal, so a folder holding anything at
        all (another book's files, a stray sidecar, a part this run did not put
        there) is left exactly as it is.

        Unsplit runs never had a folder of their own to remove and are untouched.
        """
        if not (self.split_part_paths and self.split_output_dir):
            return
        try:
            os.rmdir(self.split_output_dir)  # only succeeds when empty
            log.info(f"PROCESSOR ({self.asin}): Removed the empty output folder '{self.split_output_dir}'.")
        except OSError:
            pass

    def _tracked_filepath(self):
        """
        What `audiobooks.filepath` records for this book. A split book has no
        single file, so the column holds its FOLDER (D3) and the authoritative
        per-file list lives in `book_files`; everything else records the
        audiobook itself, exactly as it always has.
        """
        if self.split_part_paths:
            return self.split_output_dir
        return self.final_output_path

    def _sidecar_base(self):
        """
        The extension-stripped base every sidecar hangs off.

        For a single-file book that is the audiobook's own path without its
        extension — today's behavior, unchanged. A split book has no such file,
        so its sidecars keep the single-file-EQUIVALENT name (D9) and sit inside
        the book's folder. That differs from the plain answer in two cases: D5's
        flat-template guard invents a subfolder, and D12 moves a collision's ASIN
        suffix from the filename onto the folder. `_plan_split_output` settles
        both when it settles the folder, so its answer is used verbatim when it
        is there; the fallback keeps working for a processor whose parts were set
        directly (the finalize tests do exactly that).
        """
        base = os.path.splitext(self.final_output_path)[0]
        if self.split_part_paths and self.split_sidecar_base:
            return self.split_sidecar_base
        if self.split_part_paths and self.split_output_dir:
            return os.path.join(self.split_output_dir, os.path.basename(base))
        return base

    def _verify_output_file(self):
        """
        Validate the finished file before we claim success, so a book is never
        marked DOWNLOADED while its file is missing, empty, or truncated (the
        "ghost book" and silent-truncation cases). Returns (ok, reason).

        A split book has N files instead of one and is checked by
        _verify_split_output_files, which applies the same two tests across the
        whole set.
        """
        if self.split_part_paths:
            return self._verify_split_output_files()

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

    def _verify_split_output_files(self):
        """
        The multi-file form of _verify_output_file (D10): every part must exist
        and clear a size floor, and the parts' durations must SUM to the book's
        expected runtime under the same 95%/10-minute tolerance the single-file
        check uses. One missing or truncated part fails the whole BOOK — and it
        fails it here, before any row is written, so a split book is never
        recorded DOWNLOADED with a hole in it.

        The size floor is per part and duration-aware. _MIN_OUTPUT_BYTES was
        chosen against a whole audiobook; a single chapter can legitimately be
        tiny, since the D6 merge only folds chapters shorter than
        `minimum_file_duration` (3 seconds by default) and three seconds of
        128 kbps AAC is well under 64 KiB. Holding a short part to the whole-book
        floor would fail perfectly good books, so short parts get a floor that
        still catches what this check is FOR: the header-only file ffmpeg writes
        when it encoded no audio at all.

        The duration test runs at both scales, and the reason is that floor's
        blind spot: a part the merge left under a minute only has to clear 4 KiB,
        and a summed check cannot see one 45-second chapter that came out 2
        seconds long. So each probed part is also held against its OWN chapter
        length before the total is compared, which is what makes D10's "one
        truncated part fails the book" literally true.
        """
        chapters = (self.context or {}).get("chapters") or []
        total = len(self.split_part_paths)

        for index, path in enumerate(self.split_part_paths):
            if not os.path.exists(path):
                return False, (
                    f"Chapter file {index + 1} of {total} is missing; the conversion reported success but "
                    f"did not produce every file."
                )
            length_ms = chapters[index].get("length_ms", 0) if index < len(chapters) else 0
            floor = _MIN_OUTPUT_BYTES if length_ms >= _FULL_SIZE_FLOOR_PART_MS else _MIN_PART_BYTES
            size = os.path.getsize(path)
            if size < floor:
                return False, (
                    f"Chapter file {index + 1} of {total} is implausibly small ({size} bytes); "
                    f"the conversion likely failed."
                )

        with get_db_connection() as con:
            row = con.execute("SELECT runtime_min FROM audiobooks WHERE asin = ?", (self.asin,)).fetchone()
        expected_min = row["runtime_min"] if row else None
        if expected_min and expected_min > 0:
            actual_sec = 0.0
            for index, path in enumerate(self.split_part_paths):
                part_sec = _probe_duration_seconds(path, self.job_id)
                if part_sec is None:
                    return False, (
                        f"Chapter file {index + 1} of {total} could not be read back (corrupt or unreadable)."
                    )
                actual_sec += part_sec
                # Per-part truncation, deliberately LENIENT. A part is never
                # expected to match its chapter to the sample: the encoder's
                # frame size and AAC priming shift the boundaries by fractions of
                # a second, and the D6 chapter transforms (outro trim, minimum-
                # duration merge) are already reflected in the length_ms this
                # compares against. Five percent plus a five-second grace lets all
                # of that through while still catching the real failure — a part
                # that came out a fraction of its chapter. Chapters missing a
                # length (an odd title, or a chapter list shorter than the part
                # list) simply skip the test rather than guess.
                expected_part_sec = (chapters[index].get("length_ms") or 0) / 1000.0 if index < len(chapters) else 0
                if expected_part_sec > 0 and part_sec < expected_part_sec * 0.95 - 5:
                    return False, (
                        f"Chapter file {index + 1} of {total} is truncated "
                        f"(expected ~{int(expected_part_sec)}s, got {int(part_sec)}s)."
                    )
            expected_sec = expected_min * 60
            if actual_sec < expected_sec * 0.95 and (expected_sec - actual_sec) > 600:
                return False, (
                    f"The {total} chapter files are truncated "
                    f"(expected ~{expected_min}m in total, got {int(actual_sec / 60)}m)."
                )

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
        pdf_target = f"{self._sidecar_base()}.pdf"
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
        base = self._sidecar_base()

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
        #    Skipped for a split book (D9): a cue sheet describes the internal
        #    track layout of ONE audio file, and a split book has no such file —
        #    every chapter is already its own file, so a single-FILE cue would
        #    name an arbitrary part and then describe a timeline it doesn't have.
        if conv.get("create_cue_sheet", False) and self.split_part_paths:
            log.info(
                f"PROCESSOR ({self.asin}): Skipping the cue sheet — this book was split into per-chapter files, "
                f"which a single-file cue sheet cannot describe."
            )
        elif conv.get("create_cue_sheet", False):
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

        # 5. Raw annotations JSON (the listener's own clips, notes and bookmarks)
        #    fetched during download. Most titles have none, in which case prepare
        #    leaves the context key None and there is simply nothing to place —
        #    the setting being on is not a promise that a sidecar appears.
        if conv.get("save_annotations", False):
            annotations_file = context.get("annotations_file")
            if annotations_file and os.path.exists(annotations_file):
                annotations_target = base + ".annotations.json"
                try:
                    shutil.copy2(annotations_file, annotations_target)
                    log.info(f"PROCESSOR ({self.asin}): Saved annotations to {annotations_target}")
                except OSError as e:
                    log.warning(f"PROCESSOR ({self.asin}): Could not save annotations: {e}")

    def _apply_file_timestamps(self):
        """
        Stamp the finished audiobook and its sidecars with the book's release or
        purchase date, when `conversion.file_timestamp_source` asks for it. Off
        ("none") by default, so a default install leaves the real creation time
        alone. Best-effort like _place_sidecar_files: a missing or unparseable
        date is skipped silently and a utime failure is logged, never fatal —
        a cosmetic timestamp must not turn a finished book into an error.
        """
        book_info = (self.context or {}).get("book_info") or {}
        source, timestamp = resolve_configured_file_timestamp(book_info)
        if source is None:
            return
        if timestamp is None:
            log.debug(f"PROCESSOR ({self.asin}): No usable {source} for file timestamps; leaving them as-is.")
            return

        # Both atime and mtime, so the pair stays consistent for tools that sort
        # on either. Sidecars only exist when their setting produced them, and are
        # matched however they are spelled on disk (an Audible cover saved as
        # ".JPG" must be stamped like a ".jpg" one). A split book stamps all of
        # its chapter files, not just one.
        base = self._sidecar_base()
        targets = self._produced_output_paths() + [f"{base}{suffix}" for suffix in _existing_sidecar_suffixes(base)]
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
            # A split book removes ALL of its parts, for the same reason —
            # including any the previous download owned, which the promotion
            # rollback deliberately spares. The difference is that verification
            # is only ever reached once EVERY target was replaced, so no prior
            # copy survives at any of them: what is on disk is this run's failed
            # output either way, and leaving a known-bad set at tracked paths is
            # worse than an ERROR row with the files gone. The cost — part rows
            # left naming files that are no longer there, until the retry — is
            # recorded as backlog #51.
            for produced_path in self._produced_output_paths():
                if not os.path.exists(produced_path):
                    continue
                try:
                    os.remove(produced_path)
                    log.info(f"PROCESSOR ({self.asin}): Removed failed output file {produced_path}.")
                except OSError as e:
                    log.warning(f"PROCESSOR ({self.asin}): Could not remove failed output file: {e}")
            self._prune_empty_split_dir()
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
            # ...and the same for the previous download's PART list, which the
            # replace below is about to overwrite. For a split book those rows
            # are the only record of the individual files; `previous_path` names
            # only their folder, which the cleanup must never delete as a file.
            try:
                previous_parts = [
                    row["filepath"]
                    for row in con.execute(
                        "SELECT filepath FROM book_files WHERE asin = ? ORDER BY part_index", (self.asin,)
                    ).fetchall()
                    if row["filepath"]
                ]
            except sqlite3.Error as e:
                # A database with no child table (a hand-restored library.db)
                # must not fail a finished download; it simply has no part rows.
                log.warning(f"PROCESSOR ({self.asin}): Could not read the previous per-chapter file rows: {e}")
                previous_parts = []
            con.execute(
                "UPDATE audiobooks SET status = 'DOWNLOADED', filepath = ?, "
                "error_message = '', retry_count = 0, is_duplicate = ? WHERE asin = ?",
                (self._tracked_filepath(), int(self.is_duplicate), self.asin),
            )
            if self.split_part_paths:
                # The book's per-file rows go in the SAME transaction as the row
                # above (D3/D10), so the two can never be observed disagreeing
                # about whether this book is split. A failure here propagates:
                # part rows are the only record of where a split book's files
                # are, and a book without them is not really downloaded.
                replace_book_files(self.asin, self.split_part_paths, con=con)
            else:
                # The mirror case: a book that WAS split and has just been
                # re-downloaded as a single file must lose its old part rows,
                # since their presence is what marks a book as split. Tolerated
                # for exactly ONE cause — a database missing the child table
                # (never true after start.sh runs, but a hand-restored library.db
                # is a real thing), which must not turn a finished download into
                # an error.
                #
                # Narrowly, and this is the point: a locked database is also an
                # sqlite3.Error, and swallowing THAT would commit the parent row
                # (this `with` block is the transaction) while the book's old
                # part rows survive — a single-file book that every reader sees
                # as an N-part split, whose parts _cleanup_stale_files is about
                # to delete. Letting it propagate rolls the whole transaction
                # back, which is the same treatment the split branch's write gets.
                try:
                    replace_book_files(self.asin, [], con=con)
                except sqlite3.OperationalError as e:
                    if "no such table" not in str(e).lower():
                        raise
                    log.warning(f"PROCESSOR ({self.asin}): Could not clear stale per-chapter file rows: {e}")
        # Everything below this line is housekeeping around a book that is
        # ALREADY a success: the output is verified and the DOWNLOADED row is
        # committed. So each step is independently non-fatal (#29) — an exception
        # escaping here used to leave the completion event unset, `run` waiting
        # out its full timeout, and the book finally recorded ERROR with its
        # perfectly good files sitting on disk (and, worse, the previous download
        # already deleted).
        #
        # Place any companion PDF and optional sidecars before the temp dir is
        # torn down (the raw master, cover, and metadata all live there).
        self._post_success_step("saving the companion PDF", self._place_supplementary_pdf)
        self._post_success_step("saving the sidecar files", self._place_sidecar_files)
        # Stamp timestamps last, once every file that shares the base name exists.
        self._post_success_step("stamping the file timestamps", self._apply_file_timestamps)
        # Only now that this run's own output is fully in place is it safe to
        # remove what the previous download left somewhere else.
        self._post_success_step(
            "cleaning up the previous download", self._cleanup_stale_files, previous_path, previous_parts
        )
        _yield_progress(self.asin, "Complete!", 100, self.job_id)

    def _post_success_step(self, description, step, *args):
        """
        Run one post-success housekeeping step, swallowing anything it raises.

        Every step below the DOWNLOADED write is optional decoration around a
        book that already succeeded — sidecars, timestamps, cleanup of the
        previous download. `Exception` deliberately, not a curated tuple: the
        point is that NOTHING here can undo a success, and the realistic causes
        are exactly the ones a tuple would miss (a sqlite3 error from a locked
        database inside the cleanup's ownership checks was #29's actual trigger).
        """
        try:
            step(*args)
        except Exception as e:
            log.error(
                f"PROCESSOR ({self.asin}): {description} failed after the book was already recorded as downloaded: {e}",
                exc_info=True,
            )

    def _cleanup_stale_files(self, previous_path, previous_parts=None):
        """
        Remove the files a re-download left behind. A re-download re-derives its
        output path from the *current* settings, so a changed output format or
        naming template writes the new output somewhere else entirely and the old
        one stops being referenced by anything — it just sits in /data forever.

        The "previous download" is a SET, not a file: `previous_parts` is the old
        `book_files` list (captured before finalize overwrote it) and
        `previous_path` is the row's own filepath — which for a split book is the
        FOLDER those parts lived in, never a file to delete. That set-wise shape
        is what makes every shape transition work: single -> split, split ->
        single, and split -> split with a different format or naming (where the
        old parts sit right beside the new ones under other names).

        Gated on this job's answer to the UI prompt OR the saved setting, since a
        scheduled job carries no params and the setting is the only thing that can
        speak for it. Every guard below is load-bearing: this is the finalizer's
        only destructive step, so it refuses to touch anything that is not a real,
        different, unclaimed file inside the output root. Each unlink is
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

        previous_parts = [path for path in (previous_parts or []) if path]
        if previous_parts:
            # The previous download was SPLIT (D3): its audio is the part set, and
            # `previous_path` names only the folder they sat in. The stem those
            # sidecars used is recorded nowhere, so it is read back off the folder
            # — and a folder that is ambiguous, or whose one candidate can't be
            # corroborated as this book's, answers None and skips the sweep. That
            # last part is what keeps an external library manager's own files
            # (Audiobookshelf writes a "cover.jpg" into every book folder) from
            # being read as an abandoned base and deleted; the stem this run
            # rendered for itself is the corroborating name.
            stale_audio = previous_parts
            stale_dirs = {os.path.dirname(path) for path in previous_parts}
            if previous_path:
                stale_dirs.add(previous_path)
            expected_stem = os.path.basename(self._sidecar_base()) if self.final_output_path else None
            # split=True: the PREVIOUS download was the split one (that is what
            # `previous_parts` means), so a ".cue" in that folder cannot be ours.
            old_sidecar_base = _owned_sidecar_base(previous_path, expected_stem, split=True) if previous_path else None
            old_base = os.path.realpath(old_sidecar_base) if old_sidecar_base else None
        elif previous_path:
            stale_audio = [previous_path]
            stale_dirs = {os.path.dirname(previous_path)}
            # Every destructive comparison below resolves symlinks: os.path.abspath
            # only collapses "."/".." , so a symlinked alias of the output tree (say
            # /data/Author -> /data/library/Author) makes the old and new paths
            # compare unequal even when they are the same file, and this run's own
            # freshly written output would be deleted.
            old_base = os.path.splitext(os.path.realpath(previous_path))[0]
        else:
            return

        # "Did we overwrite our own file?" is a question about what this run
        # actually WROTE, which for a split book is its N parts — not
        # final_output_path, which a split book only ever reserves and never
        # writes. Comparing against the reserved path instead would make the
        # single-file -> split re-download (the first thing anyone does with this
        # feature) skip the old whole-book file and strand it on disk beside the
        # new parts, untracked. For an unsplit run the set is exactly the one
        # output file, so this is the same test it always was.
        produced_real = {os.path.realpath(path) for path in self._produced_output_paths()}
        data_root = os.path.realpath("/data")
        # Read lazily: the ownership map is only needed once something has passed
        # every other guard, and the whole point of #30 is that this question is
        # asked BEFORE the delete, not after it.
        owners = None
        deleted_anything = False

        for stale_path in stale_audio:
            stale_real = os.path.realpath(stale_path)
            if stale_real in produced_real:
                continue  # The re-download overwrote this file; nothing was left behind.
            # This exists() guard is also what implements the plan's "never for a
            # MISSING book" rule: a MISSING row's tracked file is gone from disk,
            # so cleanup skips it before touching anything.
            if not os.path.exists(stale_path):
                continue
            # A tracked path that is a DIRECTORY is a split book's folder, not a
            # file this step deletes — reachable when the part rows are gone (a
            # hand-restored library.db) and `previous_path` is all that is left.
            # The empty-folder sweep at the end is what removes such a folder, and
            # only if it is genuinely empty.
            if os.path.isdir(stale_path):
                log.info(f"PROCESSOR ({self.asin}): '{stale_path}' is a folder, not a stale file; leaving it alone.")
                continue
            # Belt-and-braces on top of the realpath comparison — a hard link (or
            # a symlink realpath could not resolve) still makes two
            # different-looking paths the same inode. An OSError means the stat
            # failed, so we cannot prove they are the same file and fall through
            # to the remaining guards.
            if self._is_one_of_our_produced_files(stale_path):
                continue
            # Whatever the DB row claims, never delete outside the output
            # directory. Both sides are resolved so a symlinked /data still
            # compares as inside it.
            if not stale_real.startswith(data_root + os.sep):
                log.warning(
                    f"PROCESSOR ({self.asin}): Refusing to clean up '{stale_path}' — it is outside {data_root}."
                )
                continue
            # #30: co-ownership BEFORE the delete. Two rows tracking the identical
            # file is reachable in libraries created before same-base collisions
            # were prevented (and still via a manual upload), and one book's
            # re-download must not delete the file another row still points at.
            if owners is None:
                owners = _tracked_path_owners()
            claimants = sorted(owners.get(stale_real, set()) - {self.asin})
            if claimants:
                log.info(
                    f"PROCESSOR ({self.asin}): Left '{stale_path}' in place — "
                    f"book {claimants[0]} still tracks that file."
                )
                continue

            deleted_anything = True
            try:
                os.remove(stale_path)
                log.info(f"PROCESSOR ({self.asin}): Removed stale file from the previous download: {stale_path}")
            except OSError as e:
                log.warning(f"PROCESSOR ({self.asin}): Could not remove stale file '{stale_path}': {e}")

        # Nothing of the old output actually went, so nothing around it is stale
        # either: the previous download is either still the current one (an
        # in-place overwrite), gone already, or deliberately left alone.
        if not deleted_anything:
            return

        # Sidecars come off only when the extension-stripped BASE actually moved.
        # On a format-only change ("Title.m4b" -> "Title.mp3") the old base IS the
        # new base, so the "old" sidecars are the ones _place_sidecar_files wrote
        # moments ago for this very run — deleting them would destroy this
        # download's own output.
        new_base = os.path.splitext(os.path.realpath(self.final_output_path))[0]
        # ...and the same trap once more for a split book, whose sidecars do NOT
        # hang off final_output_path when D5's flat-template guard invented a
        # subfolder (or D12 put a collision suffix on the folder): they sit at
        # _sidecar_base() inside it. A previous single-file download living at
        # that very base (an old "{author} - {title}/{author} - {title}" layout
        # re-downloaded flat and split) leaves old_base equal to the base
        # _place_sidecar_files wrote to moments ago, while new_base points one
        # level up — so the "did the base move" test alone would sweep away this
        # run's own cover, PDF and metadata. For every other run _sidecar_base()
        # IS new_base and this extra term changes nothing.
        sidecar_base = os.path.realpath(self._sidecar_base())
        # A SPLIT run never writes final_output_path — it only reserves it — so
        # for a split book the new_base term protects nothing and can only do
        # harm: under a flat "{title}" template the reserved base
        # ("/data/Dracula") is exactly the base the PREVIOUS single-file
        # download used, while this run's sidecars went to "/data/Dracula/
        # Dracula". Consulting new_base there skips the sweep and strands the old
        # cover, PDF, cue and metadata.json in the output root forever, referenced
        # by nothing. The only base a split run wrote is `sidecar_base`, which the
        # second term already covers.
        old_base_is_ours = old_base == sidecar_base or (not self.split_part_paths and old_base == new_base)
        if old_base and not old_base_is_ours:
            # ...and only when nothing ELSE still lives at the old base. Sidecars
            # are keyed by the base while audio files are not, so a second book
            # sitting there under a different audio extension shares these exact
            # files — they may be its only cover/PDF/cue/metadata/raw master.
            if _output_base_is_shared(self.asin, old_base, {os.path.realpath(path) for path in stale_audio}):
                log.info(
                    f"PROCESSOR ({self.asin}): Skipped the stale-sidecar sweep at '{old_base}' — "
                    f"the base is still in use by another book."
                )
            else:
                # Matched however they are spelled on disk, same as the rename and
                # timestamp sweeps — a leftover ".JPG" is as stale as a ".jpg".
                # Only exact sidecar suffixes match, which is what keeps a chapter
                # file named after the same base from being swept as a sidecar.
                for suffix in _existing_sidecar_suffixes(old_base):
                    stale_sidecar = f"{old_base}{suffix}"
                    try:
                        os.remove(stale_sidecar)
                        log.info(f"PROCESSOR ({self.asin}): Removed stale sidecar: {stale_sidecar}")
                    except OSError as e:
                        log.warning(f"PROCESSOR ({self.asin}): Could not remove stale sidecar '{stale_sidecar}': {e}")

        # The one sidecar a SPLIT run can leave behind at its OWN base. A book
        # re-downloaded in place as a split one keeps its base, so `old_base` is
        # this run's own sidecar base and the sweep above is correctly skipped —
        # those files are the cover and metadata just written. The cue sheet is
        # the exception: D9 refuses to write one for a split book, so a ".cue"
        # sitting there is the previous single-file download's, describing a
        # timeline that no longer exists on disk. Named explicitly rather than by
        # relaxing the skip, which would take this run's own output with it.
        if old_base and old_base_is_ours and self.split_part_paths:
            for suffix in _existing_sidecar_suffixes(old_base):
                if suffix.lower() != ".cue":
                    continue
                stale_cue = f"{old_base}{suffix}"
                try:
                    os.remove(stale_cue)
                    log.info(f"PROCESSOR ({self.asin}): Removed the previous download's cue sheet: {stale_cue}")
                except OSError as e:
                    log.warning(f"PROCESSOR ({self.asin}): Could not remove stale cue sheet '{stale_cue}': {e}")

        # The old folder(s) may now be empty (a naming-template change moves whole
        # directory levels, and a split book's whole folder can empty out); the
        # existing helper only ever rmdirs, so a folder still holding anything —
        # another book's files, a sidecar we refused to sweep — is left alone.
        for directory in sorted(stale_dirs):
            _cleanup_empty_dirs(directory)

    def _is_one_of_our_produced_files(self, path):
        """
        True when `path` is the same file on disk as something this run produced,
        by inode rather than by name — a hard link, or a symlink realpath could
        not resolve, makes two different-looking paths one file. An OSError means
        the stat failed, so we cannot prove they are the same and the caller's
        remaining guards decide.
        """
        for produced_path in self._produced_output_paths():
            try:
                if os.path.exists(produced_path) and os.path.samefile(path, produced_path):
                    return True
            except OSError:
                continue
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

    def _finalize_split(self):
        """
        MERGE_BOOK task for a book split into per-chapter files. It takes the
        merge's place in the pipeline but does the opposite of merging: each
        encoded chunk already IS one of the book's output files, so this embeds
        the cover in each, promotes them all into the book's folder, and then
        runs the same verification and success finalization every other path
        uses.

        Promotion has to happen HERE, before the completion event is set: the
        per-book temp dir is deleted the moment `run` unblocks, taking every
        part still sitting in it.

        The whole body runs inside a try/finally because the completion event is
        the only thing that unblocks `run`: the part-row write is DELIBERATELY
        allowed to raise (see _finalize_success), and an escaping exception would
        otherwise leave `run` waiting out the full completion timeout — two hours
        minimum, with the download worker's slot held — before reporting a
        timeout for a book whose files are already on disk.
        """
        if self._cancelled():
            return
        log.info(f"TASK-SPLIT ({self.asin}): Starting.")
        conversion_start_time = time.time()
        _yield_progress(self.asin, "Finalizing chapter files...", 95, self.job_id)

        try:
            success = self._promote_split_parts()

            if not success:
                self._fail_or_cancel("Placing the per-chapter files failed.")
            elif self._timed_out.is_set():
                # The same guard _encode_mp3_and_finalize carries, and for the
                # same reason: `run` has already given up on this book, written
                # the ERROR row and walked away, so finalizing now would flip it
                # back to DOWNLOADED, reset retry_count, and — the destructive
                # part — run _cleanup_stale_files against the user's previous
                # download. The chunked and remux paths are covered for free
                # (their inputs die with the temp dir), but promotion has just
                # moved this book's parts OUT of the temp dir into /data, where
                # they outlive it exactly as the MP3 encoder's output does.
                # Discard the orphans instead of finalizing them.
                log.warning(
                    f"TASK-SPLIT ({self.asin}): Chapter files were placed after the completion timeout was "
                    f"already reported; discarding them instead of finalizing."
                )
                self._discard_timed_out_output()
            else:
                # Splitting spreads the same encode over N outputs and adds a
                # promotion pass, so the wall-clock time isn't comparable to the
                # single-file re-encode the estimator models — keep it out of the
                # shared rolling average (D15, same reasoning as the remux and MP3
                # paths' record_eta=False).
                self._finalize_success(conversion_start_time, record_eta=False)
        except Exception as e:
            # A raised finalize is a FAILED book, not a hung one. The realistic
            # cause is the part-row write hitting "database is locked": that
            # transaction rolls back whole (parent row included), so nothing was
            # recorded and the book must be marked ERROR so the retry logic can
            # pick it up. Routed through _fail_or_cancel so a cancellation or a
            # post-timeout echo is still suppressed, and so the once-only failure
            # latch still applies.
            log.error(f"PROCESSOR ({self.asin}): Finalizing the per-chapter files raised: {e}", exc_info=True)
            # The parts are already in /data — promotion moved them there before
            # the write that just failed — and the transaction rolled back whole,
            # so nothing in the database refers to them. Left alone they are N
            # untracked files in the library beside a book marked ERROR, which a
            # later deep sync would adopt. The same discard the post-timeout
            # guard runs, and for the same reason; it spares the previous
            # download's own files, so an in-place re-download loses nothing.
            self._discard_timed_out_output()
            try:
                self._fail_or_cancel(f"Recording the finished chapter files failed: {e}")
            except Exception as report_error:
                # The failure write can raise for the very same reason the
                # finalize did (a locked database). Nothing more can be done
                # about it here, and it must not stop the event below.
                log.error(f"PROCESSOR ({self.asin}): Could not record that failure either: {report_error}")
        finally:
            # This is the final step, so we signal the main `run` method to
            # unblock — on every path out of the body above, including a raise.
            self._completion_event.set()
            log.info(f"TASK-SPLIT ({self.asin}): Finalization complete.")

    def _promote_split_parts(self):
        """
        Embed the cover art in every encoded .m4b part and move each one to its
        final path. Returns True only when all of them landed.

        Each .m4b part is covered while it is still in the temp dir, so
        AtomicParsley never rewrites a file that is already visible in the
        library; .mp3 parts already carry their cover from the encode itself.
        Every part is then moved to "<target>.part" before an os.replace onto the
        real name — the same atomic-promotion pattern the MP3 encoder uses, so a
        half-copied part is never readable at its final path. shutil.move does the
        moving because the temp dir and /data are separate volumes in the shipped
        container and os.replace across devices raises EXDEV; the final os.replace
        stays within one directory, so it stays atomic.

        A failure part-way removes what already landed. Half a book in the
        library is worse than none of it: for a first download nothing tracks
        the parts yet (the DB write comes later, after verification), so they
        would sit there untracked and a later deep sync would find them.

        With ONE exception, and it is the whole reason `preexisting_part_targets`
        exists. A split->split re-download deliberately writes over the previous
        download's own files: the folder walk subtracts this ASIN, so the book's
        own parts never read as a collision, and unchanged metadata re-renders
        identical part names. Those targets ARE tracked — by the `book_files`
        rows the previous download wrote — so deleting them on a rollback
        destroys chapter files the user already had, for a cancel that is one
        click away in the job panel and a promotion that is a full cross-device
        copy of the whole book. They are recorded before the loop and skipped by
        every teardown: an overwritten-but-valid part is strictly better than a
        deleted one, and the rows keep naming a file that exists.

        Cancellation is re-checked between parts for the same reason. A cancel
        SIGTERMs every registered subprocess, but a shutil.move has no process to
        kill, so without this the loop would promote the whole remaining set and
        then take _finalize_success's cancel branch — which deliberately leaves
        the files alone — turning one cancelled book into N untracked files in
        the library. Returning False here routes into _fail_or_cancel, which
        recognizes the set stop_event and leaves the book's status untouched.
        """
        # The chunk paths arrive in completion order; their "chunk_NNN" names
        # sort back into chapter order, which is the order the planned part
        # paths are in (the same convention merge_book_chunks relies on).
        chunk_paths = sorted(self.encoded_chunk_paths)
        targets = self.split_part_paths
        if len(chunk_paths) != len(targets):
            log.error(
                f"PROCESSOR ({self.asin}): Encoded {len(chunk_paths)} chapter file(s) but planned "
                f"{len(targets)}; refusing to place a mismatched set."
            )
            return False

        cover_file = (self.context or {}).get("cover_file")
        # Taken BEFORE the first os.replace, which is the moment the previous
        # download's copy of a target stops existing.
        self.preexisting_part_targets = self._preexisting_tracked_targets()
        promoted = []
        try:
            for chunk_path, target in zip(chunk_paths, targets):
                # The stop_event is read directly rather than through
                # _cancelled(), which sets the completion event as a side effect:
                # unblocking `run` (and with it the temp-dir teardown) before the
                # rollback below has finished is a race worth not having. The
                # event is set by _finalize_split's finally either way.
                if self.stop_event is not None and self.stop_event.is_set():
                    log.info(
                        f"PROCESSOR ({self.asin}): Cancelled while placing chapter files; "
                        f"removing the {len(promoted)} already placed."
                    )
                    self._remove_promoted_parts(promoted, targets)
                    return False
                # AtomicParsley writes the mp4 "covr" atom and understands
                # nothing else, so an .mp3 part is left alone here: its cover is
                # already inside it as an id3v2 APIC frame, muxed during the
                # encode (see encode_chapter_chunk). Running AtomicParsley over
                # one would at best waste a process per part.
                if os.path.splitext(chunk_path)[1].lower() != ".mp3":
                    _embed_cover_art(self.asin, self.job_id, chunk_path, cover_file)
                os.makedirs(os.path.dirname(target), exist_ok=True)
                staged = f"{target}.part"
                shutil.move(chunk_path, staged)
                os.replace(staged, target)
                promoted.append(target)
        except Exception as e:
            # Deliberately broader than OSError: the rollback is the only thing
            # standing between a failure here and half a book sitting in the
            # library, so anything at all that escapes the loop has to reach it
            # — an unexpected type would otherwise unwind straight past into
            # _finalize_split with the parts already placed.
            log.error(f"PROCESSOR ({self.asin}): Could not place a chapter file: {e}", exc_info=True)
            self._remove_promoted_parts(promoted, targets)
            return False

        log.info(f"PROCESSOR ({self.asin}): Placed {len(promoted)} chapter file(s) in '{self.split_output_dir}'.")
        return True

    def _preexisting_tracked_targets(self):
        """
        Which of this run's planned part paths already existed on disk AND are
        already tracked to this book — the previous download's own chapter files,
        which an in-place re-download is about to write over.

        Both halves are required. "On disk" alone would spare a stranger's file
        that happens to sit where a part goes (the allocator's job, not this
        one's), and "tracked" alone would spare a row whose file is already gone.
        Together they name exactly the set whose deletion would cost the user
        data they had before this run started.

        The disk half is asked FIRST, and that is what keeps this free in the
        common case: a first download has nothing sitting at any of its targets,
        so no query is made at all — and nothing to spare, so a failed first
        download still rolls back completely. Only a re-download landing on its
        own files pays for the row read.

        That read FAILS CLOSED. `_tracked_part_paths` answers [] both for a book
        with no part rows and for a database it could not read (it swallows
        sqlite3.Error), and from here the two are indistinguishable — so an empty
        answer with files already sitting at our targets is treated as "cannot
        prove these are disposable" and spares all of them. Getting that
        backwards is the whole bug this exists to prevent: a lock on this one
        SELECT would otherwise empty the set and hand a cancel back to deleting
        the user's previous chapter files. Sparing too much costs untracked files
        a deep sync will adopt; sparing too little costs audio.
        """
        on_disk = [target for target in self.split_part_paths if os.path.exists(target)]
        if not on_disk:
            return set()
        tracked = {os.path.realpath(path) for path in _tracked_part_paths(self.asin)}
        if not tracked:
            return set(on_disk)
        return {target for target in on_disk if os.path.realpath(target) in tracked}

    def _remove_promoted_parts(self, promoted, targets):
        """
        Best-effort teardown of an abandoned promotion: every part that already
        landed, plus any ".part" staging file the interrupted move left behind,
        and then the book's folder if that emptied it. Shared by the cancel and
        the error branch of _promote_split_parts so the two can't drift apart.

        Targets the previous download already owned are skipped — see
        `preexisting_part_targets`. Whether such a target was reached before the
        interruption or not, it holds a complete chapter file either way (this
        run's or the last one's), and the `book_files` rows name it; deleting it
        would turn a cancelled re-download into missing audio. The folder prune
        below then no-ops on its own, since the folder is not empty.
        """
        for path in promoted + [f"{target}.part" for target in targets]:
            if path in self.preexisting_part_targets:
                continue
            try:
                os.remove(path)
            except OSError:
                pass
        self._prune_empty_split_dir()

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
        elif self._timed_out.is_set():
            # The symmetric guard to _fail_or_cancel's: `run` already gave up on
            # this encode, wrote the ERROR row and walked away, so a success
            # arriving now belongs to a run that has declared itself failed.
            # Finalizing anyway would flip the book back to DOWNLOADED, reset
            # retry_count, and — the destructive part — run _cleanup_stale_files
            # against the previous file. The chunked and remux paths are covered
            # for free (their inputs die with the temp dir, so they just fail),
            # but the MP3 encoder writes beside the FINAL path in /data, which
            # outlives the temp dir. Discard the orphan we just wrote.
            log.warning(
                f"TASK-MP3 ({self.asin}): Encode finished after the completion timeout was already "
                f"reported; discarding the output instead of finalizing."
            )
            self._discard_timed_out_output()
        else:
            # Single-threaded LAME rates aren't comparable to the parallel
            # chunked-AAC encode, so keep them out of the shared ETA model
            # (same reasoning as the remux path's record_eta=False).
            self._finalize_success(conversion_start_time, record_eta=False)

        # This is the final step, so we signal the main `run` method to unblock.
        self._completion_event.set()
        log.info(f"TASK-MP3 ({self.asin}): Finalization complete.")

    def _discard_timed_out_output(self):
        """
        Best-effort removal of an output this run placed in /data that nothing
        will ever refer to: the post-timeout case it is named for, and the split
        finalize whose database write raised (that transaction rolls back whole,
        so the parts it just promoted are as unreferenced as a timed-out set).

        The MP3 encoder promotes its ".part" onto the final path before returning
        success, so by the time the guard above sees `_timed_out` the file is
        already sitting in /data — unreferenced by the DB (the row says ERROR)
        but perfectly readable, so a later deep sync would adopt it. Failing to
        delete it is not worth failing anything over: the run is already recorded
        as failed.

        What it does NOT touch is the previous download's own chapter files
        (`preexisting_part_targets`): an in-place re-download overwrote them, so
        those paths hold a complete file and the `book_files` rows still name it.
        Removing them would take the user's existing copy away on behalf of a run
        that failed — the same rule the promotion rollback follows, in the same
        currency.

        A split book is the same story with N files, so this walks whatever the
        run actually produced rather than the single final path (which a split
        book never writes). The timeout can land at any point around promotion —
        before it starts, between two parts, or after the last one — so the set
        on disk may be empty, partial or complete, and every branch here is a
        per-path best-effort that simply skips what isn't there. The ".part"
        staging names are swept too: a timeout landing between the move and the
        replace leaves one behind, and it is just as untracked as a real part.
        Finally the book's folder goes if removing the parts emptied it.
        """
        for path in self._produced_output_paths():
            if path in self.preexisting_part_targets:
                continue
            if not os.path.exists(path):
                continue
            try:
                os.remove(path)
                log.info(f"PROCESSOR ({self.asin}): Removed post-timeout output file {path}.")
            except OSError as e:
                log.warning(f"PROCESSOR ({self.asin}): Could not remove post-timeout output file: {e}")

        for target in self.split_part_paths:
            staged = f"{target}.part"
            if not os.path.exists(staged):
                continue
            try:
                os.remove(staged)
                log.info(f"PROCESSOR ({self.asin}): Removed post-timeout staging file {staged}.")
            except OSError as e:
                log.warning(f"PROCESSOR ({self.asin}): Could not remove post-timeout staging file: {e}")

        self._prune_empty_split_dir()

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

        A completion timeout is the mirror image: there the stop_event is NOT set
        (nobody cancelled anything), but the timeout handler has already reported
        the failure and walked away, leaving this book's tasks running. Whenever one
        of those abandoned steps eventually fails, the report arriving here is an
        echo of a failure that is already recorded. Writing it again would bump
        retry_count a second time (or once per in-flight chunk) and overwrite
        "Processing timed out." with whichever step happened to report last. (A
        step that fails just before the timeout handler raises the flag wins the
        race instead and keeps its own message — see the latch in
        _update_db_on_failure; either way exactly one write survives.) The prepare
        path needs no equivalent guard: it already reads its own -15 as
        cancellation and writes nothing.

        Whatever the report turns out to be, the run is over for this book, so
        this is also where a split book's empty output folder goes. It runs
        BEFORE the two early returns above precisely because a cancel and a
        post-timeout echo leave the same empty directory a plain failure does,
        and it is safe on every path into here: parts only ever enter that folder
        during promotion, and a promotion that failed part-way has already taken
        back the ones it placed (_remove_promoted_parts). A folder still holding
        anything — the previous download's parts, which that rollback deliberately
        spares — is left exactly as it is: the prune is a single rmdir.
        """
        self._prune_empty_split_dir()
        if self.stop_event is not None and self.stop_event.is_set():
            log.info(f"PROCESSOR ({self.asin}): Step cancelled; leaving book status unchanged.")
            return
        if self._timed_out.is_set():
            log.info(
                f"PROCESSOR ({self.asin}): Step failed after the completion timeout was already reported "
                f"({error_message}); not recording it again."
            )
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

        Because the bump lives here, so does the once-per-run latch: the FIRST
        reporter of a run claims the report and writes it, and every later one
        (the late chunk echoes described on `self._failure_reported`, or a
        teardown error surfacing in `run`'s except block after a step already
        reported) is logged and dropped. The guarantee is exactly one failure
        write per run, first reporter wins — not a particular message: a chunk
        failing microseconds before the timeout handler sets `_timed_out` can
        claim the latch first, in which case the surviving message names the
        chunk's real cause instead of "Processing timed out." Either way the
        count — the part the retry gate depends on — is one.

        The claim is taken under the lock but the write happens outside it, and a
        write that RAISES releases the claim again (see the except below): a
        claim whose write never landed would otherwise silence the whole rest of
        the run.
        """
        with self._failure_report_lock:
            if self._failure_reported:
                log.info(
                    f"PROCESSOR ({self.asin}): A failure was already reported for this run "
                    f"({error_message}); not recording it again."
                )
                return
            # Claimed here so two threads racing produce exactly one write, but
            # only PROVISIONALLY: it is released again if the write below raises.
            self._failure_reported = True

        log.error(f"PROCESSOR ({self.asin}):   -> ERROR: {error_message}")
        try:
            with get_db_connection() as con:
                con.execute(
                    "UPDATE audiobooks SET status = 'ERROR', error_message = ?, "
                    "retry_count = COALESCE(retry_count, 0) + 1 WHERE asin = ?",
                    (error_message, self.asin),
                )
        except Exception:
            # A raised write ("database is locked" being the realistic one) must
            # not consume the run's one report. Holding a claim with no row
            # behind it is the worst of both worlds: no ERROR status, no
            # retry_count bump, and every later reporter suppressed — including
            # `run`'s own except handler after the completion timeout — leaving
            # the book NEW at retry_count 0, which every future scheduled run
            # picks up again. Release the claim so the next reporter gets a turn,
            # then re-raise: what the caller sees on a failed write is exactly
            # what it saw before the latch existed.
            with self._failure_report_lock:
                self._failure_reported = False
            raise
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

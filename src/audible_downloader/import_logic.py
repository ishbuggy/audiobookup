# audible_downloader/import_logic.py
#
# Phase 6 (FR2): manual "add existing file" import.
#
# This module holds the *shared adoption core* that both import entry points
# funnel through — the scan-in-place job (adopt files already under /data) and
# the streaming upload endpoint (write into /data, then adopt). Given a path to
# an audio file it probes the file, decides whether it is a known Audible book
# or a genuine import, writes/updates the DB row, and best-effort extracts a
# cover. It never re-encodes: imported files are adopted as-is.
#
# See ref-docs/phase6-import-design.md for the full design and the identity
# rules encoded below.

import json
import os
import re
import shutil
import subprocess
import uuid
from datetime import datetime, timezone

from . import COVERS_DIR
from .db import get_all_tracked_part_paths, get_book_files, get_db_connection
from .logger import log
from .process_registry import process_registry


def _run_registered(cmd, job_id):
    """
    Run `cmd` to completion as a registered subprocess so a job cancel can
    SIGTERM it (house rule: every long-running ffmpeg/ffprobe call registers
    with process_registry). Returns (returncode, stdout, stderr). When job_id is
    None (e.g. the upload path, which isn't a cancellable job) register/unregister
    are no-ops. OSError from spawning propagates to the caller.
    """
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    process_registry.register(job_id, proc)
    try:
        stdout, stderr = proc.communicate()
    finally:
        process_registry.unregister(job_id, proc)
    return proc.returncode, stdout, stderr


# An Audible ASIN is exactly 10 uppercase alphanumerics. The embedded `asin`
# tag is read from *file content* (fully attacker-controlled on upload), and the
# key derived from it is used to build filesystem paths (cover files) and the DB
# primary key — so a tag like "../../config/x" must never be trusted as an ASIN.
# Anything that doesn't match this pattern is treated as "no ASIN" and the file
# gets a safe synthetic IMPORT-<hex> key instead.
_ASIN_RE = re.compile(r"[A-Z0-9]{10}")
# Keys we allow to touch the covers path: our own IMPORT-<hex> and real ASINs.
_SAFE_KEY_RE = re.compile(r"[A-Za-z0-9_-]+")

# Root of the final library. A module constant (not a hard-coded literal spread
# through the file) so tests can point it at a temp directory.
DATA_DIR = "/data"

# Containers we adopt as-is. Apple audiobook containers only for v0.19; other
# formats (e.g. .mp3) are deferred (see the design note).
IMPORTABLE_EXTS = (".m4b", ".m4a")

# Uploaded files are streamed here (under DATA_DIR, so the final placement is a
# same-filesystem rename rather than a cross-volume copy) before adoption.
IMPORT_STAGING_DIRNAME = ".import_staging"


def import_staging_dir():
    """Absolute path to the upload staging directory (created on demand)."""
    return os.path.join(DATA_DIR, IMPORT_STAGING_DIRNAME)


def _run_ffprobe_json(filepath, job_id=None):
    """
    Return the parsed `ffprobe -show_format` JSON for `filepath`, or an empty
    dict if the file can't be probed. Used to pull the embedded metadata tags
    and duration in a single call.
    """
    cmd = [
        "ffprobe",
        "-v",
        "quiet",
        "-print_format",
        "json",
        "-show_format",
        filepath,
    ]
    try:
        returncode, stdout, stderr = _run_registered(cmd, job_id)
    except OSError as e:
        log.warning(f"IMPORT: ffprobe could not run on '{filepath}': {e}")
        return {}
    if returncode != 0:
        log.warning(f"IMPORT: ffprobe failed for '{filepath}': {(stderr or '').strip()}")
        return {}
    try:
        return json.loads(stdout)
    except json.JSONDecodeError:
        return {}


def _probe_metadata(filepath, job_id=None):
    """
    Extract the fields we persist for an imported book from `filepath`.

    Tag lookups are case-insensitive (ffmpeg's tag casing varies by container).
    Returns a dict with keys: embedded_asin, title, author, release_date,
    runtime_min. Missing values are returned as None / 0 for the caller to
    apply its own fallbacks.
    """
    fmt = _run_ffprobe_json(filepath, job_id).get("format", {})
    tags = {str(k).lower(): v for k, v in fmt.get("tags", {}).items()}
    # ffprobe returns no `format` object for a file it can't read (missing,
    # zero-byte, truncated, or non-audio content that merely carries an
    # importable extension). The scan path uses this to skip junk rather than
    # creating a placeholder row.
    probe_ok = bool(fmt)

    runtime_min = 0
    try:
        runtime_min = int(round(float(fmt.get("duration", 0)) / 60))
    except (TypeError, ValueError):
        runtime_min = 0

    # Only accept a well-formed ASIN; a malformed/hostile tag is dropped so the
    # caller falls back to a synthetic key (prevents path traversal via the key).
    embedded_asin = (tags.get("asin") or "").strip() or None
    if embedded_asin and not _ASIN_RE.fullmatch(embedded_asin):
        log.warning(f"IMPORT: ignoring malformed embedded asin tag {embedded_asin!r} in '{filepath}'.")
        embedded_asin = None

    return {
        "embedded_asin": embedded_asin,
        "title": (tags.get("title") or "").strip() or None,
        # `artist` is the author in Audible/Apple audiobook tags; album_artist is a fallback.
        "author": (tags.get("artist") or tags.get("album_artist") or "").strip() or None,
        "release_date": (tags.get("date") or "").strip() or None,
        "runtime_min": runtime_min,
        "probe_ok": probe_ok,
    }


def _extract_cover(filepath, key, job_id=None):
    """
    Best-effort: pull an embedded cover image out of `filepath` into the same
    covers/<key>_original.jpg + _thumb.jpg layout the sync and upload paths use.
    A file with no attached picture (or any ffmpeg error) is left without a
    cover — the grid falls back to its placeholder. Never raises.
    """
    # Defense in depth: the key becomes a filename here, so reject anything that
    # isn't a plain token (no path separators, no "..") before touching the disk.
    if not _SAFE_KEY_RE.fullmatch(key):
        log.warning(f"IMPORT: refusing to write cover for unsafe key {key!r}.")
        return

    os.makedirs(COVERS_DIR, exist_ok=True)
    covers_root = os.path.realpath(COVERS_DIR)
    original_path = os.path.join(COVERS_DIR, f"{key}_original.jpg")
    thumb_path = os.path.join(COVERS_DIR, f"{key}_thumb.jpg")
    # Belt-and-suspenders: confirm both targets resolve inside COVERS_DIR.
    for target in (original_path, thumb_path):
        if os.path.realpath(target) != os.path.join(covers_root, os.path.basename(target)):
            log.warning(f"IMPORT: cover path for key {key!r} escaped the covers dir; skipping.")
            return

    # Grab the first attached picture as a single JPEG frame. -nostdin avoids any
    # prompt hang; failure (no video stream) is expected and swallowed.
    extract = [
        "ffmpeg",
        "-nostdin",
        "-y",
        "-i",
        filepath,
        "-map",
        "0:v:0",
        "-frames:v",
        "1",
        "-update",
        "1",
        original_path,
    ]
    try:
        returncode, _out, _err = _run_registered(extract, job_id)
    except OSError as e:
        log.debug(f"IMPORT: cover extract could not run for '{key}': {e}")
        return
    if returncode != 0 or not os.path.exists(original_path):
        log.debug(f"IMPORT: no embedded cover in '{filepath}' for {key}.")
        return

    scale = ["ffmpeg", "-nostdin", "-y", "-i", original_path, "-vf", "scale=200:200", thumb_path]
    try:
        _run_registered(scale, job_id)
    except OSError as e:
        log.debug(f"IMPORT: thumbnail generation failed for {key}: {e}")


def _split_part_paths(asin):
    """
    The book's per-chapter file paths, or an empty list when it isn't split.
    ANY row here means the book is split — that presence is the split flag — so
    callers use the emptiness of this list, not the paths' existence on disk, to
    decide how the book may be touched. Rows with a blank `filepath` are dropped
    so this reader agrees with the other readers of the same rows (`sync_logic`
    and `processing_logic`) about what counts as a part.
    """
    return [row["filepath"] for row in get_book_files(asin) if row["filepath"]]


def _row_blocks_repoint(existing_filepath, incoming_filepath):
    """
    True if a DB row we were about to repoint at `incoming_filepath` already
    points at a *different* file that is still on disk — in which case we must
    NOT repoint it. Repointing there would orphan the good file, and because the
    embedded `asin` tag is file-controlled, it would let an imported file silently
    hijack a real Audible row (mark it DOWNLOADED, steal its filepath). A row with
    no filepath, a filepath whose file is gone, or one already pointing at this
    same file does not block — those are the legitimate reconcile cases.

    Single-file books only: a SPLIT book is refused outright by its caller before
    reaching here (see adopt_file), because this test cannot express the split
    case at all — such a row holds a FOLDER, so `isfile` is always False.
    """
    if not existing_filepath:
        return False
    if os.path.abspath(existing_filepath) == os.path.abspath(incoming_filepath):
        return False
    return os.path.isfile(existing_filepath)


def adopt_file(filepath, *, allow_reconcile=True, key=None, job_id=None):
    """
    Adopt a single on-disk audio file into the library.

    Identity resolution (see the design note):
      1. Embedded `asin` tag that matches an existing DB row -> reconcile that
         Audible book in place (set filepath, mark DOWNLOADED). action="reconciled".
         Guarded: if that row already points at a different, still-present file we
         skip instead of repointing (action="skipped", reason="asin-already-linked"),
         so a stray or hostile copy can neither hijack the row nor flip-flop the
         filepath between duplicates on repeated scans. A SPLIT book is refused
         unconditionally (action="skipped", reason="split-book") — see below.
      2. Same absolute path already tracked -> action="skipped" (idempotent
         re-scan; no duplicate row).
      3. Otherwise a genuine import: the key is `key` (when the caller pre-chose
         one, e.g. the upload path so the on-disk name and the row agree), else
         the embedded ASIN (present but unknown), else a synthetic "IMPORT-<uuid>".
         Metadata from the file's tags, then its filename. source='imported'.
         action="imported". The same repoint guard applies here.

    `allow_reconcile=False` forces the file to be treated as an import even if it
    carries a known ASIN tag (not used by the current callers, but keeps the core
    honest for tests).

    Returns a small result dict: {"action", "key", "title"?, "author"?, "reason"?}.
    Best-effort on covers; raises only on a genuine DB error.
    """
    filepath = os.path.abspath(filepath)
    ext = os.path.splitext(filepath)[1].lower()
    if ext not in IMPORTABLE_EXTS:
        return {"action": "skipped", "reason": "unsupported-type", "key": None}
    if not os.path.isfile(filepath):
        return {"action": "skipped", "reason": "not-found", "key": None}

    meta = _probe_metadata(filepath, job_id)
    embedded_asin = meta["embedded_asin"]

    with get_db_connection() as con:
        # (1) Known Audible book by embedded ASIN -> reconcile in place.
        if embedded_asin and allow_reconcile:
            known = con.execute("SELECT asin, filepath FROM audiobooks WHERE asin = ?", (embedded_asin,)).fetchone()
            if known is not None:
                # A SPLIT book is never repointed by an import, whether or not its
                # parts are still where the database says they are. We cannot tell
                # a book whose chapter files were DELETED from one whose folder the
                # user simply MOVED — and the moved case is exactly what the scan
                # feeds us, since relocated parts look untracked. Repointing would
                # then write one chapter file as the whole book's filepath and drop
                # the rest, silently redefining a 12-file book as a single-file book
                # holding chapter 7. Changing a book's shape belongs to a force
                # re-download, and recovering a moved folder to the deep sync's
                # restore (sync_logic._reconcile_database) — never to an import.
                known_parts = _split_part_paths(embedded_asin)
                if known_parts:
                    log.info(
                        f"IMPORT: ASIN {embedded_asin} is a split book with {len(known_parts)} chapter files; "
                        f"skipping '{filepath}' rather than repointing it at a single file."
                    )
                    return {"action": "skipped", "reason": "split-book", "key": embedded_asin}
                if _row_blocks_repoint(known["filepath"], filepath):
                    log.info(
                        f"IMPORT: ASIN {embedded_asin} is already linked to an existing file; "
                        f"skipping '{filepath}' rather than repointing it."
                    )
                    return {"action": "skipped", "reason": "asin-already-linked", "key": embedded_asin}
                con.execute(
                    "UPDATE audiobooks SET status = 'DOWNLOADED', filepath = ? WHERE asin = ?",
                    (filepath, embedded_asin),
                )
                con.commit()
                log.info(f"IMPORT: Reconciled known ASIN {embedded_asin} to '{filepath}'.")
                return {"action": "reconciled", "key": embedded_asin}

        # (2) Idempotency: this exact path is already adopted.
        tracked = con.execute("SELECT asin FROM audiobooks WHERE filepath = ?", (filepath,)).fetchone()
        if tracked is not None:
            return {"action": "skipped", "reason": "already-tracked", "key": tracked["asin"]}

    # (3) Genuine import.
    # Scan-path robustness: a zero-byte or unprobeable file is almost certainly
    # not a genuine audiobook (empty, truncated, or non-audio content that merely
    # carries an importable extension). Skip it rather than polluting the library
    # with a junk row (title=filename, Unknown Author, runtime 0). The upload path
    # (key set) is an explicit user action and already rejects empty bodies at the
    # endpoint, so it isn't second-guessed here.
    if key is None and (os.path.getsize(filepath) == 0 or not meta.get("probe_ok", True)):
        log.info(f"IMPORT: skipping unreadable/empty media '{filepath}' (ffprobe found no media format).")
        return {"action": "skipped", "reason": "unreadable-media", "key": None}

    # Choose a stable key and build the metadata. NOTE: for an untagged file the
    # synthetic IMPORT-<uuid> key is minted fresh per adoption and identity rests
    # solely on the filepath match in step (2) — moving or renaming an already-
    # imported untagged file within /data orphans its old row and adopts the new
    # path under a new key (a duplicate row). Inherent to the no-content-hash
    # design; a tagged file (real ASIN) reconciles correctly across moves.
    key = key or embedded_asin or f"IMPORT-{uuid.uuid4().hex[:12]}"
    title = meta["title"] or os.path.splitext(os.path.basename(filepath))[0]
    author = meta["author"] or "Unknown Author"
    release_date = meta["release_date"] or "N/A"
    date_added = datetime.now(timezone.utc).isoformat()

    with get_db_connection() as con:
        exists = con.execute("SELECT asin, filepath FROM audiobooks WHERE asin = ?", (key,)).fetchone()
        if exists is not None:
            # The chosen key already has a row. Same two guards as step 1, in the
            # same order: a split book is refused outright (its shape is not an
            # import's to change), and a row already linked to a different live
            # file is never repointed.
            existing_parts = _split_part_paths(key)
            if existing_parts:
                log.info(
                    f"IMPORT: key {key} is a split book with {len(existing_parts)} chapter files; "
                    f"skipping '{filepath}' rather than repointing it at a single file."
                )
                return {"action": "skipped", "reason": "split-book", "key": key}
            if _row_blocks_repoint(exists["filepath"], filepath):
                log.info(
                    f"IMPORT: key {key} is already linked to an existing file; "
                    f"skipping '{filepath}' rather than repointing it."
                )
                return {"action": "skipped", "reason": "key-already-linked", "key": key}
            # Re-adopting a previously imported book (embedded-ASIN key) at a new
            # path: refresh its location without spawning a duplicate row.
            con.execute(
                "UPDATE audiobooks SET status = 'DOWNLOADED', filepath = ?, source = 'imported' WHERE asin = ?",
                (filepath, key),
            )
            action = "reconciled"
        else:
            con.execute(
                (
                    "INSERT INTO audiobooks "
                    "(asin, author, title, status, series, narrator, runtime_min, release_date, "
                    "filepath, publisher, language, purchase_date, summary, date_added, source) "
                    "VALUES (?, ?, ?, 'DOWNLOADED', 'N/A', 'N/A', ?, ?, ?, 'N/A', 'N/A', 'N/A', 'N/A', ?, 'imported')"
                ),
                (key, author, title, meta["runtime_min"], release_date, filepath, date_added),
            )
            action = "imported"
        con.commit()

    _extract_cover(filepath, key, job_id)
    log.info(f"IMPORT: {action} '{title}' as {key} from '{filepath}'.")
    return {"action": action, "key": key, "title": title, "author": author}


def _first_free_output_path(base_path, suffix):
    """
    Return the first path in the collision sequence whose extension-stripped BASE
    is free on disk, so a file placed there can never overwrite another book's
    file *or* end up sharing another book's sidecars: `base_path`, then
    `<root>_<suffix><ext>`, then `<root>_<suffix>_2<ext>`, `_3`, ... `suffix` is the
    (already-sanitized) adoption key, mirroring the download/rename ASIN-suffix
    convention. In practice the first or second candidate is free; the loop only
    guards the pathological case where both the template name and the key-suffixed
    name are taken (e.g. a duplicate that was itself already downloaded there).

    Freeness is judged on the base rather than on the full filename because the
    sidecars (cover, PDF, cue, metadata) hang off the extension-stripped base: an
    existing book at "<root>.m4b" makes "<root>.m4a" unusable for a *different*
    book even though that exact filename is free, since the two would then write
    and delete the same sidecar set. This is the same currency processing_logic's
    reservation and rename allocators use.
    """
    # Imported here (not at module top) for the same reason adopt_upload does it:
    # keep the cross-module dependency lazy so import order stays unconstrained.
    from .processing_logic import _sibling_audio_paths

    root, ext = os.path.splitext(base_path)

    def _base_is_free(candidate_root):
        # A base is free only when NOTHING occupies it — neither the extension we
        # are about to write nor any sibling audio extension (_AUDIO_EXTENSIONS),
        # each of which would share this base's sidecars.
        if os.path.exists(f"{candidate_root}{ext}"):
            return False
        return not any(os.path.exists(sibling) for sibling in _sibling_audio_paths(candidate_root, ext))

    if _base_is_free(root):
        return base_path
    candidate_root = f"{root}_{suffix}"
    n = 2
    while not _base_is_free(candidate_root):
        candidate_root = f"{root}_{suffix}_{n}"
        n += 1
    return f"{candidate_root}{ext}"


def adopt_upload(staging_path, original_filename, settings):
    """
    Place an already-staged upload into the managed library structure and adopt
    it. The final name comes from the naming template + the file's probed
    metadata (the same template downloads use); a name that collides with an
    existing book gets a key suffix rather than overwriting it. The key chosen
    here is passed through to adopt_file so the on-disk name and the DB row agree.

    Returns the adopt_file result dict with a "filepath" key added. On success the
    staging file has been moved into /data; on failure it is left for the caller
    to clean up.
    """
    # Imported here (not at module top) to avoid an import cycle: processing_logic
    # imports nothing from us, but keeping this local mirrors the codebase's other
    # lazy cross-module imports and is cheap.
    from .processing_logic import _sanitize_filename, build_base_output_path

    meta = _probe_metadata(staging_path)
    # Reject non-media uploads before placing anything under /data. The endpoint
    # already rejects an empty body, but a *non-empty* file can still be renamed
    # junk (an importable extension over non-audio bytes): ffprobe finds no format
    # and adopting it would create a bogus DOWNLOADED row (title=filename, Unknown
    # Author, runtime 0). The scan path skips these via probe_ok; uploads get the
    # same guard. The staging file is left unmoved for the endpoint to clean up.
    if not meta.get("probe_ok", False):
        log.info(f"IMPORT: rejecting unreadable upload '{original_filename}' (ffprobe found no media format).")
        return {"action": "skipped", "reason": "unreadable-media", "key": None, "filepath": None}
    key = meta["embedded_asin"] or f"IMPORT-{uuid.uuid4().hex[:12]}"
    title = meta["title"] or os.path.splitext(os.path.basename(original_filename))[0] or "Unknown Title"
    author = meta["author"] or "Unknown Author"

    # Keep the uploaded file's real container extension (.m4a stays .m4a) rather
    # than defaulting to .m4b, so the on-disk name reflects the actual content and
    # matches what scan-in-place would record for the same file.
    ext = os.path.splitext(staging_path)[1].lower() or ".m4b"
    # Pass exactly the values adopt_file will write to the row below, so the placed
    # path and the stored metadata agree. Without this a "{year}"/"{series}" template
    # renders those segments empty at import time but filled on the first metadata
    # edit, and apply_custom_to_filenames then moves the freshly imported book. Only
    # the date is recoverable from the tags; series/series_sequence/language are the
    # row's "N/A"/NULL placeholders and render as dropped segments.
    release_date = meta["release_date"] or "N/A"
    base_path = build_base_output_path(
        settings,
        key,
        author,
        title,
        "N/A",
        "N/A",
        ext=ext,
        series="N/A",
        series_sequence=None,
        release_date=release_date,
        language="N/A",
    )
    # Collision-safe: never overwrite an existing file. A SINGLE key suffix is not
    # enough — the ASIN-suffixed name is exactly where this key's own prior
    # duplicate download already lives, so a bare "_<key>" could land straight on a
    # linked book's file and shutil.move would clobber it *before* adopt_file's
    # reconcile guard runs. Walk suffixes until the target is genuinely free; the
    # placed file is then adopted, and if the key belongs to an already-linked row
    # adopt_file declines to repoint it and adopt_upload removes the redundant copy.
    final_path = _first_free_output_path(base_path, _sanitize_filename(key))

    os.makedirs(os.path.dirname(final_path), exist_ok=True)
    shutil.move(staging_path, final_path)

    result = adopt_file(final_path, key=key)
    if result.get("action") == "skipped":
        # The upload duplicated a book already linked to an existing file, so
        # adopt_file declined to repoint the row. Don't leave the redundant copy
        # we just placed sitting untracked under /data (a later scan would keep
        # re-skipping it forever); remove it and report the skip.
        try:
            os.remove(final_path)
        except OSError as e:
            log.warning(f"IMPORT: could not remove redundant upload '{final_path}': {e}")
        result["filepath"] = None
        return result
    result["filepath"] = final_path
    return result


def scan_data_dir_for_untracked():
    """
    Walk DATA_DIR and return the list of importable file paths that are NOT
    already tracked — by a book's own `filepath` OR by one of its per-chapter
    part rows. The staging directory is skipped. Cheap (no probing) — the scan
    job probes each returned path via adopt_file.
    """
    with get_db_connection() as con:
        tracked = {
            os.path.abspath(row["filepath"])
            for row in con.execute("SELECT filepath FROM audiobooks WHERE filepath IS NOT NULL AND filepath != ''")
            if row["filepath"]
        }
    # A split book owns many files but contributes only ONE `audiobooks.filepath`
    # (its folder), so the parent column alone would leave every per-chapter file
    # we produced looking untracked — reported as an import candidate on every
    # single scan, forever. The child rows close that gap.
    tracked.update(os.path.abspath(path) for path in get_all_tracked_part_paths() if path)

    staging = os.path.abspath(import_staging_dir())
    untracked = []
    for root, _dirs, files in os.walk(DATA_DIR):
        # Skip the staging dir itself and anything under it. Match on a separator
        # boundary so a sibling like `/data/.import_staging_old/` isn't caught by
        # a bare prefix match.
        root_abs = os.path.abspath(root)
        if root_abs == staging or root_abs.startswith(staging + os.sep):
            continue
        for name in files:
            if name.lower().endswith(IMPORTABLE_EXTS):
                path = os.path.abspath(os.path.join(root, name))
                if path not in tracked:
                    untracked.append(path)
    return untracked

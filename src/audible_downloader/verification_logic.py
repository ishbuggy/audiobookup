# audible_downloader/verification_logic.py

import json
import os
import subprocess

from . import announcer
from .db import get_book_files, get_db_connection
from .logger import log


def _yield_progress(status_text, progress):
    payload = {
        "asin": "verify-job",
        "status_text": status_text,
        "progress": progress,
    }
    announcer.announce(f"event: job_update\ndata: {json.dumps(payload)}\n\n")


def _probe_duration(filepath):
    """
    Run the duration ffprobe against one file. Returns (seconds, error_text):
    exactly one of the two is meaningful — a float duration on success, or the
    ffprobe stderr when the file could not be read. Factored out only so the
    split-book path can probe N files without duplicating the command.
    """
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
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        return None, result.stderr
    return float(result.stdout.strip()), None


def _verify_split_book(job_id, asin, title, expected_min, part_paths):
    """
    Integrity-check a book that was split into per-chapter files, and return
    True if it was marked ERROR.

    Split books have no single file to measure, so (D10) the book is whole only
    if EVERY part is present, and its duration is the SUM of the parts' — checked
    against the same tolerance a single file gets. There is no partial state: any
    missing part fails the whole book, with an "N of M parts" detail so the user
    can see how much of it survived.
    """
    total_parts = len(part_paths)
    missing = [path for path in part_paths if not path or not os.path.exists(path)]
    if missing:
        log.warning(f"VERIFY ({job_id}): {len(missing)} of {total_parts} parts missing for {title} ({asin})")
        _mark_as_error(asin, f"Integrity Check Failed: {len(missing)} of {total_parts} parts missing from disk.")
        return True

    if not expected_min or expected_min <= 0:
        return False

    # Cost note: a split book costs one ffprobe PER PART instead of one per book,
    # so a library of 30-chapter books makes a Verify job ~30x more subprocess
    # work. Logged per book so a slow verification has an explanation in app.log.
    log.debug(f"VERIFY ({job_id}): {title} is split into {total_parts} files; running {total_parts} ffprobe checks.")
    actual_sec = 0.0
    for index, path in enumerate(part_paths, start=1):
        seconds, error_text = _probe_duration(path)
        if seconds is None:
            log.warning(f"VERIFY ({job_id}): Part {index} of {total_parts} corrupt (ffprobe failed) for {title}")
            _mark_as_error(asin, f"Integrity Check Failed: Part {index} of {total_parts} corrupt. {error_text}")
            return True
        actual_sec += seconds

    expected_sec = expected_min * 60
    # Identical tolerance to the single-file check: only a significantly SHORTER
    # book is a failure (5% AND more than 10 minutes off).
    diff = abs(actual_sec - expected_sec)
    if actual_sec < (expected_sec * 0.95) and diff > 600:
        log.warning(
            f"VERIFY ({job_id}): Truncated split book detected for {title}! "
            f"Expected {expected_min}m, got {int(actual_sec / 60)}m across {total_parts} parts."
        )
        _mark_as_error(
            asin,
            f"Integrity Check Failed: Duration mismatch across {total_parts} parts "
            f"(Expected {expected_min}m, Got {int(actual_sec / 60)}m).",
        )
        return True

    log.debug(f"VERIFY ({job_id}): {title} passed ({int(actual_sec)}s / {expected_sec}s across {total_parts} parts).")
    return False


def run_verification_logic(job_id):
    """
    Scans all DOWNLOADED books and verifies their file integrity (duration).
    Marks corrupt books as ERROR.
    """
    log.info(f"VERIFY ({job_id}): Starting library integrity check...")
    _yield_progress("Starting verification...", 0)

    with get_db_connection() as con:
        books = con.execute(
            "SELECT asin, title, runtime_min, filepath FROM audiobooks WHERE status = 'DOWNLOADED'"
        ).fetchall()

    total_books = len(books)
    issues_found = 0

    if total_books == 0:
        log.info(f"VERIFY ({job_id}): No downloaded books to verify.")
        return True

    for i, book in enumerate(books):
        asin = book["asin"]
        title = book["title"]
        filepath = book["filepath"]
        expected_min = book["runtime_min"]

        # Calculate progress
        progress = int(((i + 1) / total_books) * 100)
        _yield_progress(f"Verifying: {title}", progress)

        # 0. Split book? The presence of `book_files` rows IS the split flag, and
        # such a book's `filepath` is its folder rather than an audio file — so
        # the single-file checks below cannot be applied to it at all. Rows with a
        # blank `filepath` are dropped, matching every other reader of these rows
        # (sync_logic, processing_logic) — otherwise a hand-written blank row would
        # be "not split" to the sync and "a missing part" to us.
        part_paths = [row["filepath"] for row in get_book_files(asin) if row["filepath"]]
        if part_paths:
            try:
                if _verify_split_book(job_id, asin, title, expected_min, part_paths):
                    issues_found += 1
            except Exception as e:
                # Same policy as the single-file path below: an unexpected error
                # is logged, never auto-marked as a corrupt book.
                log.error(f"VERIFY ({job_id}): Error checking {title}: {e}")
            continue

        # 1. Basic File Check
        if not filepath or not os.path.exists(filepath):
            log.warning(f"VERIFY ({job_id}): Missing file for {title} ({asin})")
            _mark_as_error(asin, "Integrity Check Failed: File missing from disk.")
            issues_found += 1
            continue

        # 2. Duration Check
        if expected_min and expected_min > 0:
            try:
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
                result = subprocess.run(cmd, capture_output=True, text=True)

                if result.returncode != 0:
                    log.warning(f"VERIFY ({job_id}): File corrupt (ffprobe failed) for {title}")
                    _mark_as_error(asin, f"Integrity Check Failed: File corrupt. {result.stderr}")
                    issues_found += 1
                    continue

                actual_sec = float(result.stdout.strip())
                expected_sec = expected_min * 60

                # Tolerance: 5% or 10 minutes
                diff = abs(actual_sec - expected_sec)
                # We only care if it's significantly SHORTER
                if actual_sec < (expected_sec * 0.95) and diff > 600:
                    log.warning(
                        f"VERIFY ({job_id}): Truncated file detected for {title}! "
                        f"Expected {expected_min}m, got {int(actual_sec / 60)}m."
                    )
                    _mark_as_error(
                        asin,
                        f"Integrity Check Failed: Duration mismatch "
                        f"(Expected {expected_min}m, Got {int(actual_sec / 60)}m).",
                    )
                    issues_found += 1
                else:
                    log.debug(f"VERIFY ({job_id}): {title} passed ({int(actual_sec)}s / {expected_sec}s).")

            except Exception as e:
                log.error(f"VERIFY ({job_id}): Error checking {title}: {e}")
                # Don't mark as error automatically on exception, just log it

    log.info(f"VERIFY ({job_id}): Check complete. {issues_found} issues found.")
    _yield_progress(f"Complete. Found {issues_found} issues.", 100)
    return True


def _mark_as_error(asin, message):
    """
    Helper to update DB status to ERROR.

    `retry_count` is set to 1 in the same write (#27). The auto-process gate
    selects errored books with `retry_count <= 1`, so a book flagged here while
    its counter still sits at 0 would otherwise be picked up twice — a re-download
    and then another one after that failed — rather than getting the single
    automatic retry the settings UI promises. Setting it (not incrementing it)
    also keeps a book that fails verification repeatedly from ratcheting past the
    gate forever, since verification failure is about the file on disk, not about
    a download attempt that consumed a retry.
    """
    with get_db_connection() as con:
        con.execute(
            "UPDATE audiobooks SET status = 'ERROR', error_message = ?, retry_count = 1 WHERE asin = ?",
            (message, asin),
        )
        con.commit()

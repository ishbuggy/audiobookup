# audible_downloader/verification_logic.py

import json
import os
import subprocess

from . import announcer
from .db import get_db_connection
from .logger import log


def _yield_progress(status_text, progress):
    payload = {
        "asin": "verify-job",
        "status_text": status_text,
        "progress": progress,
    }
    announcer.announce(f"event: job_update\ndata: {json.dumps(payload)}\n\n")


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
    """Helper to update DB status to ERROR"""
    with get_db_connection() as con:
        con.execute("UPDATE audiobooks SET status = 'ERROR', error_message = ? WHERE asin = ?", (message, asin))
        con.commit()

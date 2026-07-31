# audible_downloader/db.py

import os
import sqlite3
from datetime import datetime, timezone

# Import the centralized path for the database file from the package initializer.
# The '.' makes it a relative import from within the same package.
from . import DB_FILE

# Import the central logger
from .logger import log

# --- Database Helper Functions (Centralized) ---
# This module contains all functions that directly interact with the SQLite database.


def get_db_connection():
    """Establishes and returns a connection to the SQLite database."""
    con = sqlite3.connect(DB_FILE)
    # Use the Row factory to access columns by name
    con.row_factory = sqlite3.Row
    return con


def get_db_stats():
    """Fetches the count of books for each status and returns it as a dictionary."""
    stats = {"DOWNLOADED": 0, "NEW": 0, "MISSING": 0, "ERROR": 0}
    if not os.path.exists(DB_FILE):
        return stats
    con = get_db_connection()
    cur = con.cursor()
    cur.execute("SELECT status, COUNT(*) as count FROM audiobooks GROUP BY status")
    rows = cur.fetchall()
    con.close()
    for row in rows:
        if row["status"] in stats:
            stats[row["status"]] = row["count"]
    return stats


def apply_metadata_overrides(book_dict):
    """
    Layer user metadata overrides onto a book row. The effective title/author
    (the custom value when set, else the native Audible value) become
    `title`/`author` so all existing display code shows them automatically; the
    originals are preserved as `native_title`/`native_author` for the edit UI.
    Mutates and returns the dict.
    """
    native_title = book_dict.get("title")
    native_author = book_dict.get("author")
    book_dict["native_title"] = native_title
    book_dict["native_author"] = native_author
    book_dict["title"] = book_dict.get("custom_title") or native_title
    book_dict["author"] = book_dict.get("custom_author") or native_author
    return book_dict


def get_all_books():
    """Retrieves all books from the database for display in the library."""
    if not os.path.exists(DB_FILE):
        return []
    con = get_db_connection()
    try:
        cur = con.cursor()
        # Select only the columns needed for the main library grid to be efficient.
        # `file_count` is a correlated count of the book's `book_files` rows: 0 for
        # a single-file (or legacy) book, N for a book split into N chapter files.
        # bin/start.sh creates the table unconditionally before the app starts, but
        # a hand-restored library.db is a real thing and a missing child table must
        # not turn the whole library page into a 500 — same tolerance every other
        # `book_files` reader carries.
        cur.execute(
            "SELECT a.author, a.title, a.custom_title, a.custom_author, a.status, a.asin, a.series, a.narrator, "
            "a.runtime_min, a.release_date, a.date_added, a.source, a.is_duplicate, "
            "(SELECT COUNT(*) FROM book_files bf WHERE bf.asin = a.asin) AS file_count "
            "FROM audiobooks a ORDER BY a.author, a.title"
        )
        books_from_db = cur.fetchall()
    except sqlite3.Error as e:
        log.error(f"Database error while fetching the library: {e}", exc_info=True)
        return []
    finally:
        con.close()
    books_with_covers = []
    # Append the cover URL, which is not stored in the DB but follows a known pattern
    for book in books_from_db:
        book_dict = apply_metadata_overrides(dict(book))
        # Provenance: default old rows (pre-`source` column) to the Audible origin.
        if book_dict.get("source") is None:
            book_dict["source"] = "audible"
        # Duplicate flag (v0.19 Phase 1.3): default old rows to not-a-duplicate so
        # the v0.20 grid badge / filter can read it unconditionally.
        if book_dict.get("is_duplicate") is None:
            book_dict["is_duplicate"] = 0
        book_dict["cover_url"] = f"/covers/{book_dict['asin']}_thumb.jpg"
        books_with_covers.append(book_dict)
    return books_with_covers


# --- Split-book part files (`book_files`) ---
# A book that was converted into one file per chapter gets one `book_files` row
# per output file; a normal single-file book has none. That presence/absence IS
# the split flag — there is no column on `audiobooks` saying "this book is split"
# — so every writer below keeps the row set consistent with what is on disk.
# The `filepath` column on `audiobooks` still holds a path for split books too
# (the book's folder), which is why these helpers never touch it: the caller
# updates the parent row, these update the children, ideally together (see the
# shared-connection note on replace_book_files).


def get_book_files(asin):
    """
    Returns the book's part rows as a list of dicts (`part_index`, `filepath`),
    ordered by `part_index` — i.e. in playback order. An empty list means the
    book is not split (a normal single-file book, or one downloaded before this
    feature existed).
    """
    if not os.path.exists(DB_FILE):
        return []

    con = get_db_connection()
    try:
        cur = con.cursor()
        cur.execute("SELECT part_index, filepath FROM book_files WHERE asin = ? ORDER BY part_index", (asin,))
        return [dict(row) for row in cur.fetchall()]
    except sqlite3.Error as e:
        log.error(f"Database error while fetching part files for {asin}: {e}", exc_info=True)
        return []
    finally:
        con.close()


def replace_book_files(asin, paths, con=None):
    """
    Replaces the book's entire part list with `paths`, in order: every existing
    row for the ASIN is deleted and the new list inserted, so no stale part can
    survive a re-download that produced fewer files. `part_index` is the
    ZERO-BASED position in `paths` and exists purely to preserve that order —
    user-facing chapter numbering is rendered separately from the file names.
    Passing an empty `paths` list is therefore the same as deleting the rows,
    which is exactly what converting a split book back to a single file needs.

    Delete and insert always run in ONE transaction, so the row set is never
    observed half-written. Pass an open connection as `con` to enlist in the
    CALLER's transaction — that is how the finalize step writes the `audiobooks`
    row (status/filepath) and these child rows as a single atomic update; the
    caller then owns the commit and the close. With no `con`, this opens its own
    connection and commits. Database errors propagate: a failed part-list write
    must not be mistaken for a successful download.
    """
    rows = [(asin, part_index, path) for part_index, path in enumerate(paths)]

    owns_connection = con is None
    if owns_connection:
        # Same existence guard the read helpers use, and it matters more here:
        # sqlite3.connect CREATES the file, so writing with the database missing
        # would leave a 0-byte stub that start.sh then mistakes for a real
        # database and tries to migrate. A borrowed connection is already open
        # against a real database, so that path skips the check.
        if not os.path.exists(DB_FILE):
            log.error(f"Database not found, skipping part-file write for {asin}.")
            return
        con = get_db_connection()
    try:
        con.execute("DELETE FROM book_files WHERE asin = ?", (asin,))
        if rows:
            con.executemany("INSERT INTO book_files (asin, part_index, filepath) VALUES (?, ?, ?)", rows)
        if owns_connection:
            con.commit()
    finally:
        # Closing without a commit discards the implicit transaction, so an
        # exception on our own connection leaves the old rows untouched.
        if owns_connection:
            con.close()


def delete_book_files(asin):
    """
    Removes every part row for the ASIN, marking the book as no longer split
    (used when its files are deleted or it is re-downloaded unsplit). Deleting
    rows that don't exist is a no-op, so this is safe to call unconditionally.
    """
    # No database means no part rows to remove — and connecting would create an
    # empty file rather than delete anything (see replace_book_files).
    if not os.path.exists(DB_FILE):
        log.error(f"Database not found, skipping part-file delete for {asin}.")
        return

    con = get_db_connection()
    try:
        con.execute("DELETE FROM book_files WHERE asin = ?", (asin,))
        con.commit()
    finally:
        con.close()


def get_all_tracked_part_paths():
    """
    Returns a flat list of every part `filepath` known to the database, across
    all books. Filesystem scanners use it to tell a file the app produced from a
    stray one, for which the single `audiobooks.filepath` column is not enough
    once a book can own many files.
    """
    if not os.path.exists(DB_FILE):
        return []

    con = get_db_connection()
    try:
        cur = con.cursor()
        cur.execute("SELECT filepath FROM book_files")
        return [row["filepath"] for row in cur.fetchall()]
    except sqlite3.Error as e:
        log.error(f"Database error while fetching tracked part paths: {e}", exc_info=True)
        return []
    finally:
        con.close()


def cleanup_stale_jobs():
    """Finds and fails any jobs left in a 'QUEUED' or 'RUNNING' state from a previous run."""
    log.info("Running startup cleanup for stale jobs...")
    # Check for DB file existence before attempting to connect
    if not os.path.exists(DB_FILE):
        log.info("Database not found, skipping stale job cleanup.")
        return

    con = get_db_connection()
    try:
        # Check if the 'jobs' table exists to prevent errors on a fresh DB
        table_check = con.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='jobs'").fetchone()
        if not table_check:
            log.info("'jobs' table not found, skipping stale job cleanup.")
            con.close()
            return

        stale_jobs = con.execute("SELECT job_id FROM jobs WHERE status = 'RUNNING' OR status = 'QUEUED'").fetchall()
        if not stale_jobs:
            log.info("No stale jobs found.")
            return

        stale_job_ids = [job["job_id"] for job in stale_jobs]
        log.info(f"Found stale jobs to clean up: {stale_job_ids}")

        end_time_iso = datetime.now(timezone.utc).isoformat()
        placeholders = ",".join("?" for _ in stale_job_ids)

        con.execute(
            f"UPDATE job_items SET status = 'FAILED', log = 'Job failed due to application restart.' "
            f"WHERE job_id IN ({placeholders}) AND status IN ('QUEUED', 'PROCESSING')",
            stale_job_ids,
        )
        con.execute(
            f"UPDATE jobs SET status = 'FAILED', end_time = ? WHERE job_id IN ({placeholders})",
            [end_time_iso] + stale_job_ids,
        )
        con.commit()
        log.info(f"Successfully cleaned up {len(stale_job_ids)} stale job(s).")
    except sqlite3.Error as e:
        log.error(f"Database error during stale job cleanup: {e}")
    finally:
        con.close()


def _get_books_by_status(statuses, include_errored_retries=False):
    """
    A private helper function to fetch books with specific statuses.
    This is the core query logic for both automatic and manual jobs.

    Args:
        statuses (list): A list of statuses to query for (e.g., ['NEW', 'MISSING']).
        include_errored_retries (bool): If True, ignores the retry_count for ERROR books
                                        (manual selection offers every errored book).
                                        If False, ERROR books are limited to the
                                        one automatic re-download attempt below.
    """
    if not os.path.exists(DB_FILE) or not statuses:
        return []

    # Build the WHERE clause dynamically.
    conditions = []
    has_error_status = "ERROR" in statuses

    # Create a new list of statuses without ERROR to handle it specially
    other_statuses = [s for s in statuses if s != "ERROR"]

    if other_statuses:
        # Use IN operator for a cleaner query for NEW, MISSING, etc.
        placeholders = ",".join("?" for _ in other_statuses)
        conditions.append(f"status IN ({placeholders})")

    if has_error_status:
        if include_errored_retries:
            # For manual selection, get all ERROR books
            conditions.append("status = 'ERROR'")
        else:
            # For automatic jobs, only get ERROR books that still have their one
            # automatic re-download left. The counter rises by one per failure
            # (processing_logic.py `_update_db_on_failure`) and is only reset by a
            # success or a manually started job, so:
            #   0 -> first failure hasn't happened yet (or was manually re-armed)
            #   1 -> failed once; this is the one automatic retry the settings UI
            #        promises, and it runs without clearing the counter
            #   2+ -> the retry failed too; never selected automatically again
            # Plain comparison, deliberately NOT COALESCE: a legacy row whose
            # retry_count is NULL matches neither, which is the safe direction
            # (it is simply never auto-retried) and is the behavior these rows
            # have always had.
            conditions.append("(status = 'ERROR' AND retry_count <= 1)")

    # We must have other_statuses for the IN clause, so we pass them in order
    params = other_statuses
    where_clause = " OR ".join(conditions)
    query = f"SELECT asin, title, author FROM audiobooks WHERE {where_clause} ORDER BY title ASC"

    con = get_db_connection()
    try:
        cur = con.cursor()
        cur.execute(query, params)
        books_from_db = [dict(book) for book in cur.fetchall()]
        return books_from_db
    except sqlite3.Error as e:
        log.error(f"Database error while fetching downloadable books: {e}", exc_info=True)
        return []
    finally:
        con.close()


def get_books_for_auto_job(settings):
    """
    Public function to get a list of books for an AUTOMATIC download job,
    based on the user's settings.
    """
    statuses_to_fetch = []
    if settings.get("tasks", {}).get("auto_process_new", False):
        statuses_to_fetch.append("NEW")
    if settings.get("tasks", {}).get("auto_process_missing", False):
        statuses_to_fetch.append("MISSING")
    if settings.get("tasks", {}).get("auto_process_error", False):
        statuses_to_fetch.append("ERROR")

    # For automatic jobs, we never include errored retries.
    return _get_books_by_status(statuses_to_fetch, include_errored_retries=False)


def get_books_for_download_modal():
    """
    Public function to get categorized lists of books for the MANUAL download modal.
    Returns lists for NEW, MISSING, and ERROR statuses separately.
    """
    # Fetch each category of book using our private helper function.
    new_books = _get_books_by_status(["NEW"])
    missing_books = _get_books_by_status(["MISSING"])

    # For manual selection, we want ALL errored books, regardless of retry count.
    errored_books = _get_books_by_status(["ERROR"], include_errored_retries=True)

    # Return a dictionary with three distinct keys for each category.
    return {"new": new_books, "missing": missing_books, "errored": errored_books}

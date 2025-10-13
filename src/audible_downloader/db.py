# audible_downloader/db.py

import os
import sqlite3
from datetime import datetime

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


def get_all_books():
    """Retrieves all books from the database for display in the library."""
    if not os.path.exists(DB_FILE):
        return []
    con = get_db_connection()
    cur = con.cursor()
    # Select only the columns needed for the main library grid to be efficient
    cur.execute(
        "SELECT author, title, status, asin, series, narrator, runtime_min, release_date, date_added "
        "FROM audiobooks ORDER BY author, title"
    )
    books_from_db = cur.fetchall()
    con.close()
    books_with_covers = []
    # Append the cover URL, which is not stored in the DB but follows a known pattern
    for book in books_from_db:
        book_dict = dict(book)
        book_dict["cover_url"] = f"/covers/{book_dict['asin']}_thumb.jpg"
        books_with_covers.append(book_dict)
    return books_with_covers


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

        end_time_iso = datetime.utcnow().isoformat()
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
        include_errored_retries (bool): If True, ignores the retry_count for ERROR books.
                                        This is for manual selection.
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
            # For automatic jobs, only get ERROR books that haven't been retried
            conditions.append("(status = 'ERROR' AND retry_count = 0)")

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

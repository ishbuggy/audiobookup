# audible_downloader/sync_logic.py

import json
import os
import sqlite3
import subprocess

import requests  # type: ignore

from . import DATABASE_DIR

# Import necessary components from our other modules
from .db import get_db_connection
from .logger import log


# --- Private Helper: Yields progress updates in the correct format ---
def _yield_progress(status_text, progress, stage_text=None):
    """
    A helper generator to format and yield progress updates.
    Optionally includes a stage_text for major phase updates.
    """
    payload = {
        "asin": "sync-job",  # A consistent fake ASIN for the UI to track the sync job
        "status_text": status_text,
        "progress": progress,
    }
    # NEW: If stage_text is provided, add it to the payload.
    if stage_text:
        payload["stage_text"] = stage_text

    yield f"EVENT_SYNC_UPDATE:{json.dumps(payload)}"


# --- Private Helper 1: Fetch and update books from Audible API ---
def _fetch_and_update_from_audible(job_id, sync_mode="DEEP"):
    """
    Generator that fetches the full library from Audible's API,
    processes covers, and inserts/updates book records in the database.
    Yields progress updates and returns the total number of books found.
    """
    # --- START: MODIFICATION ---
    # The stage text and progress calculations now depend on the sync mode.
    stage_text = "Phase 1/1: Fetching from Audible" if sync_mode == "FAST" else "Phase 1/3: Fetching from Audible"
    yield from _yield_progress("Fetching library from Audible...", 5, stage_text=stage_text)
    # --- END: MODIFICATION ---

    all_items = []
    page = 1
    page_size = 1000

    while True:
        try:
            endpoint = (
                f"/1.0/library?num_results={page_size}&page={page}"
                f"&response_groups=media,contributors,series,product_attrs,product_desc"
            )
            command = ["audible", "api", endpoint]
            env = os.environ.copy()
            env["HOME"] = DATABASE_DIR
            result = subprocess.run(command, capture_output=True, text=True, check=True, encoding="utf-8", env=env)
            response = json.loads(result.stdout)
            items_on_page = response.get("items", [])
            if not items_on_page:
                break
            all_items.extend(items_on_page)
            page += 1
        except (subprocess.CalledProcessError, json.JSONDecodeError, KeyError) as e:
            log.error(f"SYNC-LOGIC ({job_id}): API fetch failed: {e}")
            yield from _yield_progress("Error: API fetch failed", 100)
            raise RuntimeError("Could not fetch library from Audible API.")

    total_books = len(all_items)
    log.info(f"SYNC-LOGIC ({job_id}): Processing {total_books} total books from library.")
    CONFIG_DIR = "/config"
    COVERS_DIR = os.path.join(CONFIG_DIR, "covers")
    os.makedirs(COVERS_DIR, exist_ok=True)

    with get_db_connection() as con:
        cur = con.cursor()
        new_from_audible, updated_in_db, items_processed = 0, 0, 0
        for item in all_items:
            items_processed += 1
            # --- START: MODIFICATION ---
            # Progress range is adjusted based on whether this is the only step or the first of three.
            progress_range = 90 if sync_mode == "FAST" else 40
            progress = 5 + int((items_processed / total_books) * progress_range)
            if items_processed % 5 == 0 or items_processed == total_books:
                status_text = f"Processing book {items_processed}/{total_books}"
                yield from _yield_progress(status_text, progress, stage_text=stage_text)
            # --- END: MODIFICATION ---

            asin = item.get("asin")
            if not asin:
                continue

            cover_url = item.get("product_images", {}).get("500")
            original_cover_path = os.path.join(COVERS_DIR, f"{asin}_original.jpg")
            thumb_cover_path = os.path.join(COVERS_DIR, f"{asin}_thumb.jpg")
            if not os.path.exists(thumb_cover_path) and cover_url:
                try:
                    response = requests.get(cover_url, stream=True)
                    response.raise_for_status()
                    with open(original_cover_path, "wb") as f:
                        for chunk in response.iter_content(chunk_size=8192):
                            f.write(chunk)
                    # Ruff E501: Break long command list into multiple lines
                    ffmpeg_command = [
                        "ffmpeg", "-y", "-i", original_cover_path,
                        "-vf", "scale=200:200", thumb_cover_path
                    ]
                    subprocess.run(ffmpeg_command, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
                except (requests.exceptions.RequestException, subprocess.CalledProcessError) as e:
                    log.warning(f"SYNC-LOGIC ({job_id}): Could not process cover for {asin}: {e}")

            title = item.get("title", "N/A")
            authors_list = item.get("authors")
            author = authors_list[0].get("name", "N/A") if authors_list else "N/A"
            series_list = item.get("series")
            series = series_list[0].get("title", "N/A") if series_list else "N/A"
            narrators_list = item.get("narrators")
            narrator = narrators_list[0].get("name", "N/A") if narrators_list else "N/A"
            runtime_min, release_date, publisher, language, purchase_date = (
                item.get("runtime_length_min", 0),
                item.get("release_date", "N/A"),
                item.get("publisher_name", "N/A"),
                item.get("language", "N/A"),
                item.get("purchase_date", "N/A"),
            )
            summary_raw = item.get("merchandising_summary") or ""
            summary = summary_raw.replace("</p>", "\n").replace("<p>", "").replace("<br />", "\n").strip()
            if not summary:
                summary = "N/A"
            date_added = item.get("library_status", {}).get("date_added", "N/A")

            cur.execute("SELECT COUNT(*) FROM audiobooks WHERE asin = ?", (asin,))
            exists = cur.fetchone()[0]
            if exists == 0:
                new_from_audible += 1
                cur.execute(
                    (
                        "INSERT INTO audiobooks (asin, author, title, status, series, narrator, "
                        "runtime_min, release_date, publisher, language, purchase_date, summary, date_added) "
                        "VALUES (?, ?, ?, 'NEW', ?, ?, ?, ?, ?, ?, ?, ?, ?)"
                    ),
                    # Ruff E501: Break long tuple of arguments into multiple lines
                    (
                        asin, author, title, series, narrator, runtime_min, release_date, publisher,
                        language, purchase_date, summary, date_added
                    ),
                )
            else:
                updated_in_db += 1
                cur.execute(
                    (
                        "UPDATE audiobooks SET author = ?, title = ?, series = ?, narrator = ?, runtime_min = ?, "
                        "release_date = ?, publisher = ?, language = ?, purchase_date = ?, date_added = ?, "
                        "summary = CASE WHEN is_summary_full = 1 THEN summary ELSE ? END "
                        "WHERE asin = ?"
                    ),
                    # Ruff E501: Break long tuple of arguments into multiple lines
                    (
                        author, title, series, narrator, runtime_min, release_date, publisher,
                        language, purchase_date, date_added, summary, asin
                    ),
                )
        con.commit()
        log.info(f"SYNC-LOGIC ({job_id}): Found {new_from_audible} new. Updated {updated_in_db} existing.")

    return total_books


# --- Private Helper 2: Scan the local filesystem for .m4b files ---
def _scan_local_filesystem(job_id):
    """
    Generator that scans the /data directory for .m4b files, using a cache
    to speed up the process. Yields progress updates and returns a dictionary
    mapping ASINs to file paths.
    """
    yield from _yield_progress("Scanning local files...", 50, stage_text="Phase 2/3: Scanning Filesystem")
    CONFIG_DIR = "/config"
    AUDIOBOOK_LIBRARY_PATH = "/data"
    CACHE_FILE = os.path.join(CONFIG_DIR, ".file_scan_cache")
    cache = {}
    if os.path.exists(CACHE_FILE):
        with open(CACHE_FILE, encoding="utf-8") as f:
            for line in f:
                parts = line.strip().split("|", 2)
                if len(parts) == 3:
                    cache[parts[2]] = {"mtime": parts[0], "asin": parts[1]}

    found_files = {}
    new_cache_lines = []
    files_processed, cache_hits = 0, 0

    # Ruff E501: Rewrite long list comprehension as a standard loop for readability
    files_to_scan = []
    for r, _, fs in os.walk(AUDIOBOOK_LIBRARY_PATH):
        for f in fs:
            if f.endswith(".m4b"):
                files_to_scan.append(os.path.join(r, f))

    total_files_to_scan = len(files_to_scan)

    for filepath in files_to_scan:
        files_processed += 1
        if total_files_to_scan > 0:
            # This is phase 2/3, so progress goes from 50% to 95%
            progress = 50 + int((files_processed / total_files_to_scan) * 45)
            if files_processed % 5 == 0 or files_processed == total_files_to_scan:
                status_text = f"Scanning local files... ({files_processed}/{total_files_to_scan})"
                yield from _yield_progress(status_text, progress, stage_text="Phase 2/3: Scanning Filesystem")
        try:
            current_mtime = str(int(os.path.getmtime(filepath)))
            if filepath in cache and cache[filepath]["mtime"] == current_mtime:
                asin = cache[filepath]["asin"]
                cache_hits += 1
            else:
                # Ruff E501: Break long command list into multiple lines
                ffprobe_cmd = [
                    "ffprobe", "-v", "quiet", "-show_entries", "format_tags=asin",
                    "-of", "default=noprint_wrappers=1:nokey=1", filepath
                ]
                result = subprocess.run(ffprobe_cmd, capture_output=True, text=True, check=True)
                asin = result.stdout.strip()
            if asin:
                found_files[asin] = filepath
                new_cache_lines.append(f"{current_mtime}|{asin}|{filepath}\n")
        except (OSError, subprocess.CalledProcessError) as e:
            log.warning(f"SYNC-LOGIC ({job_id}): Could not process file '{filepath}': {e}")

    with open(CACHE_FILE, "w", encoding="utf-8") as f:
        f.writelines(new_cache_lines)
    log.info(f"SYNC-LOGIC ({job_id}): Scan complete. Processed {files_processed} files ({cache_hits} from cache).")

    return found_files

# --- Private Helper 3: Reconcile DB with filesystem scan ---
def _reconcile_database(job_id, found_files):
    """
    Generator that compares the DB state with the filesystem scan results.
    It marks books as MISSING if their file is gone, and marks them as
    DOWNLOADED if the file is found but the DB state is wrong.
    """
    yield from _yield_progress("Reconciling database...", 95, stage_text="Phase 3/3: Reconciling Database")
    marked_missing, fixed_untracked = 0, 0
    with get_db_connection() as con:
        cur = con.cursor()
        downloaded_books = cur.execute("SELECT asin, filepath FROM audiobooks WHERE status = 'DOWNLOADED'").fetchall()
        for book in downloaded_books:
            if not book["filepath"] or not os.path.exists(book["filepath"]):
                cur.execute("UPDATE audiobooks SET status = 'MISSING', filepath = '' WHERE asin = ?", (book["asin"],))
                marked_missing += 1

        for asin_from_file, correct_filepath in found_files.items():
            db_status_row = cur.execute("SELECT status FROM audiobooks WHERE asin = ?", (asin_from_file,)).fetchone()
            if db_status_row and db_status_row["status"] != "DOWNLOADED":
                cur.execute(
                    "UPDATE audiobooks SET status = 'DOWNLOADED', filepath = ? WHERE asin = ?",
                    (correct_filepath, asin_from_file),
                )
                fixed_untracked += 1
        con.commit()
    # Ruff E501: Break long f-string into multiple lines
    log.info(
        f"SYNC-LOGIC ({job_id}): Sync complete. "
        f"Marked Missing: {marked_missing}, Untracked Fixed: {fixed_untracked}"
    )


# --- Main Public Function ---
def run_sync_logic(job_id, sync_mode="DEEP"):
    """
    A generator function that orchestrates the entire library sync process.

    Args:
        job_id (int): The ID of the current job for logging.
        sync_mode (str): The type of sync to perform. Can be "DEEP" or "FAST".
                         "DEEP" includes a full filesystem scan.
                         "FAST" only fetches updates from the Audible API.

    Yields:
        str: Real-time event and log lines from the helper generators.
    """
    log.info(f"SYNC-LOGIC ({job_id}): Starting Python-based sync process (Mode: {sync_mode}).")
    try:
        yield from _yield_progress("Initializing...", 2)

        # Pass the sync_mode to the helper.
        yield from _fetch_and_update_from_audible(job_id, sync_mode)

        # If this is just a FAST sync, our work is done.
        if sync_mode == "FAST":
            log.info(f"SYNC-LOGIC ({job_id}): Fast sync complete. Skipping filesystem scan.")
            # The helper has already yielded the final progress for a fast sync, so we just return.
            return True

        # For a DEEP sync, continue with the filesystem-intensive phases.
        found_files_map = yield from _scan_local_filesystem(job_id)

        yield from _reconcile_database(job_id, found_files_map)

        yield from _yield_progress("Finishing up...", 100, stage_text="Phase 3/3: Reconciling Database")
        return True

    except (RuntimeError, sqlite3.Error) as e:
        log.error(f"SYNC-LOGIC ({job_id}): A critical error occurred during sync: {e}", exc_info=True)
        yield from _yield_progress(f"Error: {e}", 100)
        return False

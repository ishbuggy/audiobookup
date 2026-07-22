import json
import math
import os
import re
import shutil
import sqlite3
import subprocess
import tempfile
import threading
import uuid
from collections import deque
from datetime import datetime, timedelta, timezone
from urllib.parse import urlsplit

from flask import (  # type: ignore
    Response,
    flash,
    jsonify,
    redirect,
    render_template,
    request,
    send_file,
    send_from_directory,
    session,
    url_for,
)
from werkzeug.security import generate_password_hash  # type: ignore

# --- App instance import ---
# The app and socketio instances are created in __init__.py, imported here to register routes and event handlers.
from audible_downloader import (
    COVERS_DIR,
    DATABASE_DIR,
    DB_FILE,
    LOG_FILE,
    MAX_LOG_LINES,
    SETUP_FLAG_FILE,
    announcer,
    app,
    settings_changed_event,
)

# --- Import the auth module and its functions ---
from audible_downloader.auth import login_required, verify_credentials

# Import the database helper functions from our new db module
from audible_downloader.db import (
    apply_metadata_overrides,
    get_all_books,
    get_books_for_download_modal,
    get_db_connection,
    get_db_stats,
)

# Import the authentication health check module
from audible_downloader.health_check import get_audible_auth_status, perform_audible_auth_check

# Import the manual-import (FR2) helpers
from audible_downloader.import_logic import IMPORTABLE_EXTS, adopt_upload, import_staging_dir

# Import from the job_manager module
from audible_downloader.job_manager import cancel_active_job, start_new_job

# Import from the logging module
from audible_downloader.logger import log

# Import the settings functions from the settings module
from audible_downloader.settings import deep_update, load_settings, save_settings

# Import the global task_runner instance
from audible_downloader.task_runner import task_runner


# --- CSRF Protection: Origin Validation ---
# All modern browsers send an Origin header on cross-site state-changing
# requests, so rejecting mismatched Origins blocks browser-based CSRF without
# any token plumbing in the frontend. Requests without an Origin header
# (curl, same-origin navigations in older browsers) are allowed through.
# Only the hosts are compared — the scheme is deliberately ignored so an
# HTTPS-terminating reverse proxy in front of the plain-HTTP container still
# passes. Note for proxy users: the proxy must forward the Host header
# (standard practice), or every write request will be rejected here.
@app.before_request
def reject_cross_origin_writes():
    if request.method in ("GET", "HEAD", "OPTIONS"):
        return None

    origin = request.headers.get("Origin")
    if not origin:
        return None

    # An Origin of "null" (sandboxed iframes, some redirect chains) has no
    # host and is treated as a mismatch.
    origin_host = urlsplit(origin).netloc
    if origin_host and origin_host.lower() == request.host.lower():
        return None

    log.warning(
        f"SECURITY: Blocked cross-origin {request.method} to {request.path}: "
        f"Origin host '{origin_host or origin}' does not match request host '{request.host}'."
    )
    return jsonify({"error": "Cross-origin request blocked."}), 403


# --- Helper Functions ---
def format_bytes(size_bytes):
    if size_bytes == 0:
        return "0B"
    size_name = ("B", "KB", "MB", "GB", "TB")
    i = int(math.floor(math.log(size_bytes, 1024)))
    p = math.pow(1024, i)
    s = round(size_bytes / p, 2)
    return f"{s} {size_name[i]}"


@app.route("/")
@login_required
def index():
    stats = get_db_stats()
    books = get_all_books()
    # Read the log file directly
    log_history = ""
    if os.path.exists(LOG_FILE):
        with open(LOG_FILE, encoding="utf-8") as f:
            log_history = "".join(deque(f, MAX_LOG_LINES))
    settings = load_settings()
    server_version = os.getenv("APP_VERSION", "local-dev")
    return render_template(
        "index.html",
        stats=stats,
        books=books,
        log_history=log_history,
        settings=settings,
        server_version=server_version,
    )


@app.route("/setup")
@login_required
def setup():
    # This function's only job is to render the page.
    return render_template("setup.html")


@app.route("/settings", methods=["GET"])  # Remove POST method
@login_required
def settings():
    """Renders the settings page."""
    # This function now only handles rendering the page. All save logic is in the API.
    current_settings = load_settings()
    server_version = os.getenv("APP_VERSION", "local-dev")
    return render_template("settings.html", settings=current_settings, server_version=server_version)


@app.route("/history")
@login_required
def history():
    """Renders the dedicated job history page."""
    server_version = os.getenv("APP_VERSION", "local-dev")
    return render_template("history.html", server_version=server_version)


## The SSE stream endpoint
@app.route("/api/settings", methods=["GET"])
@login_required
def get_settings():
    return jsonify(load_settings())


@app.route("/api/settings", methods=["POST"])
@login_required
def post_settings():
    """Receives a JSON object of settings, processes credentials, and saves."""
    new_settings = request.get_json()
    if not isinstance(new_settings, dict):
        return jsonify(error="Invalid data format"), 400

    current_settings = load_settings()

    # Flag to track if we need to force a logout
    credentials_changed = False

    # --- Securely handle credentials from the payload ---

    # Check if username was changed
    if "username" in new_settings and new_settings["username"] != current_settings.get("username"):
        credentials_changed = True
        log.info("SETTINGS: Administrator username has been updated.")

    # Check if password was changed
    if "password" in new_settings:
        new_password = new_settings["password"]
        # Only update the hash if the user actually entered a new password.
        if new_password:
            # We add a validation check here for robustness
            if len(new_password) < 8:
                return jsonify(error="New password must be at least 8 characters long."), 400
            current_settings["password_hash"] = generate_password_hash(new_password)
            credentials_changed = True
            log.info("SETTINGS: Administrator password has been updated.")

        # Always delete the temporary plain-text key before saving.
        del new_settings["password"]

    # --- Merge the rest of the settings ---
    updated_settings = deep_update(current_settings, new_settings)

    if save_settings(updated_settings):
        log.info("SETTINGS: Application settings have been updated via the API.")

        # This makes concurrency changes take effect immediately without a restart.
        task_runner.reconfigure()

        # Signal the scheduler that settings might have changed.
        settings_changed_event.set()

        if credentials_changed:
            session.pop("username", None)
            # The JS will handle the redirect, but we confirm the logout happened.
            return jsonify(success=True, message="Credentials updated, user logged out.")

        return jsonify(success=True, message="Settings saved successfully.")
    else:
        return jsonify(error="Failed to save settings."), 500


@app.route("/api/book/<string:asin>")
@login_required
def get_book_details(asin):
    if not os.path.exists(DB_FILE):
        return jsonify(error="Database not found."), 404
    con = get_db_connection()
    cur = con.cursor()
    cur.execute("SELECT * FROM audiobooks WHERE asin = ?", (asin,))
    book_from_db = cur.fetchone()
    con.close()
    if book_from_db is None:
        return jsonify(error="Book not found."), 404
    book_dict = apply_metadata_overrides(dict(book_from_db))
    if book_dict.get("is_summary_full") is None:
        book_dict["is_summary_full"] = 0
    if book_dict.get("is_duplicate") is None:
        book_dict["is_duplicate"] = 0
    # Provenance: default old rows (pre-`source` column) to the Audible origin.
    if book_dict.get("source") is None:
        book_dict["source"] = "audible"
    original_cover_path = f"/covers/{book_dict['asin']}_original.jpg"
    thumb_cover_path = f"/covers/{book_dict['asin']}_thumb.jpg"
    if os.path.exists(os.path.join(COVERS_DIR, f"{book_dict['asin']}_original.jpg")):
        book_dict["cover_url_original"] = original_cover_path
    else:
        book_dict["cover_url_original"] = thumb_cover_path
    file_path = book_dict.get("filepath")
    if file_path and os.path.exists(file_path):
        try:
            stat_info = os.stat(file_path)
            book_dict["file_size_hr"] = format_bytes(stat_info.st_size)
            book_dict["file_mtime_hr"] = datetime.fromtimestamp(stat_info.st_mtime).strftime("%Y-%m-%d %H:%M:%S")
            book_dict["file_type"] = ".m4b Audiobook"
        except Exception as e:
            log.warning(f"Could not get file stats for {file_path}: {e}")
            book_dict["file_size_hr"] = "Error"
    else:
        book_dict["file_size_hr"] = "N/A"
        book_dict["file_mtime_hr"] = "N/A"
        book_dict["file_type"] = "N/A"
    return jsonify(book_dict)


@app.route("/api/jobs/stream")
@login_required
def job_stream():
    def stream_events():
        # Each client gets its own queue to listen on.
        q = announcer.listen()
        try:
            while True:
                # Block until a message is available.
                msg = q.get()
                yield msg
        finally:
            # Runs when the client disconnects (Werkzeug closes the generator),
            # unsubscribing the dead queue right away.
            announcer.remove(q)

    return Response(stream_events(), mimetype="text/event-stream")


@app.route("/api/jobs/active")
@login_required
def get_active_job():
    """
    Checks if there is a job currently in a running or queued state.
    If so, returns the job details and all its associated items.
    """
    con = get_db_connection()
    # Find any job that is not in a final state. NOW SELECTING job_type AS WELL.
    job_row = con.execute(
        "SELECT job_id, status, job_type FROM jobs "
        "WHERE status = 'RUNNING' OR status = 'QUEUED' "
        "ORDER BY job_id DESC LIMIT 1"
    ).fetchone()
    if not job_row:
        con.close()
        return jsonify({})  # No active job
    job_id = job_row["job_id"]
    job_type = job_row["job_type"]  # Get the job type

    items_list = []
    # Only fetch items if it's a DOWNLOAD job
    if job_type == "DOWNLOAD":
        item_rows = con.execute(
            """
            SELECT i.asin, i.status, a.title, a.author
            FROM job_items i JOIN audiobooks a ON i.asin = a.asin
            WHERE i.job_id = ?
        """,
            (job_id,),
        ).fetchall()
        for item in item_rows:
            item_dict = dict(item)
            item_dict["cover_url"] = f"/covers/{item_dict['asin']}_thumb.jpg"
            items_list.append(item_dict)

    con.close()
    # ADD job_type TO THE RESPONSE
    return jsonify({"job_id": job_id, "status": job_row["status"], "job_type": job_type, "items": items_list})


@app.route("/api/jobs/history")
@login_required
def get_job_history():
    """Retrieves a paginated, filtered, and searchable list of jobs."""
    # --- 1. Get all parameters from the request query string ---
    page = request.args.get("page", 1, type=int)
    job_type = request.args.get("job_type", None, type=str)
    job_status = request.args.get("job_status", None, type=str)
    search_term = request.args.get("search_term", None, type=str)

    per_page = 50
    offset = (page - 1) * per_page

    # --- 2. Dynamically build the SQL query ---
    # We build the query in parts to safely handle different combinations of filters.
    params = []
    base_from = "FROM jobs j"
    # The base condition is to always exclude jobs that are still active.
    where_conditions = ["j.status NOT IN ('RUNNING', 'QUEUED')"]

    # If a search term is provided, the query becomes more complex.
    if search_term:
        # We must join across three tables to link jobs to book titles/authors.
        base_from += " JOIN job_items i ON j.job_id = i.job_id JOIN audiobooks a ON i.asin = a.asin"
        # The search condition checks multiple book fields.
        where_conditions.append("(a.title LIKE ? OR a.author LIKE ?)")
        search_pattern = f"%{search_term}%"
        params.extend([search_pattern, search_pattern])
        # Use DISTINCT to prevent a job from appearing multiple times if it has multiple matching books.
        select_prefix = "SELECT DISTINCT j.job_id, j.status, j.job_type, j.start_time, j.end_time"
        count_prefix = "SELECT COUNT(DISTINCT j.job_id)"
    else:
        # Without a search, the query is simpler.
        select_prefix = "SELECT j.job_id, j.status, j.job_type, j.start_time, j.end_time"
        count_prefix = "SELECT COUNT(j.job_id)"

    # Add optional filters for job type and status.
    if job_type:
        where_conditions.append("j.job_type = ?")
        params.append(job_type)
    if job_status:
        where_conditions.append("j.status = ?")
        params.append(job_status)

    where_clause = " AND ".join(where_conditions)

    # --- 3. Execute the queries ---
    jobs_list = []
    total_jobs = 0
    con = get_db_connection()
    try:
        # First, run the count query with the same filters to get the total for pagination.
        count_query = f"{count_prefix} {base_from} WHERE {where_clause}"
        total_jobs = con.execute(count_query, tuple(params)).fetchone()[0]

        # Then, run the main query to get the jobs for the current page.
        main_query = f"{select_prefix} {base_from} WHERE {where_clause} ORDER BY j.start_time DESC LIMIT ? OFFSET ?"
        job_rows = con.execute(main_query, tuple(params + [per_page, offset])).fetchall()

        # --- 4. Fetch associated items for each job (same as before) ---
        for job in job_rows:
            job_dict = dict(job)
            item_rows = con.execute(
                """
                SELECT i.asin, i.status, a.title
                FROM job_items i
                LEFT JOIN audiobooks a ON i.asin = a.asin
                WHERE i.job_id = ?
                """,
                (job["job_id"],),
            ).fetchall()
            items = []
            for item in item_rows:
                item_dict = dict(item)
                if item_dict["title"] is None:
                    item_dict["title"] = f"[Deleted Book (ASIN: {item_dict['asin']})]"
                items.append(item_dict)
            job_dict["items"] = items
            jobs_list.append(job_dict)

    except sqlite3.Error as e:
        log.error(f"Database error fetching job history: {e}", exc_info=True)
        return jsonify(error="Failed to retrieve job history."), 500
    finally:
        con.close()

    return jsonify(
        {
            "jobs": jobs_list,
            "total_jobs": total_jobs,
            "page": page,
            "per_page": per_page,
            "total_pages": math.ceil(total_jobs / per_page),
        }
    )


@app.route("/api/audible_auth_status")
@login_required
def audible_auth_status():
    """
    Returns the latest cached authentication status determined by the
    background health check thread.
    """
    status = get_audible_auth_status()
    # If the check hasn't run yet, default to valid to avoid showing an error on first load.
    # The background thread will run the check immediately on startup anyway.
    if status.get("is_valid") is None:
        return jsonify({"is_valid": True})

    return jsonify(status)


@app.route("/api/run_audible_auth_check", methods=["POST"])
@login_required
def run_audible_auth_check():
    """
    Manually triggers an authentication check and returns the fresh result.
    """
    log.info("API: Manual Audible connection check triggered by user.")
    # Call the function to perform the check synchronously
    perform_audible_auth_check()
    # Get the newly updated status
    status = get_audible_auth_status()
    return jsonify(status)


@app.route("/api/get_cpu_cores")
@login_required
def get_cpu_cores():
    """
    Detects the number of available CPU cores, respecting container cgroup limits.

    --- Attribution: Immich Project ---
    The logic for detecting CPU cores within a cgroup-limited container
    is adapted from the startup script of the Immich project.
    - Source: https://github.com/immich-app/immich
    - License: GNU Affero General Public License v3.0
    """
    try:
        quota = -1
        period = -1
        cpus = 0

        # --- Check for cgroup v2 ---
        if os.path.exists("/sys/fs/cgroup/cpu.max"):
            cpu_max = open("/sys/fs/cgroup/cpu.max").read().strip().split()
            if len(cpu_max) == 2 and cpu_max[0] != "max":
                quota = int(cpu_max[0])
                period = int(cpu_max[1])

        # --- Check for cgroup v1 (if v2 not found) ---
        elif os.path.exists("/sys/fs/cgroup/cpu/cpu.cfs_quota_us"):
            quota_str = open("/sys/fs/cgroup/cpu/cpu.cfs_quota_us").read().strip()
            quota = int(quota_str)
            if quota != -1 and os.path.exists("/sys/fs/cgroup/cpu/cpu.cfs_period_us"):
                period_str = open("/sys/fs/cgroup/cpu/cpu.cfs_period_us").read().strip()
                period = int(period_str)

        # --- Calculate CPUs from cgroup limits ---
        if quota > -1 and period > 0:
            cpus = math.floor(quota / period)

        # --- Fallback to os.cpu_count() if no cgroup limits are found ---
        if cpus == 0:
            # os.cpu_count() is a reliable cross-platform way to get total cores
            cpus = os.cpu_count() or 1  # Fallback to 1 if detection fails

        # Ensure we always have at least 1 core
        cpus = max(1, cpus)

        # The recommended concurrency is one less than the core count, but never less than 1.
        recommended_concurrency = max(1, cpus - 1)

        log.info(f"CPU detection: Found {cpus} available cores. Recommending concurrency of {recommended_concurrency}.")

        return jsonify({"success": True, "total_cores": cpus, "recommended_concurrency": recommended_concurrency})

    except Exception as e:
        log.error(f"Failed to detect CPU cores: {e}", exc_info=True)
        # Fallback to a safe default on any error
        return jsonify({"success": False, "error": str(e), "recommended_concurrency": 2}), 500


@app.route("/api/run_scheduled_job_now", methods=["POST"])
@login_required
def run_scheduled_job_now():
    """
    Manually triggers a scheduled job type to run immediately for testing.
    """
    data = request.get_json()
    job_type = data.get("job_type")

    log.info(f"API: Manual 'Run Now' triggered for job type: {job_type}")

    if job_type == "SYNC":
        success, result = start_new_job("SYNC")
    elif job_type == "PROCESS":
        success, result = start_new_job("DOWNLOAD", asins=None)
    else:
        return jsonify(error=f"Invalid job type '{job_type}' specified."), 400

    if success:
        return jsonify(result)
    else:
        return jsonify(result), 500


@app.route("/api/jobs/start", methods=["POST"])
@login_required
def start_job():
    """API endpoint to start a new job by calling the job manager."""
    data = request.get_json()
    job_type = data.get("job_type")  # to allow different types of jobs to be managed

    if job_type == "DOWNLOAD":
        asins = data.get("asins")
        if not asins or not isinstance(asins, list):
            return jsonify(error="List of ASINs is required for DOWNLOAD job."), 400
        success, result = start_new_job(job_type, asins=asins)

    elif job_type == "SYNC":
        # Get the job_params dictionary from the JSON payload sent by the frontend.
        job_params = data.get("job_params", {})
        # For a SYNC job, 'asins' is always None.
        success, result = start_new_job(job_type="SYNC", asins=None, job_params=job_params)

    elif job_type == "VERIFY":
        # Verification jobs don't need params or ASINs
        success, result = start_new_job(job_type="VERIFY", asins=None)

    elif job_type == "IMPORT":
        # Scan-in-place import: no params or ASINs — the worker discovers files under /data.
        success, result = start_new_job(job_type="IMPORT", asins=None)

    else:
        return jsonify(error="Invalid or missing 'job_type'."), 400

    if success:
        return jsonify(result)
    else:
        status_code = 409 if "already in progress" in result.get("error", "") else 500
        return jsonify(result), status_code


@app.route("/api/jobs/cancel", methods=["POST"])
@login_required
def cancel_job():
    """API endpoint to cancel the active job by calling the job manager."""
    success, result = cancel_active_job()

    if success:
        return jsonify(result)
    else:
        return jsonify(result), 404


# Finished job states — the only rows /api/jobs/clear may delete. RUNNING and
# QUEUED (an active or pending job) are deliberately absent, so the active job
# can never be removed out from under the worker.
_CLEARABLE_JOB_STATUSES = ("COMPLETED", "FAILED", "CANCELLED")

# Upper bound for the older_than `days` param. timedelta(days=...) raises
# OverflowError for astronomically large values (and that computation sits
# outside the DB try block), so cap the input; "older than 100 years" already
# clears every job anyway.
_MAX_CLEAR_DAYS = 36500


@app.route("/api/jobs/clear", methods=["POST"])
@login_required
def clear_jobs():
    """
    Delete finished jobs (and their job_items) from history (FR10 backend).

    Login + Origin gated. Only COMPLETED/FAILED/CANCELLED jobs are eligible — a
    RUNNING or QUEUED job is never touched. Modes:
      {"mode": "all"}                      -> every finished job (default)
      {"mode": "older_than", "days": N}    -> finished jobs whose end_time
                                              (or start_time, if unset) is older
                                              than N days.
    """
    data = request.get_json(silent=True) or {}
    mode = data.get("mode", "all")
    status_placeholders = ",".join("?" for _ in _CLEARABLE_JOB_STATUSES)

    if mode == "all":
        where = f"status IN ({status_placeholders})"
        params = list(_CLEARABLE_JOB_STATUSES)
    elif mode == "older_than":
        days = data.get("days")
        if not isinstance(days, int) or isinstance(days, bool) or days <= 0 or days > _MAX_CLEAR_DAYS:
            return jsonify(
                error=f"'days' must be a positive integer no greater than {_MAX_CLEAR_DAYS} for older_than mode."
            ), 400
        cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
        # COALESCE so a finished job that somehow lacks an end_time still gets
        # aged out by its start_time rather than being immortal.
        where = f"status IN ({status_placeholders}) AND COALESCE(end_time, start_time) < ?"
        params = [*_CLEARABLE_JOB_STATUSES, cutoff]
    else:
        return jsonify(error="Invalid mode. Use 'all' or 'older_than'."), 400

    con = get_db_connection()
    try:
        cur = con.cursor()
        job_ids = [row["job_id"] for row in cur.execute(f"SELECT job_id FROM jobs WHERE {where}", params).fetchall()]
        if not job_ids:
            return jsonify(success=True, deleted_jobs=0, deleted_items=0)

        id_placeholders = ",".join("?" for _ in job_ids)
        # Remove child job_items first so no orphan rows are left behind.
        deleted_items = cur.execute(f"DELETE FROM job_items WHERE job_id IN ({id_placeholders})", job_ids).rowcount
        # Re-assert the finished-status predicate on the DELETE itself so the
        # "never delete RUNNING/QUEUED" guarantee is enforced by the query rather
        # than resting solely on the finished-states-are-terminal invariant.
        deleted_jobs = cur.execute(
            f"DELETE FROM jobs WHERE job_id IN ({id_placeholders}) AND status IN ({status_placeholders})",
            [*job_ids, *_CLEARABLE_JOB_STATUSES],
        ).rowcount
        con.commit()
    except sqlite3.Error as e:
        con.rollback()
        log.error(f"Database error clearing jobs: {e}", exc_info=True)
        return jsonify(error="Failed to clear jobs."), 500
    finally:
        con.close()

    log.info(f"JOBS: Cleared {deleted_jobs} job(s) and {deleted_items} item(s) (mode={mode}).")
    return jsonify(success=True, deleted_jobs=deleted_jobs, deleted_items=deleted_items)


@app.route("/api/clear_image_cache", methods=["POST"])
@login_required
def clear_image_cache():
    """
    Deletes all cached cover art from the /config/covers directory.
    This forces a re-download of all images on the next library sync.
    """
    log.warning("Received request to clear the image cache.")
    try:
        # The COVERS_DIR constant is already imported from __init__.py
        if os.path.isdir(COVERS_DIR):
            # Use shutil.rmtree to recursively delete the entire directory
            shutil.rmtree(COVERS_DIR)
            log.info(f"Successfully removed image cache directory: {COVERS_DIR}")

        # Re-create the empty directory immediately so the app doesn't crash
        # if it tries to write a new cover before a sync is run.
        os.makedirs(COVERS_DIR, exist_ok=True)
        log.info(f"Re-created empty image cache directory: {COVERS_DIR}")

        return jsonify(success=True, message="Image cache has been cleared. Run a library sync to re-download covers.")

    except Exception as e:
        log.error(f"An error occurred while clearing the image cache: {e}", exc_info=True)
        return jsonify(error=f"An error occurred while clearing the cache: {e}"), 500


@app.route("/api/reset_authentication", methods=["POST"])
@login_required
def reset_authentication():
    """
    Deletes the setup flag and the audible auth directory to force a re-run
    of the setup process on the next container start.
    """
    log.warning("Received request to reset authentication.")
    try:
        # The .audible directory is critical data and lives in the DATABASE_DIR volume.
        audible_dir = os.path.join(DATABASE_DIR, ".audible")

        # Delete the setup complete flag file
        if os.path.exists(SETUP_FLAG_FILE):
            os.remove(SETUP_FLAG_FILE)
            log.info(f"Removed setup flag file: {SETUP_FLAG_FILE}")

        # Delete the .audible directory
        if os.path.isdir(audible_dir):
            shutil.rmtree(audible_dir)
            log.info(f"Removed audible auth directory: {audible_dir}")

        return jsonify(success=True, message="Authentication has been reset. The application will now shut down.")

    except Exception as e:
        log.error(f"An error occurred while resetting authentication: {e}", exc_info=True)
        return jsonify(error="An error occurred during reset. Please check the logs."), 500


@app.route("/internal/shutdown", methods=["POST"])
@login_required
def shutdown():
    """
    Terminates the application process so Docker's restart policy
    (`restart: unless-stopped`) brings the container back up, where start.sh
    re-evaluates Setup Mode vs Normal Mode (used by "Reset Audible Connection").

    We send the response first, then exit after a short delay. os._exit skips
    Python's cleanup handlers deliberately: atexit would wait on the task
    runner's worker threads, which could stall the restart indefinitely.
    """
    log.warning("Shutdown requested via /internal/shutdown. Exiting in 1 second; Docker will restart the container.")
    threading.Timer(1.0, os._exit, args=(0,)).start()
    return jsonify(success=True, message="Server is restarting...")


@app.route("/covers/<path:filename>")
@login_required
def serve_cover(filename):
    # Lazy-loaded <img> requests send the session cookie, so authenticated
    # pages keep working; an expired session just shows broken thumbnails
    # until re-login instead of leaking library contents.
    return send_from_directory(COVERS_DIR, filename)


@app.route("/get_page_data")
@login_required
def get_page_data():
    stats = get_db_stats()
    books = get_all_books()
    stats_lower = {k.lower(): v for k, v in stats.items()}
    return jsonify(stats=stats_lower, books=books)


@app.route("/api/downloadable_books")
@login_required
def api_get_downloadable_books():
    """API endpoint to get categorized lists of books for the download modal."""
    # This now returns a dictionary with categorized lists.
    categorized_books = get_books_for_download_modal()
    return jsonify(categorized_books)


@app.route("/api/conversion_rate")
@login_required
def api_get_conversion_rate():
    """
    Expose the estimator's effective conversion rate (seconds of processing per
    minute of audio) so the frontend can warn before a large bulk download.

    AudioBookup always re-encodes, so a big batch takes real time; the
    large-library warning (v0.20 Phase 6 / FR13) multiplies this learned rate by
    the selected books' total runtime to show a rough expectation. Read-only.
    """
    from audible_downloader.eta_estimator import get_average_rate

    return jsonify(sec_per_min=get_average_rate())


@app.route("/clear_log", methods=["POST"])
@login_required
def clear_log():
    if os.path.exists(LOG_FILE):
        open(LOG_FILE, "w").close()
    return redirect(url_for("index"))


@app.route("/api/fetch_full_summary/<string:asin>", methods=["POST"])
@login_required
def fetch_full_summary(asin):
    command = ["audible", "api", f"/1.0/catalog/products/{asin}?response_groups=product_desc,product_extended_attrs"]
    try:
        result = subprocess.run(command, capture_output=True, text=True, check=True, encoding="utf-8")
        data = json.loads(result.stdout)
        full_summary_html = data.get("product", {}).get("publisher_summary")
        if not full_summary_html:
            return jsonify(error="Full summary not found in API response."), 404
        cleaned_summary = re.sub("<[^<]+?>", "", full_summary_html).strip()
        con = get_db_connection()
        cur = con.cursor()
        cur.execute("UPDATE audiobooks SET summary = ?, is_summary_full = 1 WHERE asin = ?", (cleaned_summary, asin))
        con.commit()
        con.close()
        return jsonify(success=True, summary=cleaned_summary)
    except subprocess.CalledProcessError as e:
        log.error(f"Error calling audible-cli for full summary of {asin}: {e.stderr}")
        return jsonify(error="Failed to fetch details from Audible API."), 502
    except (json.JSONDecodeError, AttributeError):
        return jsonify(error="Invalid API response from Audible."), 502
    except sqlite3.Error as e:
        log.error(f"Database error updating full summary for {asin}: {e}", exc_info=True)
        return jsonify(error="Failed to update database."), 500


@app.route("/api/book/<string:asin>/update", methods=["POST"])
@login_required
def update_book_metadata(asin):
    """
    Persist user metadata overrides (custom title/author) for a book.

    A field is only touched when its key is present in the request body; an
    empty value clears that override (reverting to the Audible value). Values
    are trimmed and length-capped. After the DB write the on-disk filename is
    reconciled via rename_book_to_match_metadata(), which itself gates whether a
    rename actually happens (the opt-in Phase 5.5 behavior lives inside that fn).
    """

    def _clean(value):
        if not isinstance(value, str):
            return None
        value = value.strip()
        return value[:500] if value else None

    data = request.get_json(silent=True) or {}
    updates = {}
    if "custom_title" in data:
        updates["custom_title"] = _clean(data["custom_title"])
    if "custom_author" in data:
        updates["custom_author"] = _clean(data["custom_author"])
    # Duplicate resolution (v0.20 Phase 5): an explicit opt-in that clears the
    # `is_duplicate` flag once the user has chosen a disambiguating name (or
    # accepted the ASIN-suffixed one). Kept separate from the title/author
    # allow-list above so a normal edit never touches the flag.
    if data.get("resolve_duplicate") is True:
        updates["is_duplicate"] = 0

    if not updates:
        return jsonify(error="No editable fields provided."), 400

    con = get_db_connection()
    try:
        cur = con.cursor()
        if cur.execute("SELECT asin FROM audiobooks WHERE asin = ?", (asin,)).fetchone() is None:
            return jsonify(error="Book not found."), 404
        # Column names are a fixed allow-list above, so this f-string is safe.
        set_clause = ", ".join(f"{column} = ?" for column in updates)
        cur.execute(f"UPDATE audiobooks SET {set_clause} WHERE asin = ?", [*updates.values(), asin])
        con.commit()
        row = cur.execute("SELECT * FROM audiobooks WHERE asin = ?", (asin,)).fetchone()
    except sqlite3.Error as e:
        log.error(f"Database error updating metadata for {asin}: {e}", exc_info=True)
        return jsonify(error="Failed to update database."), 500
    finally:
        con.close()

    # When the user has opted in, propagate the edit to the on-disk filename.
    # Imported lazily to avoid a module-load import cycle. Best-effort: it
    # returns the new path or None and never raises.
    from audible_downloader.processing_logic import rename_book_to_match_metadata

    renamed_to = rename_book_to_match_metadata(asin)

    book = apply_metadata_overrides(dict(row))
    return jsonify(
        success=True,
        title=book["title"],
        author=book["author"],
        native_title=book["native_title"],
        native_author=book["native_author"],
        custom_title=book.get("custom_title"),
        custom_author=book.get("custom_author"),
        is_duplicate=book.get("is_duplicate") or 0,
        renamed_to=renamed_to,
    )


ALLOWED_COVER_EXTS = {".jpg", ".jpeg", ".png", ".webp", ".bmp"}
MAX_COVER_BYTES = 15 * 1024 * 1024


@app.route("/api/book/<string:asin>/cover", methods=["POST"])
@login_required
def upload_book_cover(asin):
    """
    Replace a book's cover with an uploaded image. The upload is normalized to
    JPEG for both the full cover and the 200x200 thumbnail (same layout the sync
    uses), and custom_cover is set so the sync won't re-fetch the Audible cover.
    """
    con = get_db_connection()
    try:
        book_exists = con.execute("SELECT asin FROM audiobooks WHERE asin = ?", (asin,)).fetchone() is not None
    finally:
        con.close()
    if not book_exists:
        return jsonify(error="Book not found."), 404

    upload = request.files.get("cover")
    if upload is None or not upload.filename:
        return jsonify(error="No cover file uploaded."), 400

    ext = os.path.splitext(upload.filename)[1].lower()
    if ext not in ALLOWED_COVER_EXTS:
        return jsonify(error="Unsupported image type."), 400

    # Read with a hard size cap (reading one extra byte detects an oversize file).
    data = upload.read(MAX_COVER_BYTES + 1)
    if len(data) > MAX_COVER_BYTES:
        return jsonify(error="Cover image is too large (max 15 MB)."), 413
    if not data:
        return jsonify(error="Uploaded cover is empty."), 400

    os.makedirs(COVERS_DIR, exist_ok=True)
    original_path = os.path.join(COVERS_DIR, f"{asin}_original.jpg")
    thumb_path = os.path.join(COVERS_DIR, f"{asin}_thumb.jpg")

    with tempfile.NamedTemporaryFile(suffix=ext, delete=False) as tmp:
        tmp.write(data)
        tmp_path = tmp.name
    # Normalize into temporary .jpg outputs first and only swap BOTH into place
    # once both passes succeed. Writing straight to the final paths meant a
    # failure on the second (thumbnail) pass left the full cover already replaced
    # while the thumbnail and the custom_cover flag lagged behind — an
    # inconsistent state where the grid and detail views disagree.
    staged = []  # (temp_output, final_path) pairs, swapped in only on full success
    try:
        # ffmpeg both validates the image (non-images fail) and normalizes to JPEG.
        # Security: ffmpeg detects format by content, not our extension check, so
        # force the single-image demuxer (-f image2) and restrict protocols to
        # local files. Without this, a crafted upload could be parsed as a
        # concat/hls playlist and made to read local files or reach the network
        # (SSRF). -nostdin avoids any prompt hang.
        for final_path, vf in ((original_path, None), (thumb_path, "scale=200:200")):
            fd, out_tmp = tempfile.mkstemp(suffix=".jpg", dir=COVERS_DIR)
            os.close(fd)
            staged.append((out_tmp, final_path))
            command = ["ffmpeg", "-nostdin", "-y", "-protocol_whitelist", "file", "-f", "image2", "-i", tmp_path]
            if vf:
                command += ["-vf", vf]
            command.append(out_tmp)
            result = subprocess.run(command, capture_output=True, text=True)
            if result.returncode != 0:
                log.warning(f"Cover conversion failed for {asin}: {result.stderr}")
                return jsonify(error="Could not process the uploaded image."), 400
        # Both passes succeeded: swap the new covers in atomically.
        for out_tmp, final_path in staged:
            os.replace(out_tmp, final_path)
    finally:
        # Remove the input temp and any staged output not swapped in (the
        # failure/early-return path); swapped-in temps are already gone.
        for leftover in [tmp_path, *(out_tmp for out_tmp, _ in staged)]:
            try:
                os.remove(leftover)
            except OSError:
                pass

    con = get_db_connection()
    try:
        con.execute("UPDATE audiobooks SET custom_cover = 1 WHERE asin = ?", (asin,))
        con.commit()
    finally:
        con.close()

    return jsonify(
        success=True,
        cover_url_original=f"/covers/{asin}_original.jpg",
        cover_url_thumb=f"/covers/{asin}_thumb.jpg",
    )


@app.route("/api/library/import/upload", methods=["POST"])
@login_required
def import_upload():
    """
    Stream an uploaded audiobook file into the managed library and adopt it
    (Phase 6 / FR2). The body is the raw file bytes; the original filename comes
    from the `filename` query arg (or an `X-Filename` header). Login + Origin
    gated like every other write. The upload is streamed straight to disk under
    /data with a per-chunk size guard (from the `import.max_upload_gb` setting),
    so an oversize upload is rejected without buffering it in memory.
    """
    filename = request.args.get("filename") or request.headers.get("X-Filename", "")
    ext = os.path.splitext(filename)[1].lower()
    if ext not in IMPORTABLE_EXTS:
        # Rejected before reading any bytes; drain so the client sees this 400.
        _drain_request_stream(request.stream)
        return jsonify(error="Unsupported file type. Only .m4b and .m4a are accepted."), 400

    max_gb = load_settings().get("import", {}).get("max_upload_gb", 2)
    try:
        max_bytes = int(float(max_gb) * 1024 * 1024 * 1024)
    except (TypeError, ValueError):
        max_bytes = 2 * 1024 * 1024 * 1024

    staging_dir = import_staging_dir()
    os.makedirs(staging_dir, exist_ok=True)
    staging_path = os.path.join(staging_dir, f"{uuid.uuid4().hex}{ext}")

    written = 0
    over_limit = False
    try:
        with open(staging_path, "wb") as out:
            while True:
                chunk = request.stream.read(1024 * 1024)
                if not chunk:
                    break
                written += len(chunk)
                if written > max_bytes:
                    over_limit = True
                    break
                out.write(chunk)

        if over_limit:
            # Discard the rest of the oversize body so the client receives the 413.
            _drain_request_stream(request.stream)
            _safe_remove(staging_path)
            return jsonify(error=f"Upload exceeds the {max_gb} GB limit."), 413
        if written == 0:
            _safe_remove(staging_path)
            return jsonify(error="Uploaded file is empty."), 400

        result = adopt_upload(staging_path, filename, load_settings())
        if result.get("reason") == "unreadable-media":
            # adopt_upload rejected renamed junk before placing it and left the
            # staging file for us to remove; surface a clear 400 to the client.
            _safe_remove(staging_path)
            return jsonify(error="The uploaded file could not be read as an audio file."), 400
    except Exception as e:
        log.error(f"IMPORT: upload adoption failed for '{filename}': {e}", exc_info=True)
        _safe_remove(staging_path)
        return jsonify(error="Failed to import the uploaded file."), 500

    return jsonify(
        success=True,
        action=result.get("action"),
        asin=result.get("key"),
        title=result.get("title"),
        author=result.get("author"),
        filepath=result.get("filepath"),
    )


def _safe_remove(path):
    """Best-effort delete of a staging file; never raises."""
    try:
        os.remove(path)
    except OSError:
        pass


# Cap on how much of a rejected upload body we'll read-and-discard. Draining lets
# the client see our error response instead of a mid-send connection reset, but we
# won't sit reading an unbounded body from a client that ignores the rejection.
_DRAIN_LIMIT_BYTES = 8 * 1024 * 1024


def _drain_request_stream(stream):
    """
    Best-effort: read and discard up to _DRAIN_LIMIT_BYTES of the request body so
    a client whose upload we rejected early still receives the JSON error rather
    than a broken pipe. Never raises; stops at the cap for an oversize/hostile body.
    """
    drained = 0
    try:
        while drained < _DRAIN_LIMIT_BYTES:
            chunk = stream.read(min(1024 * 1024, _DRAIN_LIMIT_BYTES - drained))
            if not chunk:
                break
            drained += len(chunk)
    except OSError:
        pass


@app.route("/api/logs/download")
@login_required
def download_log():
    if not os.path.exists(LOG_FILE):
        return jsonify(error="Log file not found."), 404

    # Serve the file as an attachment so the browser downloads it
    return send_file(
        LOG_FILE,
        as_attachment=True,
        download_name=f"audiobookup_{datetime.now().strftime('%Y%m%d_%H%M%S')}.log",
        mimetype="text/plain",
    )


# --- Authentication Routes ---


@app.route("/login", methods=["GET", "POST"])
def login():
    """Handles the user login process."""
    # If the user is already logged in, redirect them away from the login page.
    if "username" in session:
        return redirect(url_for("index"))

    error = None
    if request.method == "POST":
        username = request.form["username"]
        password = request.form["password"]

        if verify_credentials(username, password):
            # If credentials are valid, store username in the session
            session["username"] = username

            # --- SECURITY: Validate the 'next' parameter before redirecting ---
            # Get the requested redirect path from the URL query parameter.
            next_page = request.args.get("next")

            # Validate that the path is a local, relative path.
            # This prevents "Open Redirect" vulnerabilities where an attacker could
            # craft a link that logs a user in and then redirects them to a malicious site.
            # A safe path must start with '/' and not with '//' or any protocol.
            # Backslashes are rejected too, since browsers treat '/\' like '//'.
            if not next_page or not next_page.startswith("/") or next_page.startswith("//") or "\\" in next_page:
                next_page = url_for("index")
            return redirect(next_page)
        else:
            error = "Invalid credentials. Please try again."

    return render_template("login.html", error=error)


@app.route("/initial_setup", methods=["GET", "POST"])
@login_required
def initial_setup():
    """Handles the mandatory first-time password change."""
    settings = load_settings()
    # If setup is already complete, redirect away.
    if settings.get("initial_setup_complete", False):
        return redirect(url_for("index"))

    if request.method == "POST":
        new_password = request.form.get("new_password")
        confirm_password = request.form.get("confirm_password")

        # --- Validation ---
        if not new_password or not confirm_password:
            flash("Both new password fields are required.", "error")
            return redirect(url_for("initial_setup"))
        if len(new_password) < 8:
            flash("New password must be at least 8 characters long.", "error")
            return redirect(url_for("initial_setup"))
        if new_password != confirm_password:
            flash("New passwords do not match.", "error")
            return redirect(url_for("initial_setup"))

        # --- Update Settings ---
        settings["password_hash"] = generate_password_hash(new_password)
        settings["initial_setup_complete"] = True  # Flip the flag!

        # Use our existing, thread-safe settings saver
        if save_settings(settings):
            flash("Password updated successfully! Please continue with the setup.", "success")
            # Redirect to the main page; the @login_required decorator will now
            # correctly redirect to the Audible setup if needed.
            return redirect(url_for("index"))
        else:
            flash("An error occurred while saving the new password.", "error")

    return render_template("initial_setup.html")


@app.route("/logout")
def logout():
    """Clears the session to log the user out."""
    session.pop("username", None)
    flash("You have been successfully logged out.", "success")
    return redirect(url_for("login"))

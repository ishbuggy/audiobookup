import json
import math
import os
import re
import shutil
import sqlite3
import subprocess
from collections import deque
from datetime import datetime

from flask import (  # type: ignore
    Response,
    flash,
    jsonify,
    redirect,
    render_template,
    request,
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
from audible_downloader.db import get_all_books, get_books_for_download_modal, get_db_connection, get_db_stats

# Import the authentication health check module
from audible_downloader.health_check import get_audible_auth_status, perform_audible_auth_check

# Import from the job_manager module
from audible_downloader.job_manager import cancel_active_job, start_new_job

# Import from the logging module
from audible_downloader.logger import log

# Import the settings functions from the settings module
from audible_downloader.settings import deep_update, load_settings, save_settings


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
    return render_template("index.html", stats=stats, books=books, log_history=log_history, settings=settings)


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
    return render_template("settings.html", settings=current_settings)


@app.route("/history")
@login_required
def history():
    """Renders the dedicated job history page."""
    return render_template("history.html")


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
        # Signal the scheduler that settings might have changed.
        settings_changed_event.set()

        # --- Restored Logic: Handle logout on credential change ---
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
    book_dict = dict(book_from_db)
    if book_dict.get("is_summary_full") is None:
        book_dict["is_summary_full"] = 0
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
        while True:
            # Block until a message is available.
            msg = q.get()
            yield msg

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
    func = request.environ.get("werkzeug.server.shutdown")
    if func is None:
        raise RuntimeError("Not running with the Werkzeug Server")
    func()
    return "Server shutting down..."


@app.route("/covers/<path:filename>")
def serve_cover(filename):
    return send_from_directory(COVERS_DIR, filename)


def stream_script_output(script_path, script_name, args=None):
    if args is None:
        args = []
    command = [script_path] + args
    separator = "\n=================================================="
    log.info(separator)  # <-- Use logger
    yield f"data: {separator}\n\n"
    message = f"--- Starting script: {script_name} ---"
    log.info(message)  # <-- Use logger
    yield f"data: {message}\n\n"
    process = subprocess.Popen(
        command, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, bufsize=1, encoding="utf-8"
    )
    for line in iter(process.stdout.readline, ""):
        clean_line = line.strip()
        if clean_line.startswith("EVENT_BOOK_PROCESSING_START:"):
            asin = clean_line.split(":", 1)[1]
            yield f"event: book_processing_start\ndata: {asin}\n\n"
        elif clean_line.startswith("EVENT_BOOK_PROCESSING_END:"):
            json_data = clean_line.split(":", 1)[1]
            yield f"event: book_processing_end\ndata: {json_data}\n\n"
        elif clean_line.startswith("EVENT_BOOK_UPDATE:"):
            json_data = clean_line.split(":", 1)[1]
            yield f"event: book_update\ndata: {json_data}\n\n"
        else:
            log.info(clean_line)  # <-- Use logger
            yield f"data: {clean_line}\n\n"
    process.stdout.close()
    return_code = process.wait()
    if return_code == 0:
        final_message = "--- Script finished successfully. ---"
    else:
        final_message = f"--- SCRIPT FAILED with exit code {return_code}. ---"
    log.info(final_message)  # <-- Use logger
    yield f"event: end-of-stream\ndata: {final_message}\n\n"


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


@app.route("/run_action")
@login_required
def run_action():
    script_name = request.args.get("script")
    allowed_scripts = {"sync": "/config/sync.sh", "download": "/config/download.sh"}
    if script_name in allowed_scripts:
        script_path = allowed_scripts[script_name]
        args = []
        if script_name == "download":
            concurrency = request.args.get("concurrency", "1")
            if concurrency.isdigit() and int(concurrency) > 0:
                args.append(concurrency)
            else:
                args.append("1")
            selected_asins = request.args.getlist("asins")
            if selected_asins:
                args.extend(selected_asins)
        return Response(stream_script_output(script_path, script_name, args=args), mimetype="text/event-stream")
    return Response(f"data: ERROR: Unknown script '{script_name}'\n\n", mimetype="text/event-stream")


@app.route("/run_single_action")
@login_required
def run_single_action():
    asin = request.args.get("asin")
    if not asin:
        return Response("data: ERROR: No ASIN provided.\n\n", mimetype="text/event-stream")
    return Response(
        stream_script_output("/config/process_book.sh", f"Process Book {asin}", args=[asin]),
        mimetype="text/event-stream",
    )


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

            # The login_required decorator on 'index' will handle the redirect
            # to the correct setup step if needed.
            next_page = request.args.get("next")
            return redirect(next_page or url_for("index"))
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

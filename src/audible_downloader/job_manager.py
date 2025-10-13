# audible_downloader/job_manager.py

import json
import sqlite3
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from threading import Event, Lock, Thread

# Import necessary components from our other modules
from . import announcer, app  # Needed for the app_context
from .db import get_books_for_auto_job, get_db_connection
from .logger import log
from .processing_logic import BookProcessor
from .settings import load_settings
from .sync_logic import run_sync_logic

# --- Globals for Job Management ---
job_lock = Lock()
active_job = {"job_id": None, "thread": None, "stop_event": None}


# --- START: SYNC WORKER ---
def sync_worker(job_id, app_context, stop_event, job_params=None):
    """This function runs the Python-native sync logic in a background thread."""
    with app_context:
        final_status = "FAILED"
        try:
            # Set default parameters and extract the sync_mode.
            if job_params is None:
                job_params = {}
            sync_mode = job_params.get("sync_mode", "DEEP")  # Default to DEEP for safety

            with get_db_connection() as con:
                con.execute("UPDATE jobs SET status = 'RUNNING' WHERE job_id = ?", (job_id,))
                con.commit()

            success = False  # Default to failure
            # Pass the sync_mode to the sync logic function.
            sync_generator = run_sync_logic(job_id, sync_mode=sync_mode)

            while True:
                try:
                    line = next(sync_generator)
                    log.debug(f"WORKER(stdout - SYNC): {line}")
                    if line.startswith("EVENT_SYNC_UPDATE:"):
                        try:
                            json_data = line.split(":", 1)[1]
                            update_msg = json.loads(json_data)
                            announcer.announce(f"event: job_update\ndata: {json.dumps(update_msg)}\n\n")
                        except (json.JSONDecodeError, IndexError) as e:
                            log.warning(f"WORKER: Could not parse sync event line '{line}': {e}")
                except StopIteration as e:
                    # Generator is finished, capture its return value
                    success = e.value
                    break  # Exit the loop
                except Exception as e:
                    # Catch any other unexpected error during iteration
                    log.error(
                        f"WORKER: Unhandled exception during sync logic execution for job {job_id}: {e}", exc_info=True
                    )
                    success = False
                    break  # Exit the loop

            if success:
                final_status = "COMPLETED"
                log.info(f"WORKER: Sync logic for job {job_id} completed successfully.")

                # --- START: NEW FEATURE LOGIC ---
                # Check settings to see if we should trigger a chained download job.
                settings = load_settings()
                if settings.get("tasks", {}).get("is_auto_process_enabled") and settings.get("tasks", {}).get(
                    "process_new_on_sync"
                ):
                    log.info(
                        f"WORKER ({job_id}): Sync complete. Checking if a chained download job should be triggered."
                    )

                    # This helper function will run in a separate thread to avoid deadlocks.
                    def trigger_chained_job():
                        # Wait a few seconds for this sync job to fully release its lock.
                        time.sleep(5)
                        log.info("CHAINED_JOB: Attempting to start automatic download job post-sync.")
                        with app_context:
                            # Start a new job, passing `asins=None`. The job manager will now
                            # automatically find which books to download based on settings.
                            start_new_job("DOWNLOAD", asins=None)

                    # Start the detached thread.
                    Thread(target=trigger_chained_job).start()
                # --- END: NEW FEATURE LOGIC ---

            end_time_iso = datetime.utcnow().isoformat()
            with get_db_connection() as con:
                con.execute(
                    "UPDATE jobs SET status = ?, end_time = ? WHERE job_id = ?", (final_status, end_time_iso, job_id)
                )
                con.commit()
            log.info(f"WORKER: Marked job {job_id} (SYNC) as {final_status}.")

        except Exception as e:
            log.error(f"WORKER: Unhandled exception in sync job {job_id}: {e}", exc_info=True)
            end_time_iso = datetime.utcnow().isoformat()
            with get_db_connection() as con:
                con.execute("UPDATE jobs SET status = 'FAILED', end_time = ? WHERE job_id = ?", (end_time_iso, job_id))
                con.commit()
        finally:
            payload = {"job_id": job_id, "status": final_status, "job_type": "SYNC"}
            announcer.announce(f"event: job_finished\ndata: {json.dumps(payload)}\n\n")
            with job_lock:
                if active_job["job_id"] == job_id:
                    active_job["job_id"] = None
                    active_job["thread"] = None
                    active_job["stop_event"] = None
                    log.info(f"WORKER: Cleaned up active job tracker for job {job_id}.")


# --- END: SYNC WORKER ---


## The worker now announces events to the SSE stream, and responds to cancellation signal
def download_worker(job_id, app_context, stop_event):
    """
    This function runs in a background thread and processes the download job.
    It now uses a ThreadPoolExecutor to limit the number of books being prepared
    concurrently, and delegates all processing logic to the global TaskRunner via
    the BookProcessor class.
    """
    with app_context:
        was_cancelled = False
        final_status = "FAILED"
        try:
            with get_db_connection() as con:
                con.execute("UPDATE jobs SET status = 'RUNNING' WHERE job_id = ?", (job_id,))
                items_to_process = con.execute("SELECT asin FROM job_items WHERE job_id = ?", (job_id,)).fetchall()

            asins_to_process = [item["asin"] for item in items_to_process]

            # A dictionary to hold the BookProcessor instance for each ASIN
            processors = {asin: None for asin in asins_to_process}

            # --- "HEAD-START" CONCURRENCY V2: CORRECTLY DECOUPLED ---
            settings = load_settings()
            book_concurrency = settings.get("job", {}).get("download", {}).get("max_parallel_downloads", 1)

            def _prepare_and_process_book(asin):
                """
                This is the target for our download-limiting thread pool.
                Its only job is to create and start a BookProcessor.
                The BookProcessor itself will run asynchronously via the task_runner.
                """
                if stop_event.is_set():
                    return

                # Announce that we are starting to process this book
                update_msg = {"asin": asin, "status_text": "Queued for Preparation...", "progress": 2}
                announcer.announce(f"event: job_update\ndata: {json.dumps(update_msg)}\n\n")

                with get_db_connection() as con:
                    con.execute(
                        "UPDATE job_items SET status = 'PROCESSING' WHERE job_id = ? AND asin = ?", (job_id, asin)
                    )

                try:
                    # Create the processor and store it.
                    processor = BookProcessor(asin=asin, job_id=job_id)
                    processors[asin] = processor

                    # This is the key change: processor.run() is a blocking call that
                    # now happens on its own thread, managed by this executor.
                    # The TaskRunner inside it manages the CPU-heavy tasks.
                    processor.run()
                finally:
                    # ADDED: This block ensures the job_items status is always updated
                    # after the processor is finished, regardless of success or failure.
                    with get_db_connection() as con:
                        # Check the TRUE final status from the main audiobooks table.
                        book_status_row = con.execute(
                            "SELECT status FROM audiobooks WHERE asin = ?", (asin,)
                        ).fetchone()

                        # Determine the correct job_items status based on the true outcome.
                        final_item_status = (
                            "COMPLETED" if book_status_row and book_status_row["status"] == "DOWNLOADED" else "FAILED"
                        )

                        # Update the job_items table so the final job status check works correctly.
                        con.execute(
                            "UPDATE job_items SET status = ? WHERE job_id = ? AND asin = ?",
                            (final_item_status, job_id, asin),
                        )
                        log.info(f"WORKER ({asin}): Final job item status set to {final_item_status}.")

            with ThreadPoolExecutor(max_workers=book_concurrency) as executor:
                # We don't need to wait for the results here. We just need to
                # submit all books to this pool. The pool's size (`book_concurrency`)
                # will naturally limit how many downloads run at once.
                log.info(f"WORKER ({job_id}): Submitting all {len(asins_to_process)} books to the preparation pool.")
                for asin in asins_to_process:
                    executor.submit(_prepare_and_process_book, asin)

            # After the executor finishes, all `processor.run()` calls have returned,
            # meaning all books are either completed or have failed.
            log.info(f"WORKER ({job_id}): All book processing threads have completed.")

            # Now, we check the final status from the database.
            with get_db_connection() as con:
                final_item_statuses = con.execute("SELECT status FROM job_items WHERE job_id = ?", (job_id,)).fetchall()

            all_completed = all(row["status"] == "COMPLETED" for row in final_item_statuses)

            if stop_event.is_set():
                was_cancelled = True

            if was_cancelled:
                final_status = "CANCELLED"
            elif all_completed:
                final_status = "COMPLETED"
            else:
                final_status = "FAILED"

            end_time_iso = datetime.utcnow().isoformat()
            with get_db_connection() as con:
                if was_cancelled:
                    con.execute(
                        "UPDATE job_items SET status = 'CANCELLED' WHERE job_id = ? AND status = 'QUEUED'", (job_id,)
                    )
                con.execute(
                    "UPDATE jobs SET status = ?, end_time = ? WHERE job_id = ?", (final_status, end_time_iso, job_id)
                )
            log.info(f"WORKER: Marked job {job_id} as {final_status}.")

        except Exception as e:
            log.error(f"WORKER: Unhandled exception in job {job_id}: {e}", exc_info=True)
            end_time_iso = datetime.utcnow().isoformat()
            with get_db_connection() as con:
                con.execute("UPDATE jobs SET status = 'FAILED', end_time = ? WHERE job_id = ?", (end_time_iso, job_id))
        finally:
            # MODIFIED: Fetch the final status of all items to send to the UI.
            final_items_payload = []
            if job_id:  # Ensure we have a job_id to query
                with get_db_connection() as con:
                    item_rows = con.execute("SELECT asin, status FROM job_items WHERE job_id = ?", (job_id,)).fetchall()
                    final_items_payload = [dict(row) for row in item_rows]

            payload = {
                "job_id": job_id,
                "status": final_status,
                "job_type": "DOWNLOAD",
                "items": final_items_payload,  # ADDED: Include the final item statuses
            }
            announcer.announce(f"event: job_finished\ndata: {json.dumps(payload)}\n\n")
            with job_lock:
                if active_job["job_id"] == job_id:
                    active_job["job_id"] = None
                    active_job["thread"] = None
                    active_job["stop_event"] = None
                    log.info(f"WORKER: Cleaned up active job tracker for job {job_id}.")


# --- Public Manager Functions ---


# --- START: start_new_job ---
def start_new_job(job_type, asins=None, job_params=None):
    """Creates job records and starts the correct background worker thread based on job_type."""
    with job_lock:
        if active_job["job_id"] is not None:
            return False, {"error": f"A job (ID: {active_job['job_id']}) is already in progress."}

        # Validate job type
        if job_type not in ["DOWNLOAD", "SYNC"]:
            return False, {"error": f"Invalid job type specified: {job_type}"}

        con = get_db_connection()
        try:
            # Serialize the job_params dictionary to a JSON string for database storage.
            params_json = json.dumps(job_params) if job_params else None
            start_time_iso = datetime.utcnow().isoformat()
            cur = con.cursor()
            cur.execute(
                "INSERT INTO jobs (job_type, status, start_time, job_params) VALUES (?, ?, ?, ?)",
                (job_type, "QUEUED", start_time_iso, params_json),
            )
            job_id = cur.lastrowid

            stop_event = Event()
            worker_target = None

            if job_type == "DOWNLOAD":
                # If no ASINs are provided, this is an automatic job. Fetch the list
                # of downloadable books based on the user's settings.
                if not asins:
                    log.info("No ASINs provided for DOWNLOAD job; fetching based on auto-process settings.")
                    settings = load_settings()
                    # Use the new, dedicated function for automatic jobs
                    books_to_process = get_books_for_auto_job(settings)
                    asins = [book["asin"] for book in books_to_process]

                # --- START: LOGIC TO RESET RETRY COUNT ---
                # For any book included in a manually started job, reset its retry counter.
                # This gives the user control to re-include a failed book in automatic queues.
                if asins:
                    log.info(f"Resetting retry count for {len(asins)} book(s) before starting job {job_id}.")
                    placeholders = ",".join("?" for _ in asins)
                    # We use the cursor here because we need to commit this change
                    # with the rest of the job creation transaction.
                    cur.execute(f"UPDATE audiobooks SET retry_count = 0 WHERE asin IN ({placeholders})", asins)
                # --- END: RESET RETRY COUNT LOGIC ---

                # If, after checking, there are no books to process, exit gracefully.
                if not asins:
                    log.info("No books found to process for this DOWNLOAD job. Job will not be created.")
                    # We return success=True because this is an expected outcome, not an error.
                    # We must close the DB connection since we are returning early.
                    con.close()
                    return True, {"success": True, "message": "No books to process.", "job_id": None}

                # The rest of the logic remains the same.
                worker_target = download_worker
                items_to_insert = [(job_id, asin, "QUEUED") for asin in asins]
                cur.executemany("INSERT INTO job_items (job_id, asin, status) VALUES (?, ?, ?)", items_to_insert)

                # The download_worker takes (job_id, app_context, stop_event).
                worker_args = (job_id, app.app_context(), stop_event)

            elif job_type == "SYNC":
                worker_target = sync_worker
                worker_args = (job_id, app.app_context(), stop_event, job_params)
                # No items to insert for a sync job

            con.commit()

            worker_thread = Thread(target=worker_target, args=worker_args)

            active_job["job_id"] = job_id
            active_job["thread"] = worker_thread
            active_job["stop_event"] = stop_event

            # --- START: job_started Event Broadcast ---.
            items_list = []
            if job_type == "DOWNLOAD":
                # For a download job, we need to fetch the book details to send to the UI.
                # This logic is similar to the /api/jobs/active endpoint.
                item_rows = cur.execute(
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

            # Announce the new job to all connected clients.
            payload = {"job_id": job_id, "status": "QUEUED", "job_type": job_type, "items": items_list}
            announcer.announce(f"event: job_started\ndata: {json.dumps(payload)}\n\n")
            # --- END: job_started Event Broadcast ---

            worker_thread.start()

            log.info(f"Starting new {job_type} job (ID: {job_id}).")
            return True, {"success": True, "job_id": job_id}

        except sqlite3.Error as e:
            log.error(f"Database error starting job: {e}")
            con.rollback()
            return False, {"error": "Failed to create job in database."}
        finally:
            con.close()


# --- END: start_new_job ---


def cancel_active_job():
    """Sends the cancellation signal to the active job."""
    with job_lock:
        if active_job["job_id"] is None or active_job["stop_event"] is None:
            return False, {"error": "No active job to cancel."}

        log.info(f"API: Received cancel request for job {active_job['job_id']}")
        active_job["stop_event"].set()  # This sets the signal

        return True, {"success": True, "message": "Cancel signal sent."}

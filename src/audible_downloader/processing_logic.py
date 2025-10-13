# audible_downloader/processing_logic.py

# --- Attribution ---
# The logic for determining the final sanitized filename is adapted from
# the work of Jan van Brügge in the original audible-convert.sh script.
# Original Source: https://github.com/jvanbruegge/nix-config/blob/master/scripts/audible-convert.sh
# License: MIT (included in the project's LICENSE.txt file)
# --- End Attribution ---

import os
import re
import tempfile
import time
from threading import Event, Lock

# Import the task-oriented functions and the global announcer
from .chunked_conversion_logic import (
    _yield_progress,
    encode_chapter_chunk,
    merge_book_chunks,
    prepare_book_assets,
)
from .db import get_db_connection
from .eta_estimator import record_conversion_time
from .logger import log
from .settings import load_settings
from . import TEMP_DIR

# Import the task runner and task objects
from .task_runner import Task, TaskPriority, task_runner


def _sanitize_filename(name):
    """Sanitizes a string to be used as a valid filename."""
    name = re.sub(r'[\\/:\*\?"<>|]', "_", name)
    name = name.strip(" .")
    name = re.sub(r"\s+", " ", name).strip()
    return name


class BookProcessor:
    """
    Manages the state and task submission for a single book's conversion process.
    This acts as the "General Contractor" for one book.
    """

    def __init__(self, asin, job_id, download_complete_event=None):
        self.asin = asin
        self.job_id = job_id
        self.download_complete_event = download_complete_event
        self.temp_dir = None
        self.final_output_path = None
        self.context = {}
        self.total_chunks = 0
        self.completed_chunks = 0
        self.encoded_chunk_paths = []
        self._lock = Lock()
        self._completion_event = Event()

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
                # Set a generous timeout (e.g., 2 hours) to prevent it from waiting forever.
                completed = self._completion_event.wait(timeout=7200)
                if not completed:
                    raise RuntimeError("Processing timed out.")
        except Exception as e:
            log.error(f"PROCESSOR ({self.asin}): A critical error occurred in the processor run: {e}", exc_info=True)
            self._update_db_on_failure(f"A critical error occurred: {e}")
        finally:
            log.info(f"PROCESSOR ({self.asin}): Finished run method.")

    def _prepare_and_spawn_encode_tasks(self):
        """The actual function for the PREPARE_BOOK task."""
        log.info(f"TASK-PREPARE ({self.asin}): Starting.")
        # --- 1. Fetch book details and determine final path ---
        try:
            settings = load_settings()
            template = settings.get("naming", {}).get("template", "{author}/{title}/{author} - {title}")
            with get_db_connection() as con:
                book_details = con.execute(
                    "SELECT author, title FROM audiobooks WHERE asin = ?", (self.asin,)
                ).fetchone()
            if not book_details:
                raise ValueError(f"Could not find ASIN {self.asin} in the database.")

            safe_author = _sanitize_filename(book_details["author"])
            safe_title = _sanitize_filename(book_details["title"])
            final_relative_path = template.replace("{author}", safe_author).replace("{title}", safe_title)
            self.final_output_path = os.path.join("/data", f"{final_relative_path}.m4b")
            os.makedirs(os.path.dirname(self.final_output_path), exist_ok=True)
        except Exception as e:
            log.error(f"TASK-PREPARE ({self.asin}): Failed to get details or create path: {e}")
            self._update_db_on_failure("Failed to prepare file path.")
            self._completion_event.set()
            return

        # --- 2. Call the asset preparation logic ---
        self.context = prepare_book_assets(self.asin, self.job_id, self.temp_dir)

        # Signal that the download/prepare phase is complete.
        # This will unblock the main worker in job_manager.py, allowing it
        # to start the next book's download.
        if self.download_complete_event:
            self.download_complete_event.set()

        if not self.context:
            self._update_db_on_failure("Failed during asset download/preparation.")
            self._completion_event.set()
            return

        # --- 3. Spawn all the ENCODE_CHAPTER tasks ---
        chapters = self.context.get("chapters", [])
        self.total_chunks = len(chapters)
        if self.total_chunks == 0:
            log.warning(f"TASK-PREPARE ({self.asin}): Book has no chapter information. Cannot process.")
            self._update_db_on_failure("Book has no chapter information.")
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

    def _encode_and_track_chunk(self, chunk_info):
        """The actual function for the ENCODE_CHAPTER task."""
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
                self._update_db_on_failure("A chapter chunk failed to encode.")
                self._completion_event.set()  # Signal completion to unblock the main thread
                return  # Stop processing further

            # If this was the last chunk to be processed, spawn the final MERGE task.
            if self.completed_chunks == self.total_chunks:
                log.info(f"PROCESSOR ({self.asin}): All chunks encoded. Submitting final merge task.")
                merge_task = Task(
                    priority=TaskPriority.MERGE_BOOK,
                    job_id=self.job_id,
                    func=self._merge_and_finalize,
                )
                task_runner.submit_task(merge_task)

    def _merge_and_finalize(self):
        """The actual function for the MERGE_BOOK task."""
        log.info(f"TASK-MERGE ({self.asin}): Starting.")
        conversion_start_time = time.time()

        success = merge_book_chunks(
            self.asin, self.job_id, self.temp_dir, self.final_output_path, self.context, self.encoded_chunk_paths
        )

        if success:
            conversion_duration_sec = time.time() - conversion_start_time
            with get_db_connection() as con:
                runtime_row = con.execute("SELECT runtime_min FROM audiobooks WHERE asin = ?", (self.asin,)).fetchone()
                if runtime_row:
                    record_conversion_time(runtime_row["runtime_min"], conversion_duration_sec)

            # On Success, update the database
            with get_db_connection() as con:
                con.execute(
                    "UPDATE audiobooks SET status = 'DOWNLOADED', filepath = ?, "
                    "error_message = '', retry_count = 0 WHERE asin = ?",
                    (self.final_output_path, self.asin),
                )
            _yield_progress(self.asin, "Complete!", 100, self.job_id)
        else:
            self._update_db_on_failure("Final merge of chapter chunks failed.")

        # This is the final step, so we signal the main `run` method to unblock.
        self._completion_event.set()
        log.info(f"TASK-MERGE ({self.asin}): Finalization complete.")

    def _update_db_on_failure(self, error_message):
        """Centralized method to update the database when any step fails."""
        log.error(f"PROCESSOR ({self.asin}):   -> ERROR: {error_message}")
        with get_db_connection() as con:
            con.execute(
                "UPDATE audiobooks SET status = 'ERROR', error_message = ? WHERE asin = ?", (error_message, self.asin)
            )
        _yield_progress(self.asin, "Failed!", 100, self.job_id)


def run_book_processing_logic(asin, job_id, download_complete_event=None):
    """
    Main entry point called by the download_worker.
    Creates a BookProcessor instance and runs it.
    """
    # Pass the event down to the BookProcessor instance.
    processor = BookProcessor(asin=asin, job_id=job_id, download_complete_event=download_complete_event)
    processor.run()

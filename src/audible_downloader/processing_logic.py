# audible_downloader/processing_logic.py

# --- Attribution ---
# The logic for determining the final sanitized filename is adapted from
# the work of Jan van Brügge in the original audible-convert.sh script.
# Original Source: https://github.com/jvanbruegge/nix-config/blob/master/scripts/audible-convert.sh
# License: MIT (included in the project's LICENSE.txt file)
# --- End Attribution ---

import os
import re
import shutil
import subprocess
import tempfile
import time
from threading import Event, Lock

from . import TEMP_DIR

# Import the task-oriented functions and the global announcer
from .chunked_conversion_logic import (
    _yield_progress,
    encode_chapter_chunk,
    merge_book_chunks,
    prepare_book_assets,
)
from .db import get_db_connection
from .eta_estimator import estimate_conversion_time, record_conversion_time
from .logger import log
from .process_registry import process_registry
from .settings import load_settings

# Import the task runner and task objects
from .task_runner import Task, TaskPriority, task_runner

# Output paths claimed by in-flight books, guarded by a lock. The on-disk/DB
# collision check only sees files that already exist; in a bulk job two
# different books with the same author+title both run PREPARE before either
# has written its file, so without this the loser's merge would silently
# overwrite the winner. Each book reserves its chosen path here for the
# duration of its run and releases it when finished.
_reserved_output_paths: set[str] = set()
_reservation_lock = Lock()

# A finished .m4b is far larger than this floor; it only catches an absent or
# empty/stub output — the "ghost book" that reported success but isn't on disk.
_MIN_OUTPUT_BYTES = 64 * 1024


def _probe_duration_seconds(filepath):
    """Return the media duration of `filepath` in seconds via ffprobe, or None
    if it can't be determined (missing or unreadable/corrupt file)."""
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
    try:
        result = subprocess.run(cmd, capture_output=True, text=True)
    except OSError:
        return None
    if result.returncode != 0:
        return None
    try:
        return float(result.stdout.strip())
    except ValueError:
        return None


def _sanitize_filename(name):
    """Sanitizes a string to be used as a valid filename."""
    name = re.sub(r'[\\/:\*\?"<>|]', "_", name)
    name = name.strip(" .")
    name = re.sub(r"\s+", " ", name).strip()
    return name


def _strip_subtitle(title):
    """
    Drop a trailing "Main Title: Subtitle" subtitle for cleaner filenames, e.g.
    "999: The Extraordinary Young Women..." -> "999". Splits on the first
    colon-space, so ratios/times like "12:00" are left intact. Returns the
    original title unchanged when there's no subtitle or when stripping would
    leave nothing. Must run BEFORE _sanitize_filename, which rewrites ':' to '_'.
    """
    if not title:
        return title
    main = title.split(": ", 1)[0].strip()
    return main or title


class BookProcessor:
    """
    Manages the state and task submission for a single book's conversion process.
    This acts as the "General Contractor" for one book.
    """

    def __init__(self, asin, job_id, download_complete_event=None, stop_event=None):
        self.asin = asin
        self.job_id = job_id
        self.download_complete_event = download_complete_event
        # The job's cancellation signal. The process_registry kills subprocesses
        # that are already running, but tasks still sitting in the queue would
        # otherwise start fresh work after a cancel — each task checks this
        # event before doing anything.
        self.stop_event = stop_event
        self.temp_dir = None
        self.final_output_path = None
        # Set True when a same-author+title collision forced an ASIN suffix onto
        # our filename; persisted to the DB on success so the UI can flag it.
        self.is_duplicate = False
        self.context = {}
        self.total_chunks = 0
        self.completed_chunks = 0
        self.encoded_chunk_paths = []
        self._lock = Lock()
        self._completion_event = Event()

    def _cancelled(self):
        """
        Returns True (and unblocks `run`) if the job has been cancelled.
        Task functions call this on entry so queued tasks become no-ops after a
        cancel instead of working against a soon-deleted temp dir.
        """
        if self.stop_event is not None and self.stop_event.is_set():
            log.info(f"PROCESSOR ({self.asin}): Job cancelled. Skipping remaining work.")
            self._completion_event.set()
            return True
        return False

    def _probe_file_asin(self, filepath):
        """
        Reads the embedded ASIN tag from an audio file using the same ffprobe
        invocation the deep filesystem scan uses. Returns the ASIN string, or
        None if the tag is absent or the file can't be probed.
        """
        ffprobe_cmd = [
            "ffprobe",
            "-v",
            "quiet",
            "-show_entries",
            "format_tags=asin",
            "-of",
            "default=noprint_wrappers=1:nokey=1",
            filepath,
        ]
        try:
            process = subprocess.Popen(ffprobe_cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
            process_registry.register(self.job_id, process)
            try:
                stdout, stderr = process.communicate()
            finally:
                process_registry.unregister(self.job_id, process)

            if process.returncode != 0:
                # Exit code -15 (SIGTERM) means the job was cancelled — not a real failure.
                if process.returncode != -15:
                    log.warning(f"TASK-PREPARE ({self.asin}): ffprobe failed for '{filepath}': {stderr}")
                return None

            return stdout.strip() or None
        except OSError as e:
            log.warning(f"TASK-PREPARE ({self.asin}): Could not probe '{filepath}': {e}")
            return None

    def _reserve_output_path(self, base_output_path, safe_asin):
        """
        Choose this book's final output path and reserve it in-process so two
        books with the same author+title can't both claim it.

        Collision cases, in order:
          1. Another in-flight book has already reserved this path -> rename ours.
          2. A file already exists at the path -> keep it only if it verifiably
             belongs to this same book (see _existing_file_is_foreign).
        On collision we inject the ASIN ("Title.m4b" -> "Title_B00XYZ.m4b"),
        which is unique per book, and mark this book as a duplicate.
        """
        with _reservation_lock:
            collision = False
            if base_output_path in _reserved_output_paths:
                log.info(
                    f"TASK-PREPARE ({self.asin}): Target path is already claimed by another "
                    f"in-flight book. Appending unique ID."
                )
                collision = True
            elif os.path.exists(base_output_path):
                collision = self._existing_file_is_foreign(base_output_path)

            if collision:
                root, ext = os.path.splitext(base_output_path)
                final_path = f"{root}_{safe_asin}{ext}"
            else:
                final_path = base_output_path

            _reserved_output_paths.add(final_path)

        self.is_duplicate = collision
        return final_path

    def _existing_file_is_foreign(self, filepath):
        """
        A file already exists at `filepath`. Return True if it belongs to a
        different book (so we must not overwrite it), False if it's safe to
        overwrite as this book's own prior download.

          1. Tracked in the DB under a different ASIN -> foreign.
          2. Tracked under our own ASIN -> ours, overwrite.
          3. Untracked -> probe the embedded ASIN tag; only a matching tag
             proves it's an old copy of this book.
        """
        with get_db_connection() as con:
            existing_entry = con.execute("SELECT asin FROM audiobooks WHERE filepath = ?", (filepath,)).fetchone()

        if existing_entry:
            if existing_entry["asin"] != self.asin:
                log.info(
                    f"TASK-PREPARE ({self.asin}): Filename collision with tracked ASIN "
                    f"{existing_entry['asin']}. Appending unique ID."
                )
                return True
            log.info(f"TASK-PREPARE ({self.asin}): File exists and belongs to this ASIN. Overwriting.")
            return False

        embedded_asin = self._probe_file_asin(filepath)
        if embedded_asin == self.asin:
            log.info(f"TASK-PREPARE ({self.asin}): Untracked file has this book's embedded ASIN tag. Overwriting.")
            return False
        log.info(
            f"TASK-PREPARE ({self.asin}): Untracked file with foreign or missing ASIN tag "
            f"({embedded_asin or 'none'}) occupies the target path. Appending unique ID."
        )
        return True

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
                # The timeout only exists to prevent waiting forever: at least
                # 2 hours, scaled up (4x the historical ETA) so very long books
                # on slow hardware don't get killed mid-conversion.
                timeout = 7200
                with get_db_connection() as con:
                    runtime_row = con.execute(
                        "SELECT runtime_min FROM audiobooks WHERE asin = ?", (self.asin,)
                    ).fetchone()
                if runtime_row:
                    timeout = max(timeout, 4 * estimate_conversion_time(runtime_row["runtime_min"]))

                completed = self._completion_event.wait(timeout=timeout)
                if not completed:
                    raise RuntimeError("Processing timed out.")
        except Exception as e:
            log.error(f"PROCESSOR ({self.asin}): A critical error occurred in the processor run: {e}", exc_info=True)
            self._update_db_on_failure(f"A critical error occurred: {e}")
        finally:
            # Release our claimed output path so the name is available again
            # (e.g. for a later re-download of this same book).
            if self.final_output_path:
                with _reservation_lock:
                    _reserved_output_paths.discard(self.final_output_path)
            log.info(f"PROCESSOR ({self.asin}): Finished run method.")

    def _prepare_and_spawn_encode_tasks(self):
        """The actual function for the PREPARE_BOOK task."""
        if self._cancelled():
            # Unblock the worker's download slot too, or the head-start
            # pipeline in job_manager would keep waiting on it.
            if self.download_complete_event:
                self.download_complete_event.set()
            return
        log.info(f"TASK-PREPARE ({self.asin}): Starting.")
        # --- 1. Fetch book details and determine final path ---
        try:
            settings = load_settings()
            # Default template now supports more options, though we default to the standard one.
            template = settings.get("naming", {}).get("template", "{author}/{title}/{author} - {title}")

            with get_db_connection() as con:
                # Fetch additional metadata columns for the expanded naming template
                book_details = con.execute(
                    "SELECT author, title, narrator, publisher FROM audiobooks WHERE asin = ?", (self.asin,)
                ).fetchone()

            if not book_details:
                raise ValueError(f"Could not find ASIN {self.asin} in the database.")

            # Optionally trim a long subtitle from the title used in filenames
            # (opt-in; embedded metadata keeps the full title). Runs before
            # sanitization because that step rewrites the ':' separator.
            raw_title = book_details["title"] or "Unknown Title"
            if settings.get("naming", {}).get("truncate_subtitle", False):
                raw_title = _strip_subtitle(raw_title)

            # Sanitize all potential filename components
            safe_author = _sanitize_filename(book_details["author"] or "Unknown Author")
            safe_title = _sanitize_filename(raw_title)
            safe_narrator = _sanitize_filename(book_details["narrator"] or "Unknown Narrator")
            safe_publisher = _sanitize_filename(book_details["publisher"] or "Unknown Publisher")
            safe_asin = _sanitize_filename(self.asin)

            # Apply expanded template replacements
            final_relative_path = (
                template.replace("{author}", safe_author)
                .replace("{title}", safe_title)
                .replace("{narrator}", safe_narrator)
                .replace("{publisher}", safe_publisher)
                .replace("{asin}", safe_asin)
            )

            # Collision Detection Logic ("The Dracula Problem"). Reserve a
            # unique output path, guarding against both files already on disk
            # and other in-flight books racing for the same name.
            base_output_path = os.path.join("/data", f"{final_relative_path}.m4b")
            self.final_output_path = self._reserve_output_path(base_output_path, safe_asin)

            os.makedirs(os.path.dirname(self.final_output_path), exist_ok=True)
        except Exception as e:
            log.error(f"TASK-PREPARE ({self.asin}): Failed to get details or create path: {e}")
            self._update_db_on_failure("Failed to prepare file path.")
            self._completion_event.set()
            return

        # --- 2. Call the asset preparation logic ---
        self.context, prepare_error = prepare_book_assets(self.asin, self.job_id, self.temp_dir)

        # Signal that the download/prepare phase is complete.
        # This will unblock the main worker in job_manager.py, allowing it
        # to start the next book's download.
        if self.download_complete_event:
            self.download_complete_event.set()

        if not self.context:
            # prepare_error carries the real underlying cause (e.g. audible-cli
            # reporting a title is no longer available) instead of a generic
            # message; it is None only on cancellation.
            self._update_db_on_failure(prepare_error or "Failed during asset download/preparation.")
            self._completion_event.set()
            return

        # --- 3. Spawn all the ENCODE_CHAPTER tasks ---
        chapters = self.context.get("chapters", [])
        self.total_chunks = len(chapters)

        _yield_progress(self.asin, f"Preparing to process {self.total_chunks} chunk(s)", 30, self.job_id)

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
        if self._cancelled():
            return
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

    def _verify_output_file(self):
        """
        Validate the finished file before we claim success, so a book is never
        marked DOWNLOADED while its file is missing, empty, or truncated (the
        "ghost book" and silent-truncation cases). Returns (ok, reason).
        """
        path = self.final_output_path
        if not path or not os.path.exists(path):
            return False, "Conversion reported success but no output file was found on disk."

        size = os.path.getsize(path)
        if size < _MIN_OUTPUT_BYTES:
            return False, f"Output file is implausibly small ({size} bytes); the conversion likely failed."

        with get_db_connection() as con:
            row = con.execute("SELECT runtime_min FROM audiobooks WHERE asin = ?", (self.asin,)).fetchone()
        expected_min = row["runtime_min"] if row else None
        if expected_min and expected_min > 0:
            actual_sec = _probe_duration_seconds(path)
            if actual_sec is None:
                return False, "Output file could not be read back (corrupt or unreadable)."
            expected_sec = expected_min * 60
            # Mirror the library Verify job's tolerance: only flag a file that
            # is significantly SHORTER than expected (under 95% and >10 minutes).
            if actual_sec < expected_sec * 0.95 and (expected_sec - actual_sec) > 600:
                return False, f"Output file is truncated (expected ~{expected_min}m, got {int(actual_sec / 60)}m)."

        return True, None

    def _place_supplementary_pdf(self):
        """
        Copy the companion PDF (if the download produced one) next to the
        finished audiobook, sharing its base name. Best-effort: a failure is
        logged, never fatal, and titles without a PDF are a silent no-op.
        """
        pdf_file = (self.context or {}).get("pdf_file")
        if not pdf_file or not os.path.exists(pdf_file):
            return
        pdf_target = f"{os.path.splitext(self.final_output_path)[0]}.pdf"
        try:
            shutil.copy2(pdf_file, pdf_target)
            log.info(f"PROCESSOR ({self.asin}): Saved companion PDF to {pdf_target}")
        except OSError as e:
            log.warning(f"PROCESSOR ({self.asin}): Could not save companion PDF: {e}")

    def _merge_and_finalize(self):
        """The actual function for the MERGE_BOOK task."""
        if self._cancelled():
            return
        log.info(f"TASK-MERGE ({self.asin}): Starting.")
        conversion_start_time = time.time()

        success = merge_book_chunks(
            self.asin, self.job_id, self.temp_dir, self.final_output_path, self.context, self.encoded_chunk_paths
        )

        if not success:
            self._update_db_on_failure("Final merge of chapter chunks failed.")
        else:
            # Never trust the merge's exit code alone: confirm the file is
            # actually on disk and complete before declaring the book DOWNLOADED.
            output_ok, reason = self._verify_output_file()
            if not output_ok:
                log.error(f"PROCESSOR ({self.asin}): Output verification failed: {reason}")
                self._update_db_on_failure(reason)
            else:
                conversion_duration_sec = time.time() - conversion_start_time
                with get_db_connection() as con:
                    runtime_row = con.execute(
                        "SELECT runtime_min FROM audiobooks WHERE asin = ?", (self.asin,)
                    ).fetchone()
                    if runtime_row:
                        record_conversion_time(runtime_row["runtime_min"], conversion_duration_sec)

                # On Success, update the database. is_duplicate records whether a
                # same-author+title collision forced an ASIN suffix onto our name;
                # it is written explicitly (0 when clean) so a later re-download
                # that resolves without a collision clears a stale flag.
                with get_db_connection() as con:
                    con.execute(
                        "UPDATE audiobooks SET status = 'DOWNLOADED', filepath = ?, "
                        "error_message = '', retry_count = 0, is_duplicate = ? WHERE asin = ?",
                        (self.final_output_path, int(self.is_duplicate), self.asin),
                    )
                # Place any companion PDF before the temp dir is torn down.
                self._place_supplementary_pdf()
                _yield_progress(self.asin, "Complete!", 100, self.job_id)

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


def run_book_processing_logic(asin, job_id, download_complete_event=None, stop_event=None):
    """
    Main entry point called by the download_worker.
    Creates a BookProcessor instance and runs it.
    """
    # Pass the events down to the BookProcessor instance.
    processor = BookProcessor(
        asin=asin, job_id=job_id, download_complete_event=download_complete_event, stop_event=stop_event
    )
    processor.run()

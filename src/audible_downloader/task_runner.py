# audible_downloader/task_runner.py

import time
from concurrent.futures import ThreadPoolExecutor
from enum import IntEnum
from queue import PriorityQueue
from threading import Event, Thread

from .logger import log
from .settings import load_settings


# Use an Enum for clear, readable priority levels, as planned.
# Lower numbers are higher priority.
class TaskPriority(IntEnum):
    ENCODE_CHAPTER = 1
    PREPARE_BOOK = 2
    MERGE_BOOK = 3


# A simple dataclass-like structure to hold task information.
# The __lt__ method is essential for the PriorityQueue to compare tasks.
class Task:
    def __init__(self, priority: TaskPriority, job_id: int, func, *args, **kwargs):
        self.priority = priority
        self.job_id = job_id  # Associate task with a parent job for tracking
        self.func = func
        self.args = args
        self.kwargs = kwargs

    def __lt__(self, other):
        # This makes the PriorityQueue sort by priority number (lower is higher priority)
        return self.priority < other.priority

    def run(self):
        """Executes the task's function."""
        return self.func(*self.args, **self.kwargs)


class TaskRunner:
    """
    The Foreman's Worker Pool.
    Manages a single global ThreadPoolExecutor and a PriorityQueue to
    process tasks in a highly efficient, prioritized manner.
    """

    def __init__(self):
        self.queue = PriorityQueue()
        self.executor = None
        self._stop_event = Event()
        self._worker_thread = None
        self.max_workers = 1  # Default to 1, will be configured on start

    def start(self):
        """Starts the main worker thread and initializes the ThreadPoolExecutor."""
        if self._worker_thread and self._worker_thread.is_alive():
            log.warning("TASK_RUNNER: Start called but worker is already running.")
            return

        log.info("TASK_RUNNER: Initializing and starting the global task runner...")
        settings = load_settings()
        # FIX: Correct the path to read the setting from job.download.total_processing_cores
        self.max_workers = settings.get("job", {}).get("download", {}).get("total_processing_cores", 4)
        log.info(f"TASK_RUNNER: Worker pool configured with {self.max_workers} total threads.")

        self.executor = ThreadPoolExecutor(max_workers=self.max_workers)
        self._stop_event.clear()
        self._worker_thread = Thread(target=self._worker_loop, daemon=True)
        self._worker_thread.start()
        log.info("TASK_RUNNER: Worker thread has started.")

    def stop(self):
        """Stops the worker thread and shuts down the ThreadPoolExecutor."""
        if not self._worker_thread or not self._worker_thread.is_alive():
            return

        log.info("TASK_RUNNER: Stopping the global task runner...")
        self._stop_event.set()
        # Add a dummy item to unblock the queue.get() call if it's waiting
        self.queue.put(None)
        self._worker_thread.join(timeout=10)
        self.executor.shutdown(wait=True)
        log.info("TASK_RUNNER: Gracefully shut down.")

    def submit_task(self, task: Task):
        """Adds a new task to the priority queue."""
        if not self._worker_thread or not self._worker_thread.is_alive():
            log.error("TASK_RUNNER: Attempted to submit task, but the runner is not active.")
            return
        log.info(f"TASK_RUNNER: Submitting new task for Job {task.job_id} with priority {task.priority.name}")
        self.queue.put(task)

    def _worker_loop(self):
        """The main loop that pulls tasks from the queue and submits them to the thread pool."""
        while not self._stop_event.is_set():
            try:
                # The `get()` call will block until an item is available.
                task = self.queue.get()

                if task is None:  # Sentinel value to exit
                    continue

                # Submit the task's `run` method to the thread pool.
                # The thread pool will handle scheduling it on a free worker thread.
                self.executor.submit(self._run_and_log_task, task)

            except Exception as e:
                log.error(f"TASK_RUNNER: An unexpected error occurred in the worker loop: {e}", exc_info=True)
                # Avoid a tight loop on continuous errors
                time.sleep(1)

    def _run_and_log_task(self, task: Task):
        """Wrapper to execute a task and handle logging and task completion."""
        try:
            log.info(f"TASK_RUNNER: Worker picked up task for Job {task.job_id} (Priority: {task.priority.name})")
            task.run()
        except Exception as e:
            log.error(f"TASK_RUNNER: Task for Job {task.job_id} failed with an exception: {e}", exc_info=True)
        finally:
            # This is crucial for the PriorityQueue to know the task is done.
            self.queue.task_done()
            log.info(f"TASK_RUNNER: Worker finished task for Job {task.job_id} (Priority: {task.priority.name})")


# --- Global Instance ---
# Create a single, global instance of the TaskRunner that the entire application will share.
task_runner = TaskRunner()

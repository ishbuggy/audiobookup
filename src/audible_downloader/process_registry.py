# audible_downloader/process_registry.py

from collections import defaultdict
from threading import Lock

from .logger import log


class ProcessRegistry:
    """
    A thread-safe registry to track active subprocesses associated with specific jobs.
    Allows for targeted termination of all processes (e.g., ffmpeg, audible-cli)
    spawned by a specific job ID.
    """

    def __init__(self):
        self._lock = Lock()
        # Mapping: job_id -> set of subprocess.Popen objects
        self._active_processes = defaultdict(set)

    def register(self, job_id, process):
        """
        Registers a subprocess.Popen instance to a job ID.
        """
        if job_id is None or process is None:
            return

        with self._lock:
            self._active_processes[job_id].add(process)
            # log.debug(f"REGISTRY: Registered process PID {process.pid} for Job {job_id}")

    def unregister(self, job_id, process):
        """
        Removes a process from the registry. Should be called in a 'finally' block
        after the process completes.
        """
        if job_id is None:
            return

        with self._lock:
            if job_id in self._active_processes:
                self._active_processes[job_id].discard(process)
                # Clean up the dictionary key if empty to prevent memory leaks
                if not self._active_processes[job_id]:
                    del self._active_processes[job_id]

    def kill_job_processes(self, job_id):
        """
        Sends SIGTERM to all active processes associated with the given job_id.
        Returns the number of processes terminated.
        """
        count = 0
        with self._lock:
            processes = list(self._active_processes.get(job_id, []))

        if not processes:
            log.info(f"REGISTRY: No active subprocesses found for Job {job_id} to kill.")
            return 0

        log.warning(f"REGISTRY: Attempting to kill {len(processes)} active processes for Job {job_id}...")

        for proc in processes:
            try:
                if proc.poll() is None:  # Check if process is still running
                    log.info(f"REGISTRY: Sending SIGTERM to PID {proc.pid}...")
                    proc.terminate()  # Send request to terminate
                    count += 1
                else:
                    # Process already dead, just cleanup
                    self.unregister(job_id, proc)
            except Exception as e:
                log.error(f"REGISTRY: Failed to kill PID {proc.pid}: {e}")

        return count


# Global instance to be imported by other modules
process_registry = ProcessRegistry()

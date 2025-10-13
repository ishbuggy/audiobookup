# main.py

# --- Main application entry point ---
# This file is responsible for starting the web server.

# Import the atexit module for graceful shutdow
import atexit

# Import the 'os' module to interact with the filesystem
import os

# Import the app and socketio objects.
from audible_downloader import app, socketio, TEMP_DIR

# Import the cleanup function.
from audible_downloader.db import cleanup_stale_jobs

# Import and initialize the logger
from audible_downloader.logger import log

# Import the  function to start the scheduler thread
from audible_downloader.scheduler import scheduler_worker

# Import our new global task_runner instance
from audible_downloader.task_runner import task_runner

import os

if __name__ == "__main__":

    # Ensure the temporary processing directory exists on startup.
    # This prevents errors if the directory gets deleted or is missing on a fresh install.
    os.makedirs(TEMP_DIR, exist_ok=True)

    # Run the startup cleanup task for any jobs that were left running.
    cleanup_stale_jobs()

    # Start the global task runner's worker pool
    task_runner.start()

    # Register the task runner's stop method to be called on application exit
    atexit.register(task_runner.stop)

    # Start the scheduler's management worker as a background task managed by SocketIO.
    # This ensures the scheduler starts up correctly within the web server's context,
    # avoiding conflicts with the development server's threading model.
    socketio.start_background_task(target=scheduler_worker)

    log.info("Starting Flask-SocketIO server...")

    # Start the Flask-SocketIO server.
    # allow_unsafe_werkzeug is needed to support the shutdown endpoint for now.
    socketio.run(app, host="0.0.0.0", port=13300, debug=False, allow_unsafe_werkzeug=True, use_reloader=False)

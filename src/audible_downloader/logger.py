# audible_downloader/logger.py

import logging
import sys
from logging.handlers import RotatingFileHandler

# Import the centralized path for the log file
from . import LOG_FILE


def setup_logging():
    """
    Configures the root logger for the application.

    This setup directs log messages to two places:
    1. The console (standard output), which is visible via 'docker logs'.
    2. A rotating file handler that writes to the persistent log file in /config.
    """
    # Get the root logger
    logger = logging.getLogger()
    logger.setLevel(logging.DEBUG)  # Set the minimum level of logs to capture

    # Create a formatter to define the log message structure
    formatter = logging.Formatter("%(asctime)s - %(levelname)s - %(message)s", datefmt="%Y-%m-%d %H:%M:%S")

    # --- Console Handler ---
    # This handler sends logs to the standard output
    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setFormatter(formatter)
    console_handler.setLevel(logging.INFO)

    # Add the handlers to the root logger
    # Check if handlers are already present to avoid duplication on reloads
    if not logger.handlers:
        logger.addHandler(console_handler)

        # --- File Handler ---
        # This handler writes logs to the specified log file. If the log file
        # can't be opened (e.g. running outside the container, or a read-only
        # /config mount), fall back to console-only logging instead of crashing
        # at import time.
        try:
            # Rotate at 10 MB, keeping 3 old files, so app.log can't grow unbounded
            # (it lives on the user's /config volume and is downloadable from the UI).
            file_handler = RotatingFileHandler(LOG_FILE, maxBytes=10 * 1024 * 1024, backupCount=3, encoding="utf-8")
            file_handler.setFormatter(formatter)
            file_handler.setLevel(logging.DEBUG)
            logger.addHandler(file_handler)
        except OSError as e:
            logger.warning(f"Could not open log file {LOG_FILE} ({e}). Continuing with console logging only.")

    return logger


# Create and configure the logger instance when this module is first imported
log = setup_logging()

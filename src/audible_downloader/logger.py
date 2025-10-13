# audible_downloader/logger.py

import logging
import sys

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
    logger.setLevel(logging.INFO)  # Set the minimum level of logs to capture

    # Create a formatter to define the log message structure
    formatter = logging.Formatter("%(asctime)s - %(levelname)s - %(message)s", datefmt="%Y-%m-%d %H:%M:%S")

    # --- Console Handler ---
    # This handler sends logs to the standard output
    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setFormatter(formatter)

    # --- File Handler ---
    # This handler writes logs to the specified log file
    file_handler = logging.FileHandler(LOG_FILE, encoding="utf-8")
    file_handler.setFormatter(formatter)

    # Add both handlers to the root logger
    # Check if handlers are already present to avoid duplication on reloads
    if not logger.handlers:
        logger.addHandler(console_handler)
        logger.addHandler(file_handler)

    return logger


# Create and configure the logger instance when this module is first imported
log = setup_logging()

# audible_downloader/health_check.py

import os
import subprocess
import time
from threading import Lock

from . import DATABASE_DIR
from .logger import log

# --- State Management for Auth Health ---
# This dictionary will hold the latest health status, making it accessible
# to the rest of the application without needing to re-run the check.
_auth_status = {
    "is_valid": None,  # None indicates the check has not run yet
    "error": "",
    "last_checked": None,
}
_auth_status_lock = Lock()


def get_audible_auth_status():
    """Thread-safe function to get the current auth status."""
    with _auth_status_lock:
        return _auth_status.copy()


def perform_audible_auth_check():
    """
    The core logic that runs audible-cli to check the auth status.
    This function updates the global _auth_status dictionary.
    """
    log.info("AUDIBLE_AUTH_CHECK: Performing periodic Audible connection status check...")
    try:
        # 1. Copy the current process's environment. This preserves the PATH.
        env = os.environ.copy()
        # 2. Set/override the HOME variable to our persistent config directory.
        env["HOME"] = DATABASE_DIR

        result = subprocess.run(
            ["audible", "api", "/1.0/customer/status", "-p", "response_groups=benefits_status"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            env=env,
            timeout=30,
        )

        is_valid_now = result.returncode == 0
        error_message = ""

        if not is_valid_now:
            raw_error = result.stderr or result.stdout
            if "token has been expired" in raw_error:
                error_message = "Your Audible authentication token has expired. Please re-authenticate."
            else:
                error_message = "Authentication with Audible is invalid. Please check the logs and re-authenticate."
            log.warning(f"HEALTH_CHECK: Auth check failed. Message: {error_message}")
        else:
            log.info("HEALTH_CHECK: Authentication status is valid.")

        # Update the shared state dictionary safely
        with _auth_status_lock:
            _auth_status["is_valid"] = is_valid_now
            _auth_status["error"] = error_message
            _auth_status["last_checked"] = time.time()

    except FileNotFoundError:
        with _auth_status_lock:
            _auth_status["is_valid"] = False
            _auth_status["error"] = "'audible-cli' not found in the system's PATH."
    except subprocess.TimeoutExpired:
        with _auth_status_lock:
            _auth_status["is_valid"] = False
            _auth_status["error"] = "The authentication check timed out."
    except Exception as e:
        log.error(f"HEALTH_CHECK: An unexpected error occurred: {e}", exc_info=True)
        with _auth_status_lock:
            _auth_status["is_valid"] = False
            _auth_status["error"] = "An unexpected error occurred during the authentication check."


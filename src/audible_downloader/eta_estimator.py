# audible_downloader/eta_estimator.py

import json
import os
from collections import deque

from . import CONFIG_DIR
from .logger import log

# The path to our simple JSON database for ETA history
ETA_CACHE_FILE = os.path.join(CONFIG_DIR, ".eta_cache.json")
# The number of recent conversions to keep for averaging
HISTORY_LENGTH = 30


def _load_cache():
    """Loads the ETA history from the JSON file."""
    if not os.path.exists(ETA_CACHE_FILE):
        return {"conversion_rates": []}
    try:
        with open(ETA_CACHE_FILE) as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError):
        log.warning("ETA_ESTIMATOR: Could not read or parse ETA cache file. Starting fresh.")
        return {"conversion_rates": []}


def _save_cache(data):
    """Saves the ETA history to the JSON file."""
    try:
        with open(ETA_CACHE_FILE, "w") as f:
            json.dump(data, f)
    except OSError:
        log.error("ETA_ESTIMATOR: Could not write to ETA cache file.")


def record_conversion_time(runtime_min, duration_sec):
    """
    Records the performance of a completed conversion to improve future estimates.

    Args:
        runtime_min (int): The total runtime of the audiobook in minutes.
        duration_sec (int): The time the conversion process took in seconds.
    """
    if not runtime_min or not duration_sec or runtime_min == 0:
        return

    # Calculate the rate: how many seconds of processing it took for one minute of audio
    rate = duration_sec / runtime_min

    cache = _load_cache()
    # Use a deque to automatically manage the history length
    history = deque(cache.get("conversion_rates", []), maxlen=HISTORY_LENGTH)
    history.append(rate)

    cache["conversion_rates"] = list(history)
    _save_cache(cache)
    log.info(f"ETA_ESTIMATOR: Recorded new conversion rate: {rate:.2f} sec/min")


def estimate_conversion_time(runtime_min):
    """
    Estimates the conversion time for a book based on historical data.

    Args:
        runtime_min (int): The total runtime of the new audiobook in minutes.

    Returns:
        int: The estimated time in seconds, or 0 if no estimate can be made.
    """
    if not runtime_min or runtime_min == 0:
        return 0

    cache = _load_cache()
    rates = cache.get("conversion_rates", [])

    if not rates:
        # If we have no history, use a conservative default guess: 10 seconds per minute of audio
        return runtime_min * 10

    # Calculate the average rate from our history
    average_rate = sum(rates) / len(rates)
    estimated_seconds = runtime_min * average_rate

    log.info(
        f"ETA_ESTIMATOR: Estimated conversion time for {runtime_min} min book is {int(estimated_seconds)}s "
        f"(avg rate: {average_rate:.2f} sec/min)"
    )
    return int(estimated_seconds)

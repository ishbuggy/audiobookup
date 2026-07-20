# audible_downloader/settings.py

import copy
import json
import os
from threading import Lock

from werkzeug.security import generate_password_hash  # type: ignore

# Import the centralized path for the settings file
from audible_downloader import SETTINGS_FILE

# --- Settings Configuration (Centralized) ---
# This module now owns all logic related to loading, saving, and
# managing the application's settings.

DEFAULT_SETTINGS = {
    "username": "admin",
    "password_hash": generate_password_hash("changeme"),
    "initial_setup_complete": False,
    "advanced_mode_enabled": False,
    "job": {
        "download": {
            "max_parallel_downloads": 2,
            "total_processing_cores": 2,
        }
    },
    "naming": {
        "template": "{author}/{title}/{author} - {title}",
        # When true, drop a trailing "Main Title: Subtitle" subtitle from the
        # {title} used in filenames (e.g. "999: The Extraordinary..." -> "999").
        # Affects filenames only; embedded metadata keeps the full title.
        "truncate_subtitle": False,
    },
    "conversion": {
        "quality": "High",
        "is_chunked_conversion_enabled": False,
        # Download a companion PDF (booklet/supplementary material) alongside
        # the audiobook when Audible ships one, placed next to the .m4b.
        "download_supplementary_pdf": True,
    },
    "tasks": {
        "timezone": "UTC",
        "audible_auth_check_interval_hours": 6,
        # Rename for clarity: This is the "Fast" (API-only) sync.
        "is_auto_fast_sync_enabled": False,
        "fast_sync_schedule": {
            "cron": "0 */4 * * *",  # Default: Run every 4 hours.
        },
        # Add a new, separate schedule for the "Deep" (full filesystem scan) sync.
        "is_auto_deep_sync_enabled": False,
        "deep_sync_schedule": {
            "cron": "0 3 * * *",  # Default: Run once a day at 3:00 AM
        },
        "is_auto_process_enabled": False,
        "process_schedule": {
            "cron": "0 4 * * *",  # Default: Run once a day at 4:00 AM
        },
        "auto_process_new": True,
        "auto_process_missing": True,
        "auto_process_error": False,
        "process_new_on_sync": True,
    },
}
settings_lock = Lock()


def deep_update(source, overrides):
    """Recursively update a dictionary."""
    for key, value in overrides.items():
        if isinstance(value, dict) and key in source:
            source[key] = deep_update(source.get(key, {}), value)
        else:
            source[key] = value
    return source


def load_settings():
    """Securely loads settings from settings.json, falling back to defaults."""
    # Always hand out a deep copy: callers mutate the returned dict (e.g. the
    # settings API merges request data into it), and a shallow .copy() shares
    # the nested dicts with DEFAULT_SETTINGS, letting those edits corrupt the
    # defaults for the rest of the process lifetime.
    if not os.path.exists(SETTINGS_FILE):
        return copy.deepcopy(DEFAULT_SETTINGS)
    with settings_lock:
        try:
            with open(SETTINGS_FILE) as f:
                loaded_settings = json.load(f)
            # Start with defaults and layer the loaded settings on top
            # to ensure all keys are present.
            settings = copy.deepcopy(DEFAULT_SETTINGS)
            deep_update(settings, loaded_settings)
            return settings
        except (OSError, json.JSONDecodeError) as e:
            print(f"Error loading settings.json: {e}. Using default settings.")
            return copy.deepcopy(DEFAULT_SETTINGS)


def save_settings(settings_dict):
    """Performs a safe save of the settings dictionary to settings.json."""
    with settings_lock:
        temp_file = SETTINGS_FILE + ".tmp"
        try:
            with open(temp_file, "w") as f:
                json.dump(settings_dict, f, indent=4)
            # Use os.rename for an atomic operation to prevent corruption
            os.rename(temp_file, SETTINGS_FILE)
            return True
        except (OSError, TypeError) as e:
            print(f"Error saving settings: {e}")
            return False

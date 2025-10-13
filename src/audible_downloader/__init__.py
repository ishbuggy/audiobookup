# audible_downloader/__init__.py

import os
import queue
from threading import Event

from flask import Flask  # type: ignore
from flask_socketio import SocketIO  # type: ignore

# --- Basic App Setup ---
# This is the central place where the Flask app and its extensions are initialized.

# Define the config directory, which is crucial for the app's operation
CONFIG_DIR = "/config"
DATABASE_DIR = "/database"

# Create a global threading event that will be used to signal the scheduler
# that it needs to reload its configuration.
settings_changed_event = Event()

# Initialize the Flask app instance
# We specify the template folder relative to the instance path.
app = Flask(__name__, template_folder="../templates", static_folder="../static")

SECRET_FILE = os.path.join(CONFIG_DIR, "secret.key")
try:
    with open(SECRET_FILE) as f:
        # Read the key from the file and remove any trailing newline
        app.config["SECRET_KEY"] = f.read().strip()
except FileNotFoundError:
    # This is a fallback for the very first startup before the entrypoint runs.
    # The actual persistent key will be used on subsequent runs.
    print("WARNING: secret.key not found. Using a temporary, insecure key for first boot.")
    app.config["SECRET_KEY"] = "a-temporary-insecure-key-for-first-boot"

# Initialize the SocketIO instance, attaching it to the Flask app.
# The path is important for the setup process Nginx/proxy configurations.
socketio = SocketIO(app, async_mode="threading", path="/setup/socket.io")

# --- Dynamically determined paths ---
# These can be regenerated, so they stay in /config
COVERS_DIR = os.path.join(CONFIG_DIR, "covers")
LOG_FILE = os.path.join(CONFIG_DIR, "app.log")
SETTINGS_FILE = os.path.join(CONFIG_DIR, "settings.json")
TEMP_DIR = os.path.join(CONFIG_DIR, "temp_processing")
MAX_LOG_LINES = 500  # This is a configuration value, not a path, but fits well here.

# Persistent user database files, pointed to /database
DB_FILE = os.path.join(DATABASE_DIR, "library.db")
SETUP_FLAG_FILE = os.path.join(DATABASE_DIR, ".setup_complete")

# These hidden caches are also critical to performance and user data
ETA_CACHE_FILE = os.path.join(DATABASE_DIR, ".eta_cache.json")
FILE_SCAN_CACHE = os.path.join(DATABASE_DIR, ".file_scan_cache")


# --- START: ADD MessageAnnouncer Class and Instance ---
class MessageAnnouncer:
    """A simple pub/sub class for broadcasting server-sent events."""

    def __init__(self):
        self.listeners = []

    def listen(self):
        """Adds a new listener (queue) to the list and returns it."""
        q = queue.Queue(maxsize=10)
        self.listeners.append(q)
        return q

    def announce(self, msg):
        """Pushes a message to all active listeners."""
        for i in reversed(range(len(self.listeners))):
            try:
                self.listeners[i].put_nowait(msg)
            except queue.Full:
                del self.listeners[i]


# Create a single, global instance of the announcer that the whole app can import
announcer = MessageAnnouncer()
# --- END: ADD MessageAnnouncer ---

# --- Import Routes and Handlers ---
# These imports are placed at the end to avoid circular dependencies.
# Importing these modules registers their routes and event handlers with the app.
from audible_downloader import routes, setup_pty  # noqa: E402, F401, I001

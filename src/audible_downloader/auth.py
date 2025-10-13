# audible_downloader/auth.py

import os
from functools import wraps

from flask import redirect, request, session, url_for  # type: ignore
from werkzeug.security import check_password_hash  # type: ignore

# Import the path to the flag file from our main __init__
from . import SETUP_FLAG_FILE

# Import the settings functions and constants directly from the settings module
from .settings import load_settings


# --- Authentication Verification ---
def verify_credentials(username, password):
    """Verifies credentials against the stored settings."""
    # Use the canonical load_settings function
    settings = load_settings()
    stored_user = settings.get('username')
    stored_hash = settings.get('password_hash')
    if stored_user and stored_hash:
        return username == stored_user and check_password_hash(stored_hash, password)
    return False

# --- Decorator for Protecting Routes ---
def login_required(f):
    """
    Decorator to ensure a user is logged in and has completed all setup steps
    in the correct order by checking the filesystem directly.
    """
    @wraps(f)
    def decorated_function(*args, **kwargs):
        settings = load_settings()

        # Priority 1: Check if a user is logged in.
        if 'username' not in session:
            return redirect(url_for('login', next=request.url))

        # Priority 2: Check if the initial password has been changed.
        if not settings.get('initial_setup_complete', False):
            if request.endpoint not in ['initial_setup', 'logout', 'static']:
                return redirect(url_for('initial_setup'))
            else:
                return f(*args, **kwargs)

        # Priority 3: Perform a LIVE check for the Audible setup flag file.
        if not os.path.exists(SETUP_FLAG_FILE):
            # If the flag file is missing, Audible setup is still required.
            if (request.endpoint not in ['setup', 'logout', 'static']
                    and not request.path.startswith('/setup/socket.io')):
                return redirect(url_for('setup'))

        # If all checks pass, the user can access the requested page.
        return f(*args, **kwargs)
    return decorated_function

#!/bin/bash
set -e

# Set a sane umask for file permissions (drwxrwxr-x for dirs, -rw-rw-r-- for files).
# Overridable via the UMASK env var, e.g. UMASK=0000 for world-writable output
# (some NAS/share setups want this — it was the hardcoded behavior before v0.18.0).
umask "${UMASK:-0002}"

# --- Generate secret key on first run ---
SECRET_FILE="/config/secret.key"

if [ ! -f "$SECRET_FILE" ]; then
    echo "INFO: secret.key not found. Generating new secret key..."
    head /dev/urandom | tr -dc A-Za-z0-9 | head -c 32 > "$SECRET_FILE"
    echo "INFO: New secret key generated."
fi

# --- Use PUID/PGID or default to 1000 ---
PUID=${PUID:-1000}
PGID=${PGID:-1000}

# --- Modify the appuser to match the new IDs ---
# We use || true to suppress errors if the ID is already set or reserved
echo "Updating user IDs to PUID=$PUID and PGID=$PGID"
groupmod -o -g "$PGID" appuser || true
usermod -o -u "$PUID" appuser || true

# --- Set permissions for the persistent data directories ---
# We always fix /config and /database as they are app-internal and critical.
echo "Ensuring permissions on /config and /database..."
chown -R appuser:appuser /config
chown -R appuser:appuser /database

# --- Handle /data permissions (The Mac Bottleneck) ---
# On macOS or large libraries, chown -R /data can take forever or fail.
# We allow skipping it via an env var, and we don't let it crash the boot.
if [ "$SKIP_DATA_PERMS" != "true" ]; then
    echo "Ensuring permissions on /data (Set SKIP_DATA_PERMS=true to disable)..."
    # We use '|| echo' to catch errors (like Read-only file systems) and continue anyway
    chown -R appuser:appuser /data || echo "WARNING: Failed to chown some files in /data. Continuing anyway."
else
    echo "Skipping permissions check on /data as requested."
fi

# --- Drop root privileges and execute the original command ---
echo "Switching to user 'appuser' to run the application..."
exec gosu appuser "$@"
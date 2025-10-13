#!/bin/bash
set -e

# --- Generate secret key on first run ---
# Define the path for the persistent secret key within the /config volume
SECRET_FILE="/config/secret.key"

# Check if the secret key file does not exist
if [ ! -f "$SECRET_FILE" ]; then
    echo "INFO: secret.key not found. Generating new secret key..."
    # Generate 32 random alphanumeric characters and save to the file
    head /dev/urandom | tr -dc A-Za-z0-9 | head -c 32 > "$SECRET_FILE"
    echo "INFO: New secret key generated and saved to $SECRET_FILE."
fi

# Entrypoint sets permissions on persistent volumes and switches to non-root user

# --- Use PUID/PGID or default to 1000 ---
PUID=${PUID:-1000}
PGID=${PGID:-1000}

# --- Modify the appuser to match the new IDs ---
echo "Updating user IDs to PUID=$PUID and PGID=$PGID"
groupmod -o -g "$PGID" appuser
usermod -o -u "$PUID" appuser

# --- Set permissions for the persistent data directories ---
echo "Ensuring permissions are set correctly..."
chown -R appuser:appuser /config
chown -R appuser:appuser /database
chown -R appuser:appuser /data

# --- Drop root privileges and execute the original command ---
echo "Switching to user 'appuser' to run the application..."
exec gosu appuser "$@"
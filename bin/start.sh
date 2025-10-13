#!/bin/bash
#
# Start Script: start.sh
# This script is the main gatekeeper for the application. It runs every time
# the container starts and determines whether to launch in "Normal Mode" or
# "Setup Mode" based on the presence of a flag file.
#

# Exit immediately if any command fails, ensuring a clean exit state.
set -e

# --- CRITICAL: SET HOME DIRECTORY ---
# The HOME directory MUST now point to our new critical data volume
# so that .audible auth files are stored there safely.
export HOME=/database

# --- DEFINE KEY PATHS ---
DATABASE_DIR="/database"
CONFIG_DIR="/config"
SETUP_FLAG_FILE="$DATABASE_DIR/.setup_complete"
DB_FILE="$DATABASE_DIR/library.db"

# --- Database Initialization & Migration ---

# The full schema definition is now centralized here.
# We use an associative array for easy lookup of the full column definition.
declare -A DB_SCHEMA
DB_SCHEMA["asin"]="asin TEXT PRIMARY KEY"
DB_SCHEMA["author"]="author TEXT"
DB_SCHEMA["title"]="title TEXT"
DB_SCHEMA["status"]="status TEXT"
DB_SCHEMA["series"]="series TEXT"
DB_SCHEMA["narrator"]="narrator TEXT"
DB_SCHEMA["runtime_min"]="runtime_min INTEGER"
DB_SCHEMA["release_date"]="release_date TEXT"
DB_SCHEMA["filepath"]="filepath TEXT"
DB_SCHEMA["error_message"]="error_message TEXT"
DB_SCHEMA["publisher"]="publisher TEXT"
DB_SCHEMA["language"]="language TEXT"
DB_SCHEMA["purchase_date"]="purchase_date TEXT"
DB_SCHEMA["summary"]="summary TEXT"
DB_SCHEMA["is_summary_full"]="is_summary_full INTEGER DEFAULT 0"
DB_SCHEMA["date_added"]="date_added TEXT"
DB_SCHEMA["retry_count"]="retry_count INTEGER DEFAULT 0"

if [ ! -f "$DB_FILE" ]; then
    echo "Database file not found. Creating a new one in $DATABASE_DIR..."
    IFS=,
    create_statement="${!DB_SCHEMA[*]}"
    sqlite3 "$DB_FILE" "CREATE TABLE audiobooks (${create_statement// / });"
    echo "Database created successfully."
else
    echo "Database found. Verifying schema..."
    existing_columns=$(sqlite3 "$DB_FILE" "PRAGMA table_info(audiobooks);" | cut -d'|' -f2)
    for col_name in "${!DB_SCHEMA[@]}"; do
        if ! echo "$existing_columns" | grep -q "^${col_name}$"; then
            col_def=${DB_SCHEMA[$col_name]}
            echo "Schema mismatch. Adding missing column: '$col_name'..."
            sqlite3 "$DB_FILE" "ALTER TABLE audiobooks ADD COLUMN $col_def;"
            echo " -> Column '$col_name' added."
        fi
    done
    echo "Schema verification complete."
fi

# Create Job Management tables if they don't exist
if ! sqlite3 "$DB_FILE" ".table jobs" | grep -q "jobs"; then
    echo "Creating 'jobs' table..."
    sqlite3 "$DB_FILE" "CREATE TABLE jobs (job_id INTEGER PRIMARY KEY AUTOINCREMENT, job_type TEXT NOT NULL, status TEXT NOT NULL, start_time TEXT NOT NULL, end_time TEXT);"
    echo " -> 'jobs' table created."
fi
if ! sqlite3 "$DB_FILE" ".table job_items" | grep -q "job_items"; then
    echo "Creating 'job_items' table..."
    sqlite3 "$DB_FILE" "CREATE TABLE job_items (item_id INTEGER PRIMARY KEY AUTOINCREMENT, job_id INTEGER NOT NULL, asin TEXT NOT NULL, status TEXT NOT NULL, log TEXT, FOREIGN KEY (job_id) REFERENCES jobs (job_id));"
    echo " -> 'job_items' table created."
fi
if ! sqlite3 "$DB_FILE" "PRAGMA table_info(jobs);" | cut -d'|' -f2 | grep -q "^job_params$"; then
    echo "Schema mismatch. Adding missing column: 'job_params' to 'jobs' table..."
    # Add the column. It will store job-specific parameters as a JSON string.
    sqlite3 "$DB_FILE" "ALTER TABLE jobs ADD COLUMN job_params TEXT;"
    echo " -> Column 'job_params' added."
fi

# --- Mode Selection Logic ---
echo "Checking for setup completion flag at $SETUP_FLAG_FILE..."
# The core logic of the script: check if the setup flag file exists.
if [ -f "$SETUP_FLAG_FILE" ]; then
    # --- NORMAL MODE ---
    # The flag exists, so setup is complete.
    echo "✅ Setup complete. Starting in NORMAL mode."
    # Use `exec` to replace the current shell process with the Python application.
    # This is more efficient as it avoids leaving an unnecessary shell process running.
    exec python3 /app-source/main.py
else
    # --- SETUP MODE ---
    # The flag is missing, so we must run the first-time setup.
    echo "⚠️ Setup flag not found. Entering SETUP MODE."

    AUDIBLE_CONFIG_DIR="$DATABASE_DIR/.audible"
    # To ensure a clean slate for the new authentication attempt,
    # remove any old or potentially corrupted auth files from previous attempts.
    if [ -d "$AUDIBLE_CONFIG_DIR" ]; then
        echo "Cleaning up old .audible directory from $DATABASE_DIR..."
        rm -rf "$AUDIBLE_CONFIG_DIR"
    fi
    export SETUP_MODE=true
    exec python3 /app-source/main.py
fi
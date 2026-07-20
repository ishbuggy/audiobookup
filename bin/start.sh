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
DB_SCHEMA["is_duplicate"]="is_duplicate INTEGER DEFAULT 0"
DB_SCHEMA["custom_title"]="custom_title TEXT"
DB_SCHEMA["custom_author"]="custom_author TEXT"
DB_SCHEMA["custom_cover"]="custom_cover INTEGER DEFAULT 0"

# Bash associative arrays have no defined iteration order, so CREATE TABLE
# and the rebuild migration below use this explicit column order.
DB_COLUMN_ORDER=(asin author title status series narrator runtime_min release_date filepath error_message publisher language purchase_date summary is_summary_full date_added retry_count is_duplicate custom_title custom_author custom_cover)

# Build the full column-definition list plus the column/select lists used
# by the rebuild migration. Columns with a DEFAULT get a COALESCE so rows
# from a defective (default-less) table are backfilled during the copy.
schema_defs=""
copy_cols=""
copy_selects=""
for col_name in "${DB_COLUMN_ORDER[@]}"; do
    schema_defs+="${DB_SCHEMA[$col_name]}, "
    copy_cols+="$col_name, "
    case "$col_name" in
        is_summary_full | retry_count | is_duplicate | custom_cover) copy_selects+="COALESCE($col_name, 0), " ;;
        *) copy_selects+="$col_name, " ;;
    esac
done
schema_defs="${schema_defs%, }"
copy_cols="${copy_cols%, }"
copy_selects="${copy_selects%, }"

if [ ! -f "$DB_FILE" ]; then
    echo "Database file not found. Creating a new one in $DATABASE_DIR..."
    sqlite3 "$DB_FILE" "CREATE TABLE audiobooks ($schema_defs);"
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

    # --- Repair migration for databases created by the old fresh-install path ---
    # Versions <= 0.17.0 created fresh tables from column NAMES only: no types,
    # no PRIMARY KEY on asin, no DEFAULT values. Detect the missing primary key
    # and rebuild the table with the correct schema, preserving all data.
    # This runs AFTER the column-add loop above so the old table is guaranteed
    # to have every column the copy expects.
    asin_pk=$(sqlite3 "$DB_FILE" "SELECT pk FROM pragma_table_info('audiobooks') WHERE name='asin';")
    if [ "$asin_pk" != "1" ]; then
        echo "Detected defective 'audiobooks' schema (asin is not PRIMARY KEY). Rebuilding table..."
        BACKUP_FILE="$DB_FILE.pre-schema-fix.bak"
        # Keep the FIRST backup if a previous rebuild attempt already made one.
        if [ ! -f "$BACKUP_FILE" ]; then
            cp "$DB_FILE" "$BACKUP_FILE"
            echo " -> Backed up database to $BACKUP_FILE"
        fi
        row_count_before=$(sqlite3 "$DB_FILE" "SELECT COUNT(*) FROM audiobooks;")
        # INSERT OR IGNORE: a defective table could hold duplicate ASINs that
        # the new PRIMARY KEY rejects; keep the first row rather than abort.
        if sqlite3 "$DB_FILE" "BEGIN TRANSACTION;
CREATE TABLE audiobooks_new ($schema_defs);
INSERT OR IGNORE INTO audiobooks_new ($copy_cols) SELECT $copy_selects FROM audiobooks;
DROP TABLE audiobooks;
ALTER TABLE audiobooks_new RENAME TO audiobooks;
COMMIT;"; then
            row_count_after=$(sqlite3 "$DB_FILE" "SELECT COUNT(*) FROM audiobooks;")
            echo " -> Rebuild complete: $row_count_after of $row_count_before row(s) migrated."
            if [ "$row_count_after" != "$row_count_before" ]; then
                echo " -> WARNING: $((row_count_before - row_count_after)) duplicate-ASIN row(s) dropped. Originals preserved in $BACKUP_FILE."
            fi
        else
            # Don't brick the container on a failed rebuild: restore the backup
            # and continue running on the old (defective but functional) schema.
            echo " -> ERROR: Schema rebuild failed. Restoring backup and continuing with the old schema."
            cp "$BACKUP_FILE" "$DB_FILE"
        fi
    fi
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
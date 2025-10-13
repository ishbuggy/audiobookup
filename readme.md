# AudioBookup

A self-hosted, web-based application for managing and downloading your personal Audible audiobook library. This entire system runs as a single Docker container, providing a seamless user experience from first-run authentication to day-to-day library management, all through a clean web interface.

## Features

- **Modern & Responsive UI:** Manage your library through a redesigned dashboard, optimized for both desktop and mobile use.
- **Light & Dark Modes:** Switch between light and dark themes, with your preference saved in your browser for future visits.
- **Secure User Login:** The entire web interface is protected by a persistent, session-based authentication system, with a mandatory password change on first launch.
- **Search, Sort, & Filter:** Instantly search your library by title, author, or narrator, filter by book status (New, Missing, etc.), and sort by multiple criteria.
- **Persistent Background Jobs:** Start a library sync or a batch download and safely close your browser. The job runs on the server and you can reconnect to it at any time.
- **Live Job Status Panel:** A collapsible "Job Status" panel shows **granular, real-time progress** with percentage completion and an **estimated time remaining**. Sync jobs now provide **phased updates** (e.g., "Phase 1/3: Fetching from Audible").
- **Independent Sync Modes (Fast & Deep):**
    - **Fast Sync (API-only):** A lightweight sync that only checks for new books from Audible. Perfect for frequent, low-impact checks.
    - **Deep Sync (Full Scan):** A comprehensive sync that also scans all local files to detect manual changes.
- **Advanced, Timezone-Aware Scheduling:** The application is powered by a robust, cron-based scheduler for maximum flexibility and reliability.
    - **Independent Schedules:** Configure separate, automated schedules for Fast Syncs, Deep Syncs, and Download jobs.
    - **Simple & Advanced Modes:** Configure schedules using a simple UI (e.g., "every 4 hours" or "daily at 02:00"). Enable **Advanced Mode** to set schedules with full, standard **cron expressions**.
    - **Timezone Support:** A dedicated setting allows you to select your local timezone, ensuring all scheduled jobs run at the correct local time.
- **Intelligent Parallel Processing:** The application uses a sophisticated, priority-based task runner to process multiple books and chapters in parallel. It intelligently allocates all available CPU cores to the highest-priority tasks, ensuring maximum efficiency and the fastest possible completion time for each book.
- **"Head-Start" Downloads:** For multi-book jobs, the system automatically prioritizes the download of the first book to begin the CPU-intensive encoding process as quickly as possible, while subsequent book downloads are staggered to run efficiently in the background.
- **Job Management:** Cancel in-progress download jobs directly from the UI.
- **Paginated Job History with Filtering & Search:** View a complete, paginated history of all past jobs on a dedicated `/history` page. The page includes powerful controls to instantly **filter** by job type and status, and to **search** for jobs containing specific books by title or author. History items for download jobs include book cover thumbnails for easy identification.
- **Detailed Book View:** Click on any book to see a detailed modal with high-resolution art and full metadata.
- **On-Demand Full Summaries:** Fetch full, untruncated book summaries on demand from the detail view.
- **Extensive Configuration:** Configure all features from a dedicated `/settings` page. This includes custom folder/file naming templates, audio quality, and a simplified "Job Settings" UI that allows for auto-detection of CPU cores in Normal Mode, or manual control over `Total Processing Cores` and `Max Parallel Downloads` in Advanced Mode.
- **Audible Connection Health Check:** The app automatically checks if its connection to Audible is still valid on a periodic basis and displays a prominent warning banner if re-authentication is needed.
- **DRM-Free Conversion:** Converts your audiobooks into standard `.m4b` files with chapters and metadata intact.
- **Simple Docker Deployment:** Runs as a single, easy-to-manage Docker container with a clean, separated data structure.

---

## Getting Started

### Prerequisites

- Docker & Docker Compose
- Git (for cloning the repository)

### Installation

1.  **Clone the Repository:**
    Open a terminal on your host machine and clone the project repository.

    ```bash
    git clone https://github.com/ishbuggy/audiobookup.git
    cd audiobookup
    ```

2.  **Configure Your Environment:**
    The project includes a `docker-compose.yml` file. Before launching, you need to edit this file to set your user permissions and timezone.

    Open the `docker-compose.yml` file and find the `environment` section:

    ```yaml
    environment:
        - PUID=1000
        - PGID=1000
        - TZ=Etc/UTC
    ```

    - **`PUID` and `PGID`:** Change `1000` to your user's ID to prevent file permission issues inside the container. You can find your ID by running the `id` command in your terminal.
    - **`TZ`:** Change `Etc/UTC` to your local timezone (e.g., `America/New_York`, `Europe/London`). A full list can be found [here](https://en.wikipedia.org/wiki/List_of_tz_database_time_zones). This is crucial for ensuring scheduled tasks run at the correct local time.

3.  **(Optional) Configure Data Paths:**
    By default, the application will create two new folders inside your project directory:
    - `appdata/`: For all application configuration and database files.
    - `audiobooks/`: For your final converted media files.

    If you want to store this data elsewhere (e.g., on a dedicated media server or for Unraid), you can edit the `volumes` section in `docker-compose.yml` and replace the relative paths (`./appdata/...`) with **absolute paths** (e.g., `/mnt/user/appdata/audiobookup/...`).

4.  **Launch the Application:**
    From inside the `audiobookup` project directory, run the following command. This will build the Docker image for the first time and start the container in the background.

    ```bash
    docker compose up --build -d
    ```

5.  **Access the Web Interface:**
    Navigate to `http://<your-server-ip>:13300` in your web browser.

---

## First-Time Setup

On the very first launch, the application requires a multi-step setup process to secure the application and connect it to your Audible account.

### Step 1: Initial Login & Password Change

1.  **Access the Login Page:** Navigate to `http://<your-server-ip>:13300`. You will be immediately redirected to a secure login page.
2.  **Use Default Credentials:** Log in with the default username `admin` and password `changeme`.
3.  **Set a Secure Password:** Upon your first successful login, you will be automatically redirected to a mandatory "Initial Setup" page. You must set a new, secure password for the administrator account before you can proceed.

### Step 2: Connect to Audible

After setting your password, you will be guided through a graphical user interface to connect to your Audible account.

1.  **Configure Connection:** Select your Audible marketplace region from the dropdown menu.
2.  **Start Connection:** Click the "Start Connection" button.
3.  **Open Login Page:** The application will communicate with Audible's servers. A new button, "Open Audible Login Page", will appear. Click this button to open the official Audible login page in a new browser tab.
4.  **Log In to Audible:** Log in to your Audible account in the new tab.
5.  **Copy the Redirect URL:** After logging in, your browser will be redirected to a page that likely shows an error (e.g., "Page not found"). **This is expected.** Copy the _entire URL_ from your browser's address bar.
6.  **Submit the URL:** Return to the application tab, paste the long URL into the input box, and click "Submit URL".
7.  **Validation & Success:** The application will validate your login. Upon success, you will be automatically redirected to the main dashboard.

---

## How to Use (Normal Mode)

Once setup is complete, the application will always start in **Normal Mode**, taking you directly to the main dashboard.

### Dashboard Overview

The dashboard is organized into a responsive two-column layout for a clear information hierarchy.

- **Header:** The header contains the application logo and title, along with quick-access icons for **Theme Toggling**, **Settings**, **Job History**, and **Logging Out**.
- **Main Action Column (Left):** This is the primary interaction area.
    - **Core Actions:** The main "Sync Library" and "Process Downloads" buttons are at the top.
    - **Automation Banner:** A banner appears here if automated tasks are disabled, linking directly to the relevant settings.
    - **Job Status:** When a job is active, a collapsible panel appears in this column, showing real-time progress. This panel persists even if you reload the page. You can **Cancel** running download jobs from here.
- **Status Column (Right):** This area provides an at-a-glance overview of your library.
    - **Library Status:** Colorful cards show the total number of books that are Downloaded, New, Missing, or have Errors.
- **Full Library:** Below the main columns, a responsive grid displays your entire audiobook library, which can be instantly **searched**, **sorted**, and **filtered by status**.
- **Status Bar & Activity Log:** A sticky footer shows the most recent status update and can be expanded to view the full application log.

### Core Actions

- **Sync Library:** The main button on the dashboard always performs a comprehensive **Deep Sync**. It connects to the Audible API, downloads cover art, and reconciles the database with a full scan of all local files. This runs as a **persistent background job**, so you can safely close the browser. More frequent, API-only **Fast Syncs** can be configured as a separate, automated task on the Settings page.
- **Process Downloads:** Opens a selection modal to start a batch download, which runs as a persistent background job.
- **Retry Button:** For any book with a status of `ERROR` or `MISSING`, a "Retry" button will appear on its card to process only that book using the persistent job system.

---

## Maintenance and Troubleshooting

### Updating the Application

Updating is now incredibly simple. If you have updated the source code (e.g., via `git pull`), you only need to rebuild the Docker image and restart the container.

```bash
docker-compose up --build -d
```

### Managing Settings

Application settings are stored on your host machine at `./appdata/config/settings.json`. You can back up this file to save your configuration.

### Clearing the Image Cache

Cover art is cached on your host machine at `./appdata/config/covers`. To refresh them:

1.  Stop the container: `docker-compose down`
2.  Delete the covers directory: `rm -rf ./appdata/config/covers`
3.  Restart and run a **Sync Library**.

### Resetting Your Audible Connection

If your connection to Audible expires (e.g., you change your Audible password), use the **"Re-authenticate"** button that appears in the UI banner. For a full manual reset of the Audible connection:

1.  Stop the container: `docker-compose down`
2.  Delete the Audible setup flag and the authentication directory from your **database** volume:
    ```bash
    rm ./appdata/database/.setup_complete
    rm -rf ./appdata/database/.audible
    ```
3.  Restart the container: `docker-compose up -d`. You will be guided through the **Audible Setup** part of the first-time setup process again.

### Resetting Your Local User Password

If you forget the password you set for the web UI, you can reset it manually:

1.  Stop the container: `docker-compose down`
2.  Open the settings file on your host machine: `./appdata/config/settings.json`.
3.  Find the `"initial_setup_complete"` key and set its value to `false`.
4.  Save and close the file.
5.  Restart the container: `docker-compose up -d`.
6.  You can now log in with the default credentials (`admin` / `changeme`) and will be forced to set a new password.

### Accessing the Database Manually

You can directly interact with the SQLite database for advanced debugging.

```bash
# Get a shell inside the running container
docker-compose exec audible-downloader /bin/bash

# Access the database file from its new location
sqlite3 /database/library.db

# Example: List all books with an ERROR status
sqlite> SELECT author, title FROM audiobooks WHERE status = 'ERROR';

# Exit sqlite and the container
sqlite> .exit
exit
```

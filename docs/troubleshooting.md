[← Docs index](README.md)

# Troubleshooting & Maintenance

This page covers routine upkeep, common problems, and how to dig deeper when something isn't working.

## Routine maintenance

### Clearing the image cache

Cover art is cached on your host machine (in the `/config` volume, under `covers/`). If images appear broken, or you just want to force a refresh:

1. Go to **Settings → Audible Connection**.
2. Click **Clear Cache**.
3. Confirm the action. All cached cover images are deleted and will be re-downloaded automatically during the next **Sync Library**.

See also [configuration.md#audible-connection](configuration.md#audible-connection).

### Verifying library integrity

If you suspect a downloaded file is corrupt or incomplete (for example, a 13-hour book that's only 2 hours long), you can audit your whole library:

1. Go to **Settings → Audible Connection**.
2. Click **Verify Files**.
3. The app scans every downloaded book on disk. Any book that's missing or truncated compared to what the library expects is marked **ERROR**.
4. Go back to the Dashboard, filter by **Error**, and click **Download** on the affected book(s) to fetch a correct copy.

### Managing settings

All of your app settings live in a single file, `settings.json`, inside the `/config` volume on your host (by default `./appdata/config/settings.json`).

The Settings page has **Export** and **Import** buttons for this file, so you can:

- Back up your configuration before making changes.
- Copy your settings to a fresh install.
- Restore a known-good configuration if something gets misconfigured.

### Optimizing startup speed

On every startup, the container walks your whole `/data` folder and fixes up file ownership so the app can write to it. On macOS (a side effect of how Docker Desktop shares files) or with very large libraries (1000+ books), that pass can take several minutes, long enough that the app can look like it has hung.

If this affects you, add the following to your `docker-compose.yml` and recreate the container:

```yaml
environment:
  - SKIP_DATA_PERMS=true
```

If you use this setting, make sure your host folder permissions are already correct. The app will not fix them up for you, and misconfigured permissions can cause it to fail to write files. See [installation.md](installation.md) for the full environment variable and volume reference.

## Fixing problems

### Audible connection problems

If your connection to Audible has expired (most commonly because you changed your Audible password), the dashboard shows a red banner prompting you to re-authenticate.

To fully reset the connection (also useful if you want to switch Audible accounts):

1. Go to **Settings → Audible Connection**.
2. In the **Reset Audible Connection** section, click **Reset**.
3. Confirm the action. The app securely deletes your stored Audible login and restarts itself.
4. You're redirected automatically into the **Audible connection wizard** (the same three screens you went through when you first installed the app) to reconnect. See [step 3 of first-time setup](setup.md#step-3-connect-your-audible-account) for a walkthrough of those screens. Your web-UI password is untouched; you won't be asked to set it again.

If you can't reach the UI at all, you can do this manually: stop the container, then delete the `.setup_complete` file and the `.audible` folder from your `/database` volume, and start the container again.

### Resetting your local web-UI password

If you forget the password you set for the web UI (not your Audible password), you can reset it back to the default by editing the settings file directly. AudioBookup falls back to its built-in defaults for any setting your file doesn't contain, so removing the stored password puts `changeme` back.

> **Note:** Run all `docker compose` commands on this page from the folder that contains your `docker-compose.yml`.

1. Stop the container:

   ```bash
   docker compose down
   ```

2. Open `settings.json` on your host machine (by default `./appdata/config/settings.json`).
3. Delete the entire `"password_hash": ...` line.

   Be careful to keep the file valid JSON: every line except the last one inside a block ends with a comma. If the line you deleted was the last one before a closing `}`, remove the now-trailing comma from the line above it. (If you'd rather not hand-edit, keep a copy of the file first: a broken `settings.json` makes the app start with all-default settings.)

4. **Optional:** if you also changed your username and have forgotten it, delete the `"username": ...` line the same way to get `admin` back. If you leave it in place, your custom username stays as it is.
5. Save and close the file.
6. Restart the container:

   ```bash
   docker compose up -d
   ```

7. Log in with your username (`admin` unless you kept a custom one) and the default password `changeme`.
8. **Immediately set a new password** from **Settings → Authentication Settings**. The app won't prompt you for one, so until you change it your install is sitting on the publicly-known default. See [configuration.md#authentication-settings](configuration.md#authentication-settings).

### Permission problems

If the app can't write to `/config`, `/database`, or `/data` (books stuck processing, startup errors, files owned by the wrong user on the host), the usual cause is a `PUID`/`PGID` mismatch between the container and your host user, or an `UMASK` that's too restrictive for how you're sharing the output (e.g. over an SMB/NAS share that needs world-writable files).

Check that `PUID`/`PGID` in your `docker-compose.yml` match your host user (find yours with the `id` command), and adjust `UMASK` if another user or device needs to read the finished files. See the environment variable and volume tables in [installation.md](installation.md) for the exact defaults and options.

### Reverse-proxy login failures

If you put a reverse proxy (nginx, Caddy, Traefik, etc.) in front of AudioBookup and logins or other actions start failing, check that the proxy forwards the `Host` header unchanged. The app's built-in CSRF protection compares each write request's `Origin` against that host, and a mismatch (which happens when the header isn't forwarded correctly) causes the request to be rejected.

### Why is a book Missing or Error?

These two statuses look similar but mean different things:

- **Missing**: the app expected to find a previously-downloaded file at a certain path and it isn't there anymore. This usually means the file was moved, renamed, or deleted outside the app (for example, by another program or manually on the host).
- **Error**: the most recent job for this book (download, conversion, etc.) failed. Open the book's detail view to see the specific error message, and check `app.log` (see **Getting detailed logs**, below) for more context.

Either way, you can typically fix it by filtering the Dashboard for that status and clicking **Download** to retry.

## Diagnostics

### Getting detailed logs

For debugging or reporting an issue, logs are available right from the dashboard footer:

- The copy icon (**Copy Log to Clipboard**): copies the currently visible log lines to your clipboard.
- The download icon (**Download Full Log**): downloads the complete `app.log` file, which includes `DEBUG`-level detail not shown in the in-UI viewer.

The full log is almost always more useful than what's visible on screen when reporting a problem, since the UI only shows a summary.

### Accessing the database manually

For advanced debugging, you can open the SQLite database directly inside the running container:

```bash
# Get a shell inside the running container
docker compose exec audiobookup /bin/bash

# Open the database file
sqlite3 /database/library.db

# Example: list all books with an ERROR status
sqlite> SELECT author, title FROM audiobooks WHERE status = 'ERROR';

# Exit sqlite, then the container
sqlite> .exit
exit
```

Treat this as a read-only inspection tool. `library.db` is irreplaceable data, so back up your `/database` volume before making any manual changes, and avoid editing the database while a Sync/Download/Verify job is running.

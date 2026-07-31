[← Docs index](README.md)

# Installation

AudioBookup ships as a single Docker container, and **Docker Compose is the supported way to install it**. This guide covers that path in full. Unraid users get a dedicated variant of the same steps below; anyone building from source instead should see [development.md](development.md).

## Prerequisites

- Docker and Docker Compose installed on your system (Docker Desktop on macOS/Windows, or `docker` + the `compose` plugin on Linux).
- An Audible account: this is what the app authenticates against to find and download your books.
- Enough free disk space, in two places. Your `/data` volume needs room for the finished library, since audiobooks are large. Separately, while a book is being processed its download and intermediate audio files are written under the `/config` volume, so `/config` needs a few books' worth of working headroom on top of its small settings and log files. If those two volumes live on different disks, don't leave `/config` on a nearly-full one.

## Install with Docker Compose

1. **Create a folder** on your server to hold the app's configuration:

   ```bash
   mkdir audiobookup
   cd audiobookup
   ```

2. **Get the compose file.** Download the project's [`docker-compose.yml`](../docker-compose.yml) into that folder, keeping the same filename. Use the real file rather than retyping it. It carries the three volume mappings the app needs, and a container started without them loses all of your data the next time it's recreated.

   For orientation, the file's shape is:

   ```yaml
   services:
     audiobookup:
       image: ghcr.io/ishbuggy/audiobookup:latest
       container_name: audiobookup
       ports:
         - "13300:13300"
       environment:
         - PUID=1000
         - PGID=1000
         - TZ=Etc/UTC
       volumes:
         - ./appdata/config:/config
         - ./appdata/database:/database
         - ./audiobooks:/data
       restart: unless-stopped
   ```

   The real file also includes commented-out optional settings (`SKIP_DATA_PERMS`, `UMASK`) explained in the [environment variable table](#environment-variables) below.

3. **Adjust it for your system.** Open the file and set:
   - `PUID` / `PGID` to match your host user (see the table below).
   - `TZ` to your local timezone.
   - The three volume paths on the left-hand side of each `:` mapping, if you want your config, database, and finished audiobooks stored somewhere other than the folder you just created.

4. **Start the container:**

   ```bash
   docker compose up -d
   ```

5. **Open the web UI** at `http://<your-server-ip>:13300`. The first launch walks you through a one-time setup wizard. See [First-time setup](setup.md).

## Configuration reference

### Environment variables

| Variable | Purpose |
|---|---|
| `PUID` | The user ID that owns files the app creates. Match your host user by running `id` in a terminal on your server. |
| `PGID` | The group ID that owns files the app creates. Also found via `id`. |
| `TZ` | The container's timezone, as an IANA name (e.g. `America/New_York`). This sets the *container's* clock, separate from the in-app **Scheduler Timezone** setting used for cron schedules; see [configuration.md#scheduled-tasks](configuration.md#scheduled-tasks). |
| `UMASK` | File-permission mask for files the app creates. Default `0002` (group-writable: `664` files, `775` directories). Set to `0000` for world-writable output, which some NAS/SMB share setups require. |
| `SKIP_DATA_PERMS` | Set to `true` to skip the startup permission fix-up on `/data`. Recommended on macOS (Docker file sharing makes this scan slow) or for libraries with 1000+ books, where it can otherwise delay startup by several minutes. |

### Volumes

| Path | Contents |
|---|---|
| `/config` | Application settings, logs, and temporary processing files. Regenerable: safe to delete if you're starting over, but you'll lose your saved settings. |
| `/database` | The library database and your Audible login. **Critical and irreplaceable**: back this up. |
| `/data` | Your finished, converted audiobooks. |

The example compose file maps all three to relative folders (`./appdata/...`, `./audiobooks`) next to the compose file itself. Unraid users, and anyone who might move the compose file later, should change these to absolute paths instead (e.g. `/mnt/user/appdata/audiobookup/config:/config`) so the container always finds the same data.

## Deployment notes

AudioBookup is built as a **single-user** application: one account, a handful of browser tabs on a private network, not a multi-tenant service. Each open dashboard tab holds a live connection for real-time job updates, so a few tabs are fine, but the app isn't designed for many concurrent users.

If you put a reverse proxy (nginx, Caddy, Traefik, etc.) in front of the app, it **must forward the `Host` header** correctly. The app's CSRF protection compares each write request's origin against that host, and a mismatch will cause logins and other actions to fail.

## Updating

1. From the folder containing your `docker-compose.yml`, pull the new image and recreate the container:

   ```bash
   docker compose pull
   docker compose up -d
   ```

2. If your compose file uses the `:latest` tag, that's all you need to do. If you've pinned a specific version (e.g. `ghcr.io/ishbuggy/audiobookup:vX.Y.Z`), edit the `image:` line to the new version tag first, then run the commands above.

Your settings, database, and audiobooks all live in the mounted volumes, not in the container itself, so they survive an update untouched.

<details>
<summary><b>Alternative: Unraid</b></summary>

AudioBookup works well on Unraid using the **Docker Compose Manager** plugin.

### Install

1. On Unraid, go to the **Apps** tab and install the **Docker Compose Manager** plugin.
2. Go to the **Docker** tab, open **Compose Manager**, and click **Add New Stack**.
3. Give the stack a name (e.g. `audiobookup`).
4. In the **Composition** box, paste in the contents of the project's [`docker-compose.yml`](../docker-compose.yml).
5. Edit the pasted content for your system:
   - Change `PUID` to `99` and `PGID` to `100` (Unraid's standard `nobody`/`users` IDs).
   - Change `TZ` to your correct timezone.
   - Change the `volumes` to absolute paths on your Unraid shares, for example:

     ```diff
     -      - ./appdata/config:/config
     -      - ./appdata/database:/database
     -      - ./audiobooks:/data
     +      - /mnt/user/appdata/audiobookup/config:/config
     +      - /mnt/user/appdata/audiobookup/database:/database
     +      - /mnt/user/Audiobooks:/data
     ```

6. Click **Save**, then click the gear icon next to the new stack and select **Compose Up**.
7. Access the web UI at `http://<your-server-ip>:13300`.

### Update

1. On your Unraid dashboard, go to the **Docker** tab and open **Compose Manager**.
2. Find your `audiobookup` stack in the list.
3. If you pinned a specific version, click **Edit**, change the image tag to the new version (e.g. `:vX.Y.Z`), and click **Save**. If you're using `:latest`, skip this step.
4. Click the gear icon next to the stack and select **Update**. This pulls the new image and recreates the container automatically.

</details>

## For developers

Building the image from source and running the local development stack are covered in [development.md](development.md).

---

**Next:** [First-time setup →](setup.md)

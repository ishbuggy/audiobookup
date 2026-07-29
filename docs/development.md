[← Docs index](README.md)

# Development

This page is for people building or modifying AudioBookup. If you just want to run it, see [installation.md](installation.md).

## Building from source

1.  **Clone the repository:**

    ```bash
    git clone https://github.com/ishbuggy/audiobookup.git
    cd audiobookup
    ```

2.  **Create your dev compose file** from the template the repo ships. Unlike the production `docker-compose.yml` (which pulls the pre-built image from GHCR), this one builds the image from your local checkout:

    ```bash
    cp docker-compose.dev.yml.template docker-compose.dev.yml
    ```

3.  **Edit `docker-compose.dev.yml`** to set your `PUID`, `PGID`, and volume paths. Your copy is git-ignored, so these local values stay out of the repo. See [The dev stack](#the-dev-stack) below for what else is in the file.

4.  **Build and launch:**

    ```bash
    docker compose -f docker-compose.dev.yml up -d --build
    ```

### Updating a source build

```bash
git pull
docker compose -f docker-compose.dev.yml up -d --build
```

`git pull` fetches the latest source; `--build` forces Docker to rebuild the image from it before restarting the container.

## The dev stack

`docker-compose.dev.yml` — the file you created from `docker-compose.dev.yml.template` in step 2 above — is the local development stack. Unlike the production `docker-compose.yml` (which pulls the pre-built image from `ghcr.io/ishbuggy/audiobookup`), it builds from `./src/dockerfile` against your local checkout.

> **Important:** Source is baked into the image at build time — there's no live-reload or volume-mounted source. **Every code change requires a rebuild** (`up -d --build`) to be visible in the running container.

The dev compose file also sets an `APP_VERSION` build arg (e.g. `v0.17.0-dev`), which controls the version string shown in the UI footer. Bump it if you want your dev build to display a distinct version.

## Repo layout at a glance

- **`src/`** — the Flask backend, HTML templates, and vanilla-JS (ES modules) frontend. No build step for the frontend — it's served as-is.
- **`bin/`** — the container entrypoint and start scripts. Database schema creation and migrations live in `bin/start.sh`, not in Python.
- **`tests/`** — the pytest suite. Runs on the host, not in a container (see [Tooling](#tooling)).
- **`docker-compose.yml`** — the user-facing example compose file that pulls the published image. Treat it as documentation for end users, not as a dev tool.
- **`.github/workflows/`** — CI. The Docker Publish workflow builds and publishes the multi-arch image whenever a `v*` tag is pushed.

## Tooling

**Python:** linted and formatted with [Ruff](https://docs.astral.sh/ruff/), configured in `pyproject.toml` (120-character line length; `E`, `W`, `F`, `I`, `UP` rule sets enabled).

```bash
ruff check src/
ruff format --check src/
```

**JS/HTML/CSS:** formatted with [Prettier](https://prettier.io/), configured in `.prettierrc.json` (4-space indentation, 120-character print width).

**Tests:** a pytest suite lives in `tests/` and runs on the host, not inside the container:

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements-dev.txt
pytest
```

`pyproject.toml` sets `pythonpath = ["src"]` so the app package resolves without installing it, and `tests/conftest.py` redirects the config and database directories to temporary folders — the suite never touches a real container or a real Audible account.

## Releases

Releases are triggered by pushing a `vX.Y.Z` git tag. The `.github/workflows/docker-publish.yml` workflow picks up the tag push, builds the multi-arch (`linux/amd64` + `linux/arm64`) image with `APP_VERSION` set from the tag, and publishes it to `ghcr.io/ishbuggy/audiobookup` — tagged with the version, the `major.minor` alias, and `latest` (for non-prerelease tags). Pushing the tag is what ships the release; there's no separate publish step.

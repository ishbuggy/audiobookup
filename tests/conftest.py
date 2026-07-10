# tests/conftest.py

import os
import tempfile

# The audible_downloader package resolves CONFIG_DIR and DATABASE_DIR from the
# environment at import time. Point both at throwaway directories BEFORE any
# test module imports the package, so no test can ever touch real data.
# (conftest.py is imported by pytest ahead of all test modules.)
os.environ["CONFIG_DIR"] = tempfile.mkdtemp(prefix="audiobookup-test-config-")
os.environ["DATABASE_DIR"] = tempfile.mkdtemp(prefix="audiobookup-test-database-")

import pytest  # noqa: E402


@pytest.fixture
def settings_file(tmp_path, monkeypatch):
    """
    Point the settings module at a per-test settings.json path (which does not
    exist yet — write to it in the test to simulate a saved settings file).
    Every consumer (auth, routes, ...) reads the path through the module-level
    SETTINGS_FILE binding in audible_downloader.settings, so patching that one
    attribute isolates the whole app.
    """
    from audible_downloader import settings as settings_module

    path = tmp_path / "settings.json"
    monkeypatch.setattr(settings_module, "SETTINGS_FILE", str(path))
    return path


@pytest.fixture
def client():
    """A Flask test client for the (singleton) application object."""
    from audible_downloader import app

    app.config["TESTING"] = True
    with app.test_client() as client:
        yield client

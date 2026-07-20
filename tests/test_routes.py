# tests/test_routes.py

import json
import os
import sqlite3
from urllib.parse import quote

import pytest


def _login(client, next_value=None, password="changeme"):
    """POST valid (by default) credentials, optionally with a ?next= target."""
    url = "/login"
    if next_value is not None:
        url = f"/login?next={quote(next_value, safe='')}"
    return client.post(url, data={"username": "admin", "password": password})


class TestLoginNextValidation:
    """H1 regression: the post-login redirect must never leave the app."""

    def test_local_path_is_honored(self, client, settings_file):
        response = _login(client, next_value="/settings")
        assert response.status_code == 302
        assert response.headers["Location"] == "/settings"

    def test_absolute_url_falls_back_to_index(self, client, settings_file):
        response = _login(client, next_value="https://evil.example/phish")
        assert response.status_code == 302
        assert response.headers["Location"] == "/"

    def test_protocol_relative_url_falls_back_to_index(self, client, settings_file):
        response = _login(client, next_value="//evil.example")
        assert response.status_code == 302
        assert response.headers["Location"] == "/"

    def test_backslash_path_falls_back_to_index(self, client, settings_file):
        # Browsers treat '/\' like '//', so backslashes are rejected outright.
        response = _login(client, next_value="/\\evil.example")
        assert response.status_code == 302
        assert response.headers["Location"] == "/"

    def test_missing_next_goes_to_index(self, client, settings_file):
        response = _login(client)
        assert response.status_code == 302
        assert response.headers["Location"] == "/"

    def test_invalid_credentials_do_not_redirect(self, client, settings_file):
        response = _login(client, next_value="/settings", password="wrong")
        assert response.status_code == 200
        assert b"Invalid credentials" in response.data


@pytest.fixture
def completed_setup(settings_file):
    """Puts the app in the fully-set-up state login_required checks for:
    password changed (settings flag) and Audible auth done (flag file)."""
    from audible_downloader import SETUP_FLAG_FILE

    settings_file.write_text(json.dumps({"initial_setup_complete": True}))
    with open(SETUP_FLAG_FILE, "w"):
        pass
    yield
    os.remove(SETUP_FLAG_FILE)


class TestOriginValidation:
    """M1 regression: cross-origin write requests must be rejected, while
    same-origin and Origin-less (curl-style) requests pass through."""

    def test_cross_origin_post_is_blocked(self, client, settings_file):
        response = client.post("/login", headers={"Origin": "https://evil.example"})
        assert response.status_code == 403

    def test_null_origin_post_is_blocked(self, client, settings_file):
        response = client.post("/login", headers={"Origin": "null"})
        assert response.status_code == 403

    def test_same_origin_post_is_allowed(self, client, settings_file):
        # The test client's requests go to host "localhost"; a matching Origin
        # must pass even when the scheme differs (HTTPS-terminating proxy).
        response = client.post(
            "/login",
            data={"username": "admin", "password": "changeme"},
            headers={"Origin": "https://localhost"},
        )
        assert response.status_code == 302

    def test_origin_less_post_is_allowed(self, client, settings_file):
        response = client.post("/login", data={"username": "admin", "password": "wrong"})
        assert response.status_code == 200

    def test_cross_origin_get_is_allowed(self, client, settings_file):
        # Origin checks only apply to state-changing methods.
        response = client.get("/login", headers={"Origin": "https://evil.example"})
        assert response.status_code == 200


class TestAuthenticatedAccess:
    def test_dashboard_renders_for_logged_in_user(self, client, completed_setup):
        with client.session_transaction() as session:
            session["username"] = "admin"
        response = client.get("/")
        assert response.status_code == 200

    def test_anonymous_user_is_redirected_to_login(self, client, completed_setup):
        response = client.get("/")
        assert response.status_code == 302
        assert response.headers["Location"].startswith("/login")


class TestCoverAuthentication:
    """L2 regression: cover art must not be served to anonymous clients."""

    def test_anonymous_cover_request_is_redirected_to_login(self, client, completed_setup):
        response = client.get("/covers/B00XYZ_thumb.jpg")
        assert response.status_code == 302
        assert response.headers["Location"].startswith("/login")

    def test_logged_in_cover_request_reaches_the_file_handler(self, client, completed_setup):
        with client.session_transaction() as session:
            session["username"] = "admin"
        # The cover doesn't exist in the temp COVERS_DIR, so a 404 (rather
        # than a login redirect) proves the request passed authentication.
        response = client.get("/covers/B00XYZ_thumb.jpg")
        assert response.status_code == 404


@pytest.fixture
def book_db(tmp_path, monkeypatch):
    """A temp library.db with one book, including the custom-metadata columns."""
    from audible_downloader import db as db_module

    db_path = tmp_path / "library.db"
    con = sqlite3.connect(db_path)
    con.execute(
        "CREATE TABLE audiobooks ("
        "asin TEXT PRIMARY KEY, title TEXT, author TEXT, status TEXT, "
        "custom_title TEXT, custom_author TEXT, "
        "is_summary_full INTEGER DEFAULT 0, is_duplicate INTEGER DEFAULT 0)"
    )
    con.execute(
        "INSERT INTO audiobooks (asin, title, author, status) VALUES (?, ?, ?, ?)",
        ("B001", "Native Title", "Native Author", "DOWNLOADED"),
    )
    con.commit()
    con.close()
    monkeypatch.setattr(db_module, "DB_FILE", str(db_path))
    return db_path


def _login_session(client):
    with client.session_transaction() as session:
        session["username"] = "admin"


class TestUpdateBookMetadata:
    """Phase 5.3: POST /api/book/<asin>/update persists custom title/author,
    login- and Origin-gated, without renaming files."""

    def test_requires_login(self, client, completed_setup, book_db):
        response = client.post("/api/book/B001/update", json={"custom_title": "X"})
        assert response.status_code == 302
        assert response.headers["Location"].startswith("/login")

    def test_cross_origin_is_blocked(self, client, completed_setup, book_db):
        _login_session(client)
        response = client.post(
            "/api/book/B001/update", json={"custom_title": "X"}, headers={"Origin": "https://evil.example"}
        )
        assert response.status_code == 403

    def test_sets_custom_title_and_author(self, client, completed_setup, book_db):
        _login_session(client)
        response = client.post("/api/book/B001/update", json={"custom_title": "Nicer", "custom_author": "Better"})
        assert response.status_code == 200
        data = response.get_json()
        assert data["title"] == "Nicer"
        assert data["author"] == "Better"
        assert data["native_title"] == "Native Title"
        assert data["native_author"] == "Native Author"

    def test_empty_value_clears_the_override(self, client, completed_setup, book_db):
        _login_session(client)
        client.post("/api/book/B001/update", json={"custom_title": "Nicer"})
        response = client.post("/api/book/B001/update", json={"custom_title": ""})
        data = response.get_json()
        assert data["custom_title"] is None
        assert data["title"] == "Native Title"  # reverts to native

    def test_absent_field_is_preserved(self, client, completed_setup, book_db):
        _login_session(client)
        client.post("/api/book/B001/update", json={"custom_title": "Keep", "custom_author": "AuthKeep"})
        response = client.post("/api/book/B001/update", json={"custom_author": "NewAuth"})
        data = response.get_json()
        assert data["custom_title"] == "Keep"  # untouched — key absent
        assert data["custom_author"] == "NewAuth"

    def test_whitespace_is_trimmed(self, client, completed_setup, book_db):
        _login_session(client)
        response = client.post("/api/book/B001/update", json={"custom_title": "  Padded  "})
        assert response.get_json()["custom_title"] == "Padded"

    def test_unknown_book_returns_404(self, client, completed_setup, book_db):
        _login_session(client)
        response = client.post("/api/book/NOPE/update", json={"custom_title": "X"})
        assert response.status_code == 404

    def test_no_editable_fields_returns_400(self, client, completed_setup, book_db):
        _login_session(client)
        response = client.post("/api/book/B001/update", json={"foo": "bar"})
        assert response.status_code == 400

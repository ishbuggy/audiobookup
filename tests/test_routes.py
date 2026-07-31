# tests/test_routes.py

import json
import os
import sqlite3
from io import BytesIO
from unittest import mock
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


class TestConversionRate:
    """Phase 6 / FR13: GET /api/conversion_rate exposes the estimator's learned
    sec/min rate for the large-bulk download warning."""

    def test_requires_login(self, client, completed_setup):
        response = client.get("/api/conversion_rate")
        assert response.status_code == 302
        assert response.headers["Location"].startswith("/login")

    def test_returns_rate_for_logged_in_user(self, client, completed_setup):
        with client.session_transaction() as session:
            session["username"] = "admin"
        response = client.get("/api/conversion_rate")
        assert response.status_code == 200
        data = response.get_json()
        # With no conversion history the estimator returns its default guess.
        assert data["sec_per_min"] > 0


@pytest.fixture
def book_db(tmp_path, monkeypatch):
    """A temp library.db with one book, including the custom-metadata columns."""
    from audible_downloader import db as db_module

    db_path = tmp_path / "library.db"
    con = sqlite3.connect(db_path)
    con.execute(
        "CREATE TABLE audiobooks ("
        "asin TEXT PRIMARY KEY, title TEXT, author TEXT, status TEXT, "
        "custom_title TEXT, custom_author TEXT, custom_cover INTEGER DEFAULT 0, "
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

    def test_response_reports_is_duplicate(self, client, completed_setup, book_db):
        _login_session(client)
        response = client.post("/api/book/B001/update", json={"custom_title": "X"})
        # A normal edit leaves the (default-0) flag untouched but reports it.
        assert response.get_json()["is_duplicate"] == 0


class TestResolveDuplicate:
    """Phase 5.2: an explicit resolve_duplicate flag clears is_duplicate via the
    same update endpoint; a normal edit never touches the flag."""

    def _set_duplicate(self, book_db):
        con = sqlite3.connect(book_db)
        con.execute("UPDATE audiobooks SET is_duplicate = 1 WHERE asin = 'B001'")
        con.commit()
        con.close()

    def _flag_in_db(self, book_db):
        con = sqlite3.connect(book_db)
        value = con.execute("SELECT is_duplicate FROM audiobooks WHERE asin = 'B001'").fetchone()[0]
        con.close()
        return value

    def test_resolve_with_title_clears_flag_and_sets_override(self, client, completed_setup, book_db):
        _login_session(client)
        self._set_duplicate(book_db)
        response = client.post(
            "/api/book/B001/update",
            json={"custom_title": "Native Title (Narrated by X)", "resolve_duplicate": True},
        )
        data = response.get_json()
        assert data["is_duplicate"] == 0
        assert data["title"] == "Native Title (Narrated by X)"
        assert self._flag_in_db(book_db) == 0

    def test_resolve_alone_clears_flag_without_changing_title(self, client, completed_setup, book_db):
        _login_session(client)
        self._set_duplicate(book_db)
        response = client.post("/api/book/B001/update", json={"resolve_duplicate": True})
        data = response.get_json()
        assert data["is_duplicate"] == 0
        assert data["title"] == "Native Title"  # unchanged — "keep ASIN suffix"
        assert self._flag_in_db(book_db) == 0

    def test_normal_edit_leaves_flag_set(self, client, completed_setup, book_db):
        _login_session(client)
        self._set_duplicate(book_db)
        response = client.post("/api/book/B001/update", json={"custom_title": "Nicer"})
        assert response.get_json()["is_duplicate"] == 1
        assert self._flag_in_db(book_db) == 1

    def test_resolve_false_is_ignored(self, client, completed_setup, book_db):
        _login_session(client)
        self._set_duplicate(book_db)
        # Only a literal True clears the flag; a falsey value must not.
        response = client.post("/api/book/B001/update", json={"custom_title": "Y", "resolve_duplicate": False})
        assert response.get_json()["is_duplicate"] == 1
        assert self._flag_in_db(book_db) == 1


class TestUploadBookCover:
    """Phase 5.4: POST /api/book/<asin>/cover replaces the cover, login- and
    Origin-gated, normalizing the upload to JPEG and marking custom_cover."""

    def _upload(self, client, asin="B001", filename="cover.jpg", content=b"\xff\xd8fakejpeg", **kwargs):
        return client.post(
            f"/api/book/{asin}/cover",
            data={"cover": (BytesIO(content), filename)},
            content_type="multipart/form-data",
            **kwargs,
        )

    def _mock_ffmpeg(self, monkeypatch, returncode):
        result = mock.MagicMock(returncode=returncode, stderr="err")
        monkeypatch.setattr("audible_downloader.routes.subprocess.run", lambda *a, **k: result)

    def test_requires_login(self, client, completed_setup, book_db):
        response = self._upload(client)
        assert response.status_code == 302

    def test_unknown_book_returns_404(self, client, completed_setup, book_db):
        _login_session(client)
        response = self._upload(client, asin="NOPE")
        assert response.status_code == 404

    def test_missing_file_returns_400(self, client, completed_setup, book_db):
        _login_session(client)
        response = client.post("/api/book/B001/cover", data={}, content_type="multipart/form-data")
        assert response.status_code == 400

    def test_unsupported_extension_returns_400(self, client, completed_setup, book_db):
        _login_session(client)
        response = self._upload(client, filename="cover.txt")
        assert response.status_code == 400

    def test_oversize_returns_413(self, client, completed_setup, book_db, monkeypatch):
        _login_session(client)
        monkeypatch.setattr("audible_downloader.routes.MAX_COVER_BYTES", 10)
        response = self._upload(client, content=b"x" * 50)
        assert response.status_code == 413

    def test_successful_upload_sets_custom_cover(self, client, completed_setup, book_db, monkeypatch):
        _login_session(client)
        self._mock_ffmpeg(monkeypatch, returncode=0)
        response = self._upload(client)
        assert response.status_code == 200
        assert response.get_json()["cover_url_thumb"] == "/covers/B001_thumb.jpg"
        con = sqlite3.connect(str(book_db))
        flag = con.execute("SELECT custom_cover FROM audiobooks WHERE asin = 'B001'").fetchone()[0]
        con.close()
        assert flag == 1

    def test_ffmpeg_failure_returns_400(self, client, completed_setup, book_db, monkeypatch):
        _login_session(client)
        self._mock_ffmpeg(monkeypatch, returncode=1)
        response = self._upload(client)
        assert response.status_code == 400

    def test_second_pass_failure_leaves_existing_covers_untouched(self, client, completed_setup, book_db, monkeypatch):
        # If the thumbnail pass fails after the full-cover pass succeeded, the old
        # covers must be preserved and custom_cover must NOT be set — no partial,
        # inconsistent update.
        from audible_downloader.routes import COVERS_DIR

        os.makedirs(COVERS_DIR, exist_ok=True)
        original = os.path.join(COVERS_DIR, "B001_original.jpg")
        thumb = os.path.join(COVERS_DIR, "B001_thumb.jpg")
        for p in (original, thumb):
            with open(p, "wb") as f:
                f.write(b"OLD")

        _login_session(client)
        # ffmpeg writes its output file and succeeds on the first call, fails on
        # the second (thumbnail) call.
        calls = {"n": 0}

        def fake_run(command, *args, **kwargs):
            calls["n"] += 1
            out_path = command[-1]
            if calls["n"] == 1:
                with open(out_path, "wb") as f:
                    f.write(b"NEW")
                return mock.MagicMock(returncode=0, stderr="")
            return mock.MagicMock(returncode=1, stderr="boom")

        monkeypatch.setattr("audible_downloader.routes.subprocess.run", fake_run)
        response = self._upload(client)
        assert response.status_code == 400
        # Old covers intact, no half-written staging file left behind.
        assert open(original, "rb").read() == b"OLD"
        assert open(thumb, "rb").read() == b"OLD"
        # No half-written staging temp left behind in the covers dir.
        assert not [f for f in os.listdir(COVERS_DIR) if f.startswith("tmp")]
        con = sqlite3.connect(str(book_db))
        flag = con.execute("SELECT custom_cover FROM audiobooks WHERE asin = 'B001'").fetchone()[0]
        con.close()
        assert flag == 0

    def test_ffmpeg_command_is_hardened_against_ssrf(self, client, completed_setup, book_db, monkeypatch):
        # ffmpeg detects format by content, so the input must be pinned to the
        # image demuxer with protocols restricted to local files, or a crafted
        # upload could be read as a concat/hls playlist (SSRF / local file read).
        _login_session(client)
        calls = []

        def fake_run(command, *args, **kwargs):
            calls.append(command)
            return mock.MagicMock(returncode=0, stderr="")

        monkeypatch.setattr("audible_downloader.routes.subprocess.run", fake_run)
        self._upload(client)
        assert calls, "ffmpeg should have been invoked"
        for command in calls:
            assert "-nostdin" in command
            assert command[command.index("-f") + 1] == "image2"
            assert command[command.index("-protocol_whitelist") + 1] == "file"
            # The whitelist/format flags must precede the input they guard.
            assert command.index("-f") < command.index("-i")


@pytest.fixture
def import_env(tmp_path, monkeypatch):
    """Point the import module's DATA_DIR (and thus the staging dir) at a temp
    directory so upload streaming never touches the real /data volume."""
    from audible_downloader import import_logic

    data_dir = tmp_path / "data"
    data_dir.mkdir()
    monkeypatch.setattr(import_logic, "DATA_DIR", str(data_dir))
    return data_dir


class TestImportUpload:
    """Phase 6 (FR2): POST /api/library/import/upload streams a file into /data
    and adopts it, login- and Origin-gated, with type + size validation."""

    def _upload(self, client, *, filename="Book.m4b", content=b"AUDIODATA"):
        return client.post(
            f"/api/library/import/upload?filename={quote(filename)}",
            data=content,
            content_type="application/octet-stream",
        )

    def test_requires_login(self, client, completed_setup, import_env):
        response = self._upload(client)
        assert response.status_code == 302  # redirected to login

    def test_cross_origin_is_blocked(self, client, completed_setup, import_env):
        _login_session(client)
        response = client.post(
            "/api/library/import/upload?filename=Book.m4b",
            data=b"x",
            content_type="application/octet-stream",
            headers={"Origin": "https://evil.example"},
        )
        assert response.status_code == 403

    def test_unsupported_extension_returns_400(self, client, completed_setup, import_env):
        _login_session(client)
        response = self._upload(client, filename="song.mp3")
        assert response.status_code == 400

    def test_missing_filename_returns_400(self, client, completed_setup, import_env):
        _login_session(client)
        response = client.post("/api/library/import/upload", data=b"x", content_type="application/octet-stream")
        assert response.status_code == 400

    def test_empty_body_returns_400(self, client, completed_setup, import_env):
        _login_session(client)
        response = self._upload(client, content=b"")
        assert response.status_code == 400

    def test_oversize_returns_413(self, client, completed_setup, settings_file, import_env):
        _login_session(client)
        # A zero-GB cap makes any non-empty body exceed the limit.
        settings_file.write_text(json.dumps({"initial_setup_complete": True, "import": {"max_upload_gb": 0}}))
        response = self._upload(client, content=b"x" * 100)
        assert response.status_code == 413

    def test_successful_upload_adopts_and_returns_metadata(self, client, completed_setup, import_env, monkeypatch):
        _login_session(client)
        captured = {}

        def fake_adopt_upload(staging_path, filename, settings):
            captured["staging_exists"] = os.path.exists(staging_path)
            captured["filename"] = filename
            return {
                "action": "imported",
                "key": "IMPORT-deadbeef",
                "title": "Book",
                "author": "Unknown Author",
                "filepath": "/data/Unknown Author/Book.m4b",
            }

        monkeypatch.setattr("audible_downloader.routes.adopt_upload", fake_adopt_upload)
        response = self._upload(client)
        assert response.status_code == 200
        data = response.get_json()
        assert data["success"] is True
        assert data["action"] == "imported"
        assert data["asin"] == "IMPORT-deadbeef"
        assert captured["staging_exists"] is True  # the body was streamed to disk before adoption
        assert captured["filename"] == "Book.m4b"

    def test_unreadable_media_returns_400_and_cleans_up(self, client, completed_setup, import_env, monkeypatch):
        # WF#6: adopt_upload rejects renamed junk (non-empty, importable ext, no
        # media) with the "unreadable-media" skip *before* placing it; the endpoint
        # surfaces a 400 and removes the leftover staging file rather than reporting
        # success for a book that was never adopted.
        _login_session(client)

        def reject_unreadable(staging_path, filename, settings):
            return {"action": "skipped", "reason": "unreadable-media", "key": None, "filepath": None}

        monkeypatch.setattr("audible_downloader.routes.adopt_upload", reject_unreadable)
        response = self._upload(client)
        assert response.status_code == 400
        assert response.get_json().get("success") is not True
        staging = import_env / ".import_staging"
        leftovers = list(staging.iterdir()) if staging.exists() else []
        assert leftovers == []  # staging file cleaned up

    def test_adoption_failure_returns_500_and_cleans_up(self, client, completed_setup, import_env, monkeypatch):
        _login_session(client)

        def boom(*a, **k):
            raise RuntimeError("probe blew up")

        monkeypatch.setattr("audible_downloader.routes.adopt_upload", boom)
        response = self._upload(client)
        assert response.status_code == 500
        # Staging dir should hold no leftover file after the failure cleanup.
        staging = import_env / ".import_staging"
        leftovers = list(staging.iterdir()) if staging.exists() else []
        assert leftovers == []


@pytest.fixture
def jobs_db(tmp_path, monkeypatch):
    """A temp library.db with jobs/job_items rows spanning active and finished
    states at known ages, for exercising POST /api/jobs/clear."""
    from datetime import datetime, timedelta, timezone

    from audible_downloader import db as db_module

    def ago(days):
        return (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()

    db_path = tmp_path / "library.db"
    con = sqlite3.connect(db_path)
    con.execute(
        "CREATE TABLE jobs (job_id INTEGER PRIMARY KEY AUTOINCREMENT, job_type TEXT, status TEXT, "
        "start_time TEXT, end_time TEXT, job_params TEXT)"
    )
    con.execute(
        "CREATE TABLE job_items (item_id INTEGER PRIMARY KEY AUTOINCREMENT, job_id INTEGER, asin TEXT, "
        "status TEXT, log TEXT)"
    )
    rows = [
        (1, "DOWNLOAD", "RUNNING", ago(0), None),  # active
        (2, "SYNC", "QUEUED", ago(0), None),  # active/pending
        (3, "DOWNLOAD", "COMPLETED", ago(61), ago(60)),  # old finished
        (4, "DOWNLOAD", "FAILED", ago(2), ago(1)),  # recent finished
        (5, "VERIFY", "CANCELLED", ago(101), ago(100)),  # old finished
    ]
    con.executemany("INSERT INTO jobs (job_id, job_type, status, start_time, end_time) VALUES (?, ?, ?, ?, ?)", rows)
    # Items attached to the active job (1) and two finished jobs (3, 4).
    con.executemany(
        "INSERT INTO job_items (job_id, asin, status) VALUES (?, ?, ?)",
        [(1, "B001", "PROCESSING"), (3, "B003", "COMPLETED"), (4, "B004", "FAILED")],
    )
    con.commit()
    con.close()
    monkeypatch.setattr(db_module, "DB_FILE", str(db_path))
    return db_path


def _job_ids(db):
    con = sqlite3.connect(db)
    ids = {r[0] for r in con.execute("SELECT job_id FROM jobs")}
    con.close()
    return ids


def _item_job_ids(db):
    con = sqlite3.connect(db)
    ids = {r[0] for r in con.execute("SELECT job_id FROM job_items")}
    con.close()
    return ids


class TestClearJobs:
    """Phase 7 (FR10): POST /api/jobs/clear removes finished jobs and their
    items, login- and Origin-gated, never touching an active job."""

    def test_requires_login(self, client, completed_setup, jobs_db):
        response = client.post("/api/jobs/clear", json={"mode": "all"})
        assert response.status_code == 302

    def test_cross_origin_is_blocked(self, client, completed_setup, jobs_db):
        _login_session(client)
        response = client.post("/api/jobs/clear", json={"mode": "all"}, headers={"Origin": "https://evil.example"})
        assert response.status_code == 403

    def test_mode_all_deletes_finished_keeps_active(self, client, completed_setup, jobs_db):
        _login_session(client)
        response = client.post("/api/jobs/clear", json={"mode": "all"})
        assert response.status_code == 200
        data = response.get_json()
        assert data["deleted_jobs"] == 3  # jobs 3, 4, 5
        assert data["deleted_items"] == 2  # items on jobs 3, 4 (not the active job 1)
        assert _job_ids(jobs_db) == {1, 2}  # only the active/queued jobs survive
        assert _item_job_ids(jobs_db) == {1}  # active job's item is untouched

    def test_default_mode_is_all(self, client, completed_setup, jobs_db):
        _login_session(client)
        response = client.post("/api/jobs/clear", json={})
        assert response.status_code == 200
        assert _job_ids(jobs_db) == {1, 2}

    def test_older_than_deletes_only_aged_finished(self, client, completed_setup, jobs_db):
        _login_session(client)
        response = client.post("/api/jobs/clear", json={"mode": "older_than", "days": 30})
        assert response.status_code == 200
        assert response.get_json()["deleted_jobs"] == 2  # jobs 3 (60d) and 5 (100d)
        # Recent finished job 4 (1d) and both active jobs remain.
        assert _job_ids(jobs_db) == {1, 2, 4}
        assert _item_job_ids(jobs_db) == {1, 4}

    def test_never_deletes_running_or_queued(self, client, completed_setup, jobs_db):
        _login_session(client)
        # Even with an aggressive age, active jobs must survive.
        client.post("/api/jobs/clear", json={"mode": "older_than", "days": 1})
        surviving = _job_ids(jobs_db)
        assert 1 in surviving and 2 in surviving

    def test_invalid_mode_returns_400(self, client, completed_setup, jobs_db):
        _login_session(client)
        response = client.post("/api/jobs/clear", json={"mode": "everything"})
        assert response.status_code == 400
        assert _job_ids(jobs_db) == {1, 2, 3, 4, 5}  # nothing deleted

    # The huge value (10**18) would overflow timedelta(days=...) and raise an
    # unhandled 500 before the bound was added; it must be a clean 400.
    @pytest.mark.parametrize("days", [0, -5, "30", 1.5, True, None, 10**18, 36501])
    def test_older_than_requires_positive_int_days(self, client, completed_setup, jobs_db, days):
        _login_session(client)
        response = client.post("/api/jobs/clear", json={"mode": "older_than", "days": days})
        assert response.status_code == 400
        assert _job_ids(jobs_db) == {1, 2, 3, 4, 5}  # nothing deleted

    def test_clearing_when_nothing_finished_returns_zero(self, client, completed_setup, jobs_db):
        _login_session(client)
        client.post("/api/jobs/clear", json={"mode": "all"})  # clear the finished jobs
        response = client.post("/api/jobs/clear", json={"mode": "all"})  # now none remain
        assert response.status_code == 200
        data = response.get_json()
        assert data == {"success": True, "deleted_jobs": 0, "deleted_items": 0}


class TestSettingsGetRedaction:
    """GET /api/settings is what the settings page's "Export as JSON" button
    downloads, and users share that file to back up or migrate an install — so
    the response must not carry the web-UI password hash (offline-crackable) or
    the first-run setup flag."""

    def test_response_omits_credential_and_setup_keys(self, client, completed_setup):
        _login_session(client)
        response = client.get("/api/settings")
        assert response.status_code == 200
        settings = response.get_json()
        assert "password_hash" not in settings
        assert "initial_setup_complete" not in settings

    def test_response_still_carries_the_real_settings(self, client, completed_setup):
        # Redaction must be surgical: everything the UI actually reads survives.
        _login_session(client)
        settings = client.get("/api/settings").get_json()
        assert settings["username"] == "admin"
        for key in ("advanced_mode_enabled", "job", "naming", "conversion", "import", "tasks"):
            assert key in settings

    def test_redaction_does_not_mutate_the_stored_settings(self, client, completed_setup, settings_file):
        """The handler must filter a copy, never pop the loaded dict — otherwise
        a single export would strip the credential from live in-memory settings
        (and from the next save, locking the user out)."""
        from audible_downloader import settings as settings_module

        # A recognizable stand-in for the stored hash (not a real credential).
        sentinel_hash = "pbkdf2:sha256:600000$testsalt$0123456789abcdef"
        settings_file.write_text(json.dumps({"initial_setup_complete": True, "password_hash": sentinel_hash}))

        _login_session(client)
        response = client.get("/api/settings")
        assert response.status_code == 200
        assert "password_hash" not in response.get_json()

        # The store behind the endpoint is untouched by the redaction.
        stored = settings_module.load_settings()
        assert stored["password_hash"] == sentinel_hash
        assert stored["initial_setup_complete"] is True

    def test_the_shared_defaults_are_not_stripped(self, client, completed_setup):
        """completed_setup writes a minimal settings.json, so both redacted keys
        come from DEFAULT_SETTINGS via the load-time merge — that module-level
        dict is shared process-wide and must survive an export untouched."""
        from audible_downloader import settings as settings_module

        _login_session(client)
        assert client.get("/api/settings").status_code == 200
        assert "password_hash" in settings_module.DEFAULT_SETTINGS
        assert "initial_setup_complete" in settings_module.DEFAULT_SETTINGS


class TestSettingsOutputFormatMirror:
    """Phase 0: POST /api/settings mirrors the legacy no_reencode flag from the
    new output_format enum so code still reading the old flag stays correct."""

    def test_original_sets_no_reencode_true(self, client, completed_setup):
        _login_session(client)
        response = client.post("/api/settings", json={"conversion": {"output_format": "original"}})
        assert response.status_code == 200
        settings = client.get("/api/settings").get_json()
        assert settings["conversion"]["output_format"] == "original"
        assert settings["conversion"]["no_reencode"] is True

    def test_m4b_sets_no_reencode_false(self, client, completed_setup):
        _login_session(client)
        # Flip to original first, then back to m4b, proving the mirror updates.
        client.post("/api/settings", json={"conversion": {"output_format": "original"}})
        response = client.post("/api/settings", json={"conversion": {"output_format": "m4b"}})
        assert response.status_code == 200
        settings = client.get("/api/settings").get_json()
        assert settings["conversion"]["no_reencode"] is False

    def test_mp3_sets_no_reencode_false(self, client, completed_setup):
        _login_session(client)
        client.post("/api/settings", json={"conversion": {"output_format": "original"}})
        response = client.post("/api/settings", json={"conversion": {"output_format": "mp3"}})
        assert response.status_code == 200
        settings = client.get("/api/settings").get_json()
        assert settings["conversion"]["output_format"] == "mp3"
        assert settings["conversion"]["no_reencode"] is False


class TestSettingsImportLegacyPayload:
    """Importing a pre-v0.22 settings export POSTs the raw file here: it carries
    no_reencode and no output_format, so the payload must be normalized before
    the merge or the user's lossless preference is silently discarded."""

    def test_legacy_no_reencode_true_imports_as_original(self, client, completed_setup):
        _login_session(client)
        response = client.post("/api/settings", json={"conversion": {"no_reencode": True}})
        assert response.status_code == 200
        settings = client.get("/api/settings").get_json()
        assert settings["conversion"]["output_format"] == "original"
        assert settings["conversion"]["no_reencode"] is True

    def test_legacy_no_reencode_false_imports_as_m4b(self, client, completed_setup):
        _login_session(client)
        # Start from "original" so a stuck value would be visible in the result.
        client.post("/api/settings", json={"conversion": {"output_format": "original"}})
        response = client.post("/api/settings", json={"conversion": {"no_reencode": False}})
        assert response.status_code == 200
        settings = client.get("/api/settings").get_json()
        assert settings["conversion"]["output_format"] == "m4b"
        assert settings["conversion"]["no_reencode"] is False

    def test_payload_carrying_both_keeps_the_explicit_output_format(self, client, completed_setup):
        _login_session(client)
        response = client.post("/api/settings", json={"conversion": {"output_format": "mp3", "no_reencode": True}})
        assert response.status_code == 200
        settings = client.get("/api/settings").get_json()
        assert settings["conversion"]["output_format"] == "mp3"
        assert settings["conversion"]["no_reencode"] is False


@pytest.fixture
def fake_core_count(monkeypatch):
    """Makes GET /api/get_cpu_cores see an arbitrary host core count.

    The route checks the cgroup quota files first and only falls back to
    os.cpu_count(), so the fixture hides the cgroup paths (real ones exist on a
    Linux test host) while leaving every other os.path.exists() call alone —
    @login_required uses it to check the setup flag file."""
    real_exists = os.path.exists

    def _pretend(cores):
        monkeypatch.setattr(
            os.path,
            "exists",
            lambda path: False if str(path).startswith("/sys/fs/cgroup") else real_exists(path),
        )
        monkeypatch.setattr(os, "cpu_count", lambda: cores)

    return _pretend


class TestCpuCoreRecommendation:
    """Backlog #12 regression: the auto-detect button writes this endpoint's
    recommendation straight into a number input the UI caps at 16, and nothing
    clamps it on save — so the suggestion itself must stay inside that range."""

    def test_many_core_host_is_capped_at_the_ui_maximum(self, client, completed_setup, fake_core_count):
        _login_session(client)
        fake_core_count(24)
        response = client.get("/api/get_cpu_cores")
        assert response.status_code == 200
        data = response.get_json()
        # The true core count is still reported for display; only the
        # recommendation the input receives is clamped.
        assert data["total_cores"] == 24
        assert data["recommended_concurrency"] == 16

    def test_low_core_host_still_recommends_one_less(self, client, completed_setup, fake_core_count):
        _login_session(client)
        fake_core_count(2)
        response = client.get("/api/get_cpu_cores")
        assert response.status_code == 200
        data = response.get_json()
        assert data["total_cores"] == 2
        assert data["recommended_concurrency"] == 1


class TestStartDownloadJobParams:
    """v0.23.0 #2 (D5): the stale-file cleanup answer is a destructive consent that
    has to survive the whole chain, so the route forwards job_params verbatim —
    and normalizes a malformed one instead of letting the worker thread raise
    after the job row already exists."""

    @pytest.fixture
    def captured_start(self, monkeypatch):
        """Replace start_new_job with a recorder, so no thread or DB is involved."""
        calls = []

        def fake_start_new_job(job_type, asins=None, job_params=None):
            calls.append({"job_type": job_type, "asins": asins, "job_params": job_params})
            return True, {"success": True, "job_id": 42}

        monkeypatch.setattr("audible_downloader.routes.start_new_job", fake_start_new_job)
        return calls

    def test_declined_prompt_is_forwarded_as_an_explicit_false(self, client, completed_setup, captured_start):
        # The unticked checkbox must reach the job manager as False, not vanish —
        # that False is what vetoes the saved setting.
        _login_session(client)
        response = client.post(
            "/api/jobs/start",
            json={"job_type": "DOWNLOAD", "asins": ["B001"], "job_params": {"cleanup_stale_files": False}},
        )
        assert response.status_code == 200
        assert captured_start[0]["asins"] == ["B001"]
        assert captured_start[0]["job_params"] == {"cleanup_stale_files": False}

    def test_accepted_prompt_is_forwarded_as_true(self, client, completed_setup, captured_start):
        _login_session(client)
        response = client.post(
            "/api/jobs/start",
            json={"job_type": "DOWNLOAD", "asins": ["B001"], "job_params": {"cleanup_stale_files": True}},
        )
        assert response.status_code == 200
        assert captured_start[0]["job_params"] == {"cleanup_stale_files": True}

    def test_absent_job_params_forwards_an_empty_dict(self, client, completed_setup, captured_start):
        # Bulk and card downloads send no params at all; the setting governs those.
        _login_session(client)
        response = client.post("/api/jobs/start", json={"job_type": "DOWNLOAD", "asins": ["B001"]})
        assert response.status_code == 200
        assert captured_start[0]["job_params"] == {}

    def test_malformed_job_params_are_normalized(self, client, completed_setup, captured_start):
        # A string body would become ("yes").get(...) inside the worker thread.
        _login_session(client)
        response = client.post(
            "/api/jobs/start",
            json={"job_type": "DOWNLOAD", "asins": ["B001"], "job_params": "yes"},
        )
        assert response.status_code == 200
        assert captured_start[0]["job_params"] == {}


class TestDownloadBookAnnotations:
    """v0.23.0 Phase 6: POST /api/book/<asin>/annotations saves a downloaded
    book's clips/notes/bookmarks beside its audio file. Synchronous, and the
    "this title has no annotations" answer is a success, not an error."""

    @pytest.fixture
    def annotated_db(self, tmp_path, monkeypatch):
        """A temp library.db whose one DOWNLOADED book has a real file on disk."""
        from audible_downloader import db as db_module

        book_file = tmp_path / "library" / "Dracula.m4b"
        book_file.parent.mkdir()
        book_file.write_bytes(b"audio")

        db_path = tmp_path / "library.db"
        con = sqlite3.connect(db_path)
        con.execute(
            "CREATE TABLE audiobooks (asin TEXT PRIMARY KEY, title TEXT, status TEXT, filepath TEXT, "
            "release_date TEXT, purchase_date TEXT)"
        )
        # The dates carry the conversion.file_timestamp_source stamping (#26):
        # sync stores its "N/A" placeholder when Audible omits a field.
        con.execute(
            "INSERT INTO audiobooks (asin, title, status, filepath, release_date, purchase_date) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            ("B001", "Dracula", "DOWNLOADED", str(book_file), "2019-06-27", "2023-04-05T06:07:08.000Z"),
        )
        # A book that was never downloaded: no path to place a sidecar beside.
        con.execute(
            "INSERT INTO audiobooks (asin, title, status, filepath) VALUES (?, ?, ?, ?)",
            ("B002", "Not Yet", "NEW", None),
        )
        # A DOWNLOADED row whose file has since disappeared from disk.
        con.execute(
            "INSERT INTO audiobooks (asin, title, status, filepath) VALUES (?, ?, ?, ?)",
            ("B003", "Ghost", "DOWNLOADED", str(tmp_path / "library" / "Ghost.m4b")),
        )
        # v0.24.0: a book split into per-chapter files tracks its FOLDER, and its
        # sidecars keep the single-file-equivalent name INSIDE that folder (D9).
        con.execute("CREATE TABLE book_files (asin TEXT, part_index INTEGER, filepath TEXT)")
        split_dir = tmp_path / "library" / "Dracula"
        split_dir.mkdir()
        parts = [split_dir / "Dracula - 1 - One.m4b", split_dir / "Dracula - 2 - Two.m4b"]
        for part in parts:
            part.write_bytes(b"audio")
        (split_dir / "Bram Stoker - Dracula.jpg").write_bytes(b"cover")
        con.execute(
            "INSERT INTO audiobooks (asin, title, status, filepath) VALUES (?, ?, ?, ?)",
            ("B004", "Dracula", "DOWNLOADED", str(split_dir)),
        )
        con.executemany(
            "INSERT INTO book_files (asin, part_index, filepath) VALUES (?, ?, ?)",
            [("B004", index, str(part)) for index, part in enumerate(parts)],
        )
        con.commit()
        con.close()
        monkeypatch.setattr(db_module, "DB_FILE", str(db_path))
        return book_file

    def _fake_run(self, *, writes_file, returncode=0):
        """Stand in for the audible-cli call, optionally writing the dump that a
        title with annotations produces (named after the title, not the ASIN)."""
        import subprocess as sp

        def runner(cmd, **kwargs):
            out_dir = cmd[cmd.index("-o") + 1]
            if writes_file:
                with open(os.path.join(out_dir, "Dracula-annotations.json"), "w", encoding="utf-8") as f:
                    f.write('{"payload": {"records": [{"type": "audible.clip"}]}}')
            return sp.CompletedProcess(cmd, returncode, "", "")

        return runner

    def test_requires_login(self, client, completed_setup, annotated_db):
        response = client.post("/api/book/B001/annotations")
        assert response.status_code == 302
        assert response.headers["Location"].startswith("/login")

    def test_cross_origin_is_blocked(self, client, completed_setup, annotated_db):
        _login_session(client)
        response = client.post("/api/book/B001/annotations", headers={"Origin": "https://evil.example"})
        assert response.status_code == 403

    def test_dump_is_moved_next_to_the_audiobook(self, client, completed_setup, annotated_db):
        from audible_downloader import DATABASE_DIR

        _login_session(client)
        with mock.patch(
            "audible_downloader.routes.subprocess.run", side_effect=self._fake_run(writes_file=True)
        ) as run:
            response = client.post("/api/book/B001/annotations")
        assert response.status_code == 200
        data = response.get_json()
        assert data["success"] is True
        assert data["annotations"] is True

        sidecar = annotated_db.with_name("Dracula.annotations.json")
        assert sidecar.exists()
        assert "audible.clip" in sidecar.read_text(encoding="utf-8")

        # The call is the annotations dump, into a scratch dir of our own, with
        # HOME pointed at /database so audible-cli finds its auth, and capped.
        cmd, kwargs = run.call_args[0][0], run.call_args[1]
        assert cmd[:5] == ["audible", "download", "-a", "B001", "--annotation"]
        assert kwargs["env"]["HOME"] == DATABASE_DIR
        assert kwargs["timeout"] > 0
        assert cmd[cmd.index("-o") + 1] != str(annotated_db.parent)

    def test_no_annotations_is_a_success_not_an_error(self, client, completed_setup, annotated_db):
        # audible-cli exits 0 and writes nothing for a title with no annotations.
        _login_session(client)
        with mock.patch("audible_downloader.routes.subprocess.run", side_effect=self._fake_run(writes_file=False)):
            response = client.post("/api/book/B001/annotations")
        assert response.status_code == 200
        data = response.get_json()
        assert data["success"] is True
        assert data["annotations"] is False
        assert not annotated_db.with_name("Dracula.annotations.json").exists()

    def test_cli_failure_reports_an_error(self, client, completed_setup, annotated_db):
        _login_session(client)
        with mock.patch(
            "audible_downloader.routes.subprocess.run", side_effect=self._fake_run(writes_file=False, returncode=1)
        ):
            response = client.post("/api/book/B001/annotations")
        assert response.status_code == 502
        assert "error" in response.get_json()

    def test_timeout_reports_an_error(self, client, completed_setup, annotated_db):
        import subprocess as sp

        _login_session(client)
        with mock.patch("audible_downloader.routes.subprocess.run", side_effect=sp.TimeoutExpired(["audible"], 120)):
            response = client.post("/api/book/B001/annotations")
        assert response.status_code == 504
        assert "error" in response.get_json()

    def test_scratch_directory_is_cleaned_up(self, client, completed_setup, annotated_db):
        from audible_downloader import TEMP_DIR

        _login_session(client)
        with mock.patch("audible_downloader.routes.subprocess.run", side_effect=self._fake_run(writes_file=True)):
            client.post("/api/book/B001/annotations")
        leftovers = [name for name in os.listdir(TEMP_DIR) if name.startswith("B001_annotations_")]
        assert leftovers == []

    def test_never_downloaded_book_is_rejected(self, client, completed_setup, annotated_db):
        _login_session(client)
        with mock.patch("audible_downloader.routes.subprocess.run") as run:
            response = client.post("/api/book/B002/annotations")
        assert response.status_code == 400
        assert "error" in response.get_json()
        run.assert_not_called()  # no Audible call for a book with nowhere to save

    def test_missing_file_on_disk_is_rejected(self, client, completed_setup, annotated_db):
        _login_session(client)
        with mock.patch("audible_downloader.routes.subprocess.run") as run:
            response = client.post("/api/book/B003/annotations")
        assert response.status_code == 400
        run.assert_not_called()

    def test_unknown_book_returns_404(self, client, completed_setup, annotated_db):
        _login_session(client)
        response = client.post("/api/book/NOPE/annotations")
        assert response.status_code == 404

    def test_split_book_saves_inside_its_folder_at_the_sidecar_base(self, client, completed_setup, annotated_db):
        # v0.24.0 (D9): the row's filepath is the book's FOLDER, so splitting the
        # extension off it would drop the sidecar NEXT to the folder under the
        # folder's own name. It belongs inside, at the base the cover already uses.
        _login_session(client)
        split_dir = annotated_db.parent / "Dracula"
        with mock.patch("audible_downloader.routes.subprocess.run", side_effect=self._fake_run(writes_file=True)):
            response = client.post("/api/book/B004/annotations")
        assert response.status_code == 200
        assert response.get_json()["annotations"] is True
        assert (split_dir / "Bram Stoker - Dracula.annotations.json").exists()
        assert not (annotated_db.parent / "Dracula.annotations.json").exists()

    def test_single_file_book_is_unaffected(self, client, completed_setup, annotated_db):
        # The control: a normal book still saves next to its own audio file.
        _login_session(client)
        with mock.patch("audible_downloader.routes.subprocess.run", side_effect=self._fake_run(writes_file=True)):
            client.post("/api/book/B001/annotations")
        assert annotated_db.with_name("Dracula.annotations.json").exists()

    def test_an_unanswerable_split_base_is_an_error_not_a_guess(self, client, completed_setup, annotated_db):
        # M6: the "next to the audio file" fallback is wrong for exactly the
        # shape that can reach it — a split book's filepath is its FOLDER, so
        # the dump would land beside the folder (and be truncated at any dot in
        # the folder's name). Report the failure instead of hiding the file.
        _login_session(client)
        split_dir = annotated_db.parent / "Dracula"
        with (
            mock.patch("audible_downloader.processing_logic.sidecar_base_for_tracked_book", return_value=None),
            mock.patch("audible_downloader.routes.subprocess.run", side_effect=self._fake_run(writes_file=True)),
        ):
            response = client.post("/api/book/B004/annotations")
        assert response.status_code == 500
        assert "error" in response.get_json()
        assert not (annotated_db.parent / "Dracula.annotations.json").exists()
        assert not list(split_dir.glob("*.annotations.json"))

    def test_an_unanswerable_single_file_base_still_uses_the_fallback(self, client, completed_setup, annotated_db):
        # The control for the guard above: a book with no chapter-file rows has
        # a real audio path to hang the sidecar off, so the fallback stands.
        _login_session(client)
        with (
            mock.patch("audible_downloader.processing_logic.sidecar_base_for_tracked_book", return_value=None),
            mock.patch("audible_downloader.routes.subprocess.run", side_effect=self._fake_run(writes_file=True)),
        ):
            response = client.post("/api/book/B001/annotations")
        assert response.status_code == 200
        assert annotated_db.with_name("Dracula.annotations.json").exists()

    def _run_with_timestamp_source(self, client, source, asin="B001"):
        """POST the annotations route with conversion.file_timestamp_source set."""
        from audible_downloader import processing_logic

        settings = {"conversion": {"file_timestamp_source": source}}
        with (
            mock.patch.object(processing_logic, "load_settings", return_value=settings),
            mock.patch("audible_downloader.routes.subprocess.run", side_effect=self._fake_run(writes_file=True)),
        ):
            return client.post(f"/api/book/{asin}/annotations")

    @pytest.mark.parametrize(
        ("source", "expected"),
        [("release_date", (2019, 6, 27)), ("purchase_date", (2023, 4, 5))],
    )
    def test_sidecar_is_stamped_like_the_books_other_files(
        self, client, completed_setup, annotated_db, source, expected
    ):
        # Backlog #26: download-time annotations are stamped at finalize, so with
        # a timestamp source configured an on-demand dump must not be the one
        # file of the book still reading "now".
        from datetime import datetime

        _login_session(client)
        response = self._run_with_timestamp_source(client, source)
        assert response.status_code == 200
        sidecar = annotated_db.with_name("Dracula.annotations.json")
        assert sidecar.stat().st_mtime == datetime(*expected).timestamp()

    @pytest.mark.parametrize("source", ["none", "bogus"])
    def test_stamping_off_leaves_the_sidecar_alone(self, client, completed_setup, annotated_db, source):
        # The default ("none") and an unrecognized value from an old
        # settings.json both mean "leave real file times alone".
        from datetime import datetime

        _login_session(client)
        response = self._run_with_timestamp_source(client, source)
        assert response.status_code == 200
        sidecar = annotated_db.with_name("Dracula.annotations.json")
        assert sidecar.stat().st_mtime != datetime(2019, 6, 27).timestamp()

    def test_a_book_with_no_usable_date_still_saves_its_annotations(self, client, completed_setup, annotated_db):
        # B004 has no release_date at all: the stamp is skipped, the download is
        # still a success — a cosmetic timestamp must never fail the route.
        _login_session(client)
        response = self._run_with_timestamp_source(client, "release_date", asin="B004")
        assert response.status_code == 200
        assert response.get_json()["annotations"] is True
        assert (annotated_db.parent / "Dracula" / "Bram Stoker - Dracula.annotations.json").exists()

    def test_a_failed_stamp_does_not_fail_the_download(self, client, completed_setup, annotated_db):
        # Resolution or utime blowing up is non-fatal: the annotations are
        # already on disk by then.
        _login_session(client)
        with mock.patch("audible_downloader.routes.os.utime", side_effect=OSError("read-only")):
            response = self._run_with_timestamp_source(client, "release_date")
        assert response.status_code == 200
        assert response.get_json()["annotations"] is True
        assert annotated_db.with_name("Dracula.annotations.json").exists()


class TestGetBookDetailsSplit:
    """v0.24.0 Phase 6: GET /api/book/<asin> reports File Information across a
    split book's parts (its `filepath` is the folder, the audio is in
    `book_files`), while a single-file book keeps the legacy single-stat path."""

    @pytest.fixture
    def details_db(self, tmp_path, monkeypatch):
        """A temp library.db holding one single-file book and three split books
        (all parts present / one part deleted / every part deleted).

        The route reads DB_FILE through its OWN module-level binding for the
        "database not found" guard, so both bindings are pointed at the temp
        database — patching only the db module would 404 every request.
        """
        from audible_downloader import db as db_module
        from audible_downloader import routes as routes_module

        library = tmp_path / "library"
        library.mkdir()

        # The single-file control: the row's path is the audio file itself.
        single_file = library / "Dracula.m4b"
        single_file.write_bytes(b"a" * 1024)

        def make_split(folder_name, part_sizes):
            """Create a book folder with one real .m4b per chapter, returning
            (folder, [part paths]) so a test can delete parts afterwards."""
            folder = library / folder_name
            folder.mkdir()
            paths = []
            for index, size in enumerate(part_sizes, start=1):
                part = folder / f"{folder_name} - {index} - Chapter {index}.m4b"
                part.write_bytes(b"a" * size)
                paths.append(part)
            return folder, paths

        all_present_dir, all_present_parts = make_split("AllPresent", [1024, 2048, 4096])
        one_missing_dir, one_missing_parts = make_split("OneMissing", [1024, 2048, 4096])
        none_present_dir, none_present_parts = make_split("NonePresent", [1024, 2048])

        db_path = tmp_path / "library.db"
        con = sqlite3.connect(db_path)
        con.execute(
            "CREATE TABLE audiobooks ("
            "asin TEXT PRIMARY KEY, title TEXT, author TEXT, status TEXT, filepath TEXT, "
            "custom_title TEXT, custom_author TEXT, custom_cover INTEGER DEFAULT 0, "
            "is_summary_full INTEGER DEFAULT 0, is_duplicate INTEGER DEFAULT 0)"
        )
        # The real schema lives in bin/start.sh; the test database hand-creates
        # only the tables under test, so `book_files` is created here too.
        con.execute(
            "CREATE TABLE IF NOT EXISTS book_files ("
            "asin TEXT NOT NULL, part_index INTEGER NOT NULL, filepath TEXT NOT NULL, "
            "PRIMARY KEY (asin, part_index))"
        )
        con.executemany(
            "INSERT INTO audiobooks (asin, title, author, status, filepath) VALUES (?, ?, ?, ?, ?)",
            [
                ("B001", "Dracula", "Bram Stoker", "DOWNLOADED", str(single_file)),
                ("B002", "AllPresent", "Author", "DOWNLOADED", str(all_present_dir)),
                ("B003", "OneMissing", "Author", "DOWNLOADED", str(one_missing_dir)),
                ("B004", "NonePresent", "Author", "DOWNLOADED", str(none_present_dir)),
            ],
        )
        for asin, parts in (("B002", all_present_parts), ("B003", one_missing_parts), ("B004", none_present_parts)):
            con.executemany(
                "INSERT INTO book_files (asin, part_index, filepath) VALUES (?, ?, ?)",
                [(asin, index, str(part)) for index, part in enumerate(parts)],
            )
        con.commit()
        con.close()

        # The middle part of B003 and every part of B004 vanish from disk while
        # their rows stay behind — the shapes the "Missing" rendering exists for.
        one_missing_parts[1].unlink()
        for part in none_present_parts:
            part.unlink()

        monkeypatch.setattr(db_module, "DB_FILE", str(db_path))
        monkeypatch.setattr(routes_module, "DB_FILE", str(db_path))
        return {
            "single_file": single_file,
            "all_present_dir": all_present_dir,
            "one_missing_dir": one_missing_dir,
            "one_missing": one_missing_parts,
            "none_present": none_present_parts,
        }

    def test_single_file_book_reports_no_parts(self, client, completed_setup, details_db):
        _login_session(client)
        response = client.get("/api/book/B001")
        assert response.status_code == 200
        data = response.get_json()
        assert data["file_count"] == 0
        assert data["files"] == []
        # The legacy single-file stats are unchanged.
        assert data["file_size_hr"] == "1.0 KB"
        assert data["file_type"] == ".m4b Audiobook"
        assert data["file_mtime_hr"] != "N/A"

    def test_split_book_reports_every_part_in_order(self, client, completed_setup, details_db):
        _login_session(client)
        response = client.get("/api/book/B002")
        assert response.status_code == 200
        data = response.get_json()
        assert data["file_count"] == 3
        assert [part["name"] for part in data["files"]] == [
            "AllPresent - 1 - Chapter 1.m4b",
            "AllPresent - 2 - Chapter 2.m4b",
            "AllPresent - 3 - Chapter 3.m4b",
        ]
        assert all(part["size_hr"] != "Missing" for part in data["files"])
        # D3: a split book's `filepath` is its FOLDER — the modal's Path row.
        assert data["filepath"] == str(details_db["all_present_dir"])
        # 1024 + 2048 + 4096 bytes, reported as the set's total.
        assert data["file_size_hr"] == "7.0 KB"
        assert data["file_type"] == ".m4b Audiobook"
        assert data["file_mtime_hr"] != "N/A"

    def test_missing_part_is_flagged_without_losing_the_rest(self, client, completed_setup, details_db):
        _login_session(client)
        response = client.get("/api/book/B003")
        assert response.status_code == 200
        data = response.get_json()
        assert data["file_count"] == 3
        assert data["files"][1]["size_hr"] == "Missing"
        assert data["files"][1]["name"] == "OneMissing - 2 - Chapter 2.m4b"
        assert data["files"][0]["size_hr"] != "Missing"
        assert data["files"][2]["size_hr"] != "Missing"
        # D3: a split book's `filepath` is its FOLDER — the modal's Path row.
        assert data["filepath"] == str(details_db["one_missing_dir"])
        # The total covers only the parts still on disk (1024 + 4096 bytes).
        assert data["file_size_hr"] == "5.0 KB"
        assert data["file_mtime_hr"] != "N/A"

    def test_split_book_with_no_parts_on_disk_reports_na(self, client, completed_setup, details_db):
        _login_session(client)
        response = client.get("/api/book/B004")
        assert response.status_code == 200
        data = response.get_json()
        assert data["file_count"] == 2
        assert [part["size_hr"] for part in data["files"]] == ["Missing", "Missing"]
        assert data["file_size_hr"] == "N/A"
        assert data["file_mtime_hr"] == "N/A"
        # Nothing on disk means no format to assert either (review M2).
        assert data["file_type"] == "N/A"

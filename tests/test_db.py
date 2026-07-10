# tests/test_db.py

import sqlite3

import pytest

from audible_downloader import db as db_module

SEED_ROWS = [
    # (asin, title, author, status, retry_count)
    ("B001", "Alpha", "Author A", "NEW", 0),
    ("B002", "Bravo", "Author B", "MISSING", 0),
    ("B003", "Charlie", "Author C", "ERROR", 0),
    ("B004", "Delta", "Author D", "ERROR", 2),
    ("B005", "Echo", "Author E", "DOWNLOADED", 0),
]


@pytest.fixture
def seeded_db(tmp_path, monkeypatch):
    """A temp library.db with one book in every interesting state."""
    db_path = tmp_path / "library.db"
    con = sqlite3.connect(db_path)
    con.execute(
        "CREATE TABLE audiobooks ("
        "asin TEXT PRIMARY KEY, title TEXT, author TEXT, "
        "status TEXT NOT NULL DEFAULT 'NEW', retry_count INTEGER NOT NULL DEFAULT 0)"
    )
    con.executemany("INSERT INTO audiobooks VALUES (?, ?, ?, ?, ?)", SEED_ROWS)
    con.commit()
    con.close()
    monkeypatch.setattr(db_module, "DB_FILE", str(db_path))
    return db_path


def _asins(books):
    return [book["asin"] for book in books]


class TestGetBooksByStatus:
    def test_single_status(self, seeded_db):
        assert _asins(db_module._get_books_by_status(["NEW"])) == ["B001"]

    def test_multiple_statuses_ordered_by_title(self, seeded_db):
        assert _asins(db_module._get_books_by_status(["MISSING", "NEW"])) == ["B001", "B002"]

    def test_error_excludes_retried_books_by_default(self, seeded_db):
        # Automatic jobs must not endlessly retry a failing book.
        assert _asins(db_module._get_books_by_status(["ERROR"])) == ["B003"]

    def test_error_includes_retried_books_when_requested(self, seeded_db):
        # Manual selection shows every errored book regardless of retries.
        books = db_module._get_books_by_status(["ERROR"], include_errored_retries=True)
        assert _asins(books) == ["B003", "B004"]

    def test_mixed_statuses_with_error(self, seeded_db):
        assert _asins(db_module._get_books_by_status(["NEW", "ERROR"])) == ["B001", "B003"]

    def test_empty_status_list_returns_nothing(self, seeded_db):
        assert db_module._get_books_by_status([]) == []

    def test_missing_db_file_returns_nothing(self, tmp_path, monkeypatch):
        monkeypatch.setattr(db_module, "DB_FILE", str(tmp_path / "does-not-exist.db"))
        assert db_module._get_books_by_status(["NEW"]) == []


class TestGetBooksForAutoJob:
    def test_settings_toggles_select_statuses(self, seeded_db):
        settings = {"tasks": {"auto_process_new": True, "auto_process_missing": False, "auto_process_error": True}}
        # NEW plus non-retried ERROR books; MISSING excluded by settings.
        assert _asins(db_module.get_books_for_auto_job(settings)) == ["B001", "B003"]

    def test_all_toggles_off_selects_nothing(self, seeded_db):
        settings = {"tasks": {}}
        assert db_module.get_books_for_auto_job(settings) == []

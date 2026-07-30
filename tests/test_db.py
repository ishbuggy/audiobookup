# tests/test_db.py

import sqlite3
from threading import Event
from unittest import mock

import pytest

from audible_downloader import db as db_module
from audible_downloader import processing_logic

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


@pytest.fixture
def retry_counter_db(tmp_path, monkeypatch):
    """
    A temp library.db wide enough for the processor's real failure/success
    UPDATEs, so the retry counter can be driven through the writes that maintain
    it and then read back through the auto-process gate.

    Only db_module.DB_FILE is patched: processing_logic goes through the same
    get_db_connection, which resolves DB_FILE at call time. retry_count is left
    nullable exactly as bin/start.sh declares it, so the legacy-NULL row below is
    a state a real database can actually be in.
    """
    db_path = tmp_path / "library.db"
    con = sqlite3.connect(db_path)
    con.execute(
        "CREATE TABLE audiobooks ("
        "asin TEXT PRIMARY KEY, title TEXT, author TEXT, status TEXT NOT NULL DEFAULT 'NEW', "
        "retry_count INTEGER DEFAULT 0, error_message TEXT, filepath TEXT, is_duplicate INTEGER)"
    )
    con.executemany(
        "INSERT INTO audiobooks (asin, title, author, status, retry_count) VALUES (?, ?, ?, ?, ?)",
        [
            ("B009", "Foxtrot", "Author F", "ERROR", 0),  # never failed since its last re-arm
            ("B010", "Golf", "Author G", "ERROR", None),  # legacy row, counter never written
            ("B011", "Hotel", "Author H", "ERROR", 1),  # failed once: the one auto retry is due
            ("B012", "India", "Author I", "ERROR", 2),  # the auto retry failed too
        ],
    )
    con.commit()
    con.close()
    monkeypatch.setattr(db_module, "DB_FILE", str(db_path))
    return db_path


def _read_row(asin):
    con = db_module.get_db_connection()
    try:
        return con.execute("SELECT status, retry_count FROM audiobooks WHERE asin = ?", (asin,)).fetchone()
    finally:
        con.close()


class TestRetryCounterGate:
    """
    v0.23.0 #9: the ERROR gate above only works if something actually raises
    retry_count. The download failure path does (and nothing else does), which is
    what limits a failing book to ONE automatic re-download: the failure that put
    it in ERROR takes the counter to 1, the gate still admits 1 so the retry runs,
    and the retry's own failure takes it to 2 and out. A cancellation must not
    count as an attempt, and success clears the slate.
    """

    def test_gate_admits_zero_and_one_only(self, retry_counter_db):
        # The arithmetic the whole feature rests on: 0 (fresh/re-armed) and 1
        # (failed once, retry due) are in; 2+ is spent; NULL matches neither.
        assert _asins(db_module._get_books_by_status(["ERROR"])) == ["B009", "B011"]
        # Manual selection ignores the counter entirely and offers all four.
        manual = db_module._get_books_by_status(["ERROR"], include_errored_retries=True)
        assert _asins(manual) == ["B009", "B010", "B011", "B012"]

    def test_failure_increments_but_leaves_the_one_retry(self, retry_counter_db):
        # First failure: the book becomes ERROR at 1, which the gate still admits,
        # so the next scheduled run performs the promised single retry.
        processor = processing_logic.BookProcessor(asin="B009", job_id=1)
        with mock.patch.object(processing_logic, "_yield_progress"):
            processor._update_db_on_failure("ffmpeg exploded")

        assert _read_row("B009")["retry_count"] == 1
        assert "B009" in _asins(db_module._get_books_by_status(["ERROR"]))

    def test_second_failure_spends_the_retry(self, retry_counter_db):
        # The automatic retry does NOT reset the counter (only a manual job or a
        # success does), so its failure lands at 2 and the book drops out for good.
        processor = processing_logic.BookProcessor(asin="B009", job_id=1)
        with mock.patch.object(processing_logic, "_yield_progress"):
            processor._update_db_on_failure("first failure")
            processor._update_db_on_failure("second failure")

        assert _read_row("B009")["retry_count"] == 2
        assert "B009" not in _asins(db_module._get_books_by_status(["ERROR"]))
        # The user can still pick it up by hand, which re-arms it.
        manual = db_module._get_books_by_status(["ERROR"], include_errored_retries=True)
        assert "B009" in _asins(manual)

    def test_manual_rearm_grants_exactly_one_more_auto_attempt(self, retry_counter_db):
        # Simulates what a manually started job does (job_manager.start_new_job
        # resets the counter for the books the user picked) to the spent book, then
        # replays a failing attempt. Even though that manual attempt fails, the
        # book is back inside the gate for one automatic retry — and no further.
        con = sqlite3.connect(retry_counter_db)
        con.execute("UPDATE audiobooks SET retry_count = 0 WHERE asin = 'B012'")
        con.commit()
        con.close()

        processor = processing_logic.BookProcessor(asin="B012", job_id=1)
        with mock.patch.object(processing_logic, "_yield_progress"):
            processor._update_db_on_failure("the manual attempt failed too")
        assert _read_row("B012")["retry_count"] == 1
        assert "B012" in _asins(db_module._get_books_by_status(["ERROR"]))

        with mock.patch.object(processing_logic, "_yield_progress"):
            processor._update_db_on_failure("and so did the automatic retry")
        assert _read_row("B012")["retry_count"] == 2
        assert "B012" not in _asins(db_module._get_books_by_status(["ERROR"]))

    def test_legacy_null_counter_stays_out_of_automatic_jobs(self, retry_counter_db):
        # A row predating the column can hold NULL, which neither `<= 1` nor any
        # plain comparison matches — deliberately, so these rows keep the behavior
        # they have always had (never auto-retried). The failure UPDATE still
        # coalesces, because a plain NULL + 1 stays NULL and would leave the
        # counter stuck at "unknown" forever.
        assert "B010" not in _asins(db_module._get_books_by_status(["ERROR"]))

        processor = processing_logic.BookProcessor(asin="B010", job_id=1)
        with mock.patch.object(processing_logic, "_yield_progress"):
            processor._update_db_on_failure("ffmpeg exploded")

        assert _read_row("B010")["retry_count"] == 1

    def test_cancellation_does_not_consume_the_retry(self, retry_counter_db):
        # A user-cancelled download is not a failure: _fail_or_cancel short-
        # circuits, so the status and the counter are both left alone and the
        # book is still eligible for its automatic attempt.
        stop_event = Event()
        stop_event.set()
        processor = processing_logic.BookProcessor(asin="B009", job_id=1, stop_event=stop_event)
        with mock.patch.object(processing_logic, "_yield_progress"):
            processor._fail_or_cancel("Final merge of chapter chunks failed.")

        row = _read_row("B009")
        assert row["retry_count"] == 0
        assert row["status"] == "ERROR"  # unchanged: it was already ERROR here
        assert "B009" in _asins(db_module._get_books_by_status(["ERROR"]))

    def test_success_resets_the_counter(self, retry_counter_db):
        processor = processing_logic.BookProcessor(asin="B009", job_id=1)
        with mock.patch.object(processing_logic, "_yield_progress"):
            processor._update_db_on_failure("ffmpeg exploded")
        assert _read_row("B009")["retry_count"] == 1

        processor.final_output_path = "/data/Author F/Foxtrot/Foxtrot.m4b"
        with (
            mock.patch.object(processing_logic, "_yield_progress"),
            mock.patch.object(processor, "_verify_output_file", return_value=(True, None)),
            mock.patch.object(processor, "_place_supplementary_pdf"),
            mock.patch.object(processor, "_place_sidecar_files"),
            mock.patch.object(processor, "_apply_file_timestamps"),
            mock.patch.object(processor, "_cleanup_stale_files"),
        ):
            processor._finalize_success(conversion_start_time=0.0, record_eta=False)

        row = _read_row("B009")
        assert row["status"] == "DOWNLOADED"
        assert row["retry_count"] == 0


class TestApplyMetadataOverrides:
    """Phase 5: custom title/author become the effective display values while
    the native Audible values are preserved for the edit UI."""

    def test_custom_values_win_and_native_preserved(self):
        result = db_module.apply_metadata_overrides(
            {"title": "Native T", "author": "Native A", "custom_title": "Custom T", "custom_author": "Custom A"}
        )
        assert result["title"] == "Custom T"
        assert result["author"] == "Custom A"
        assert result["native_title"] == "Native T"
        assert result["native_author"] == "Native A"

    def test_missing_custom_falls_back_to_native(self):
        result = db_module.apply_metadata_overrides(
            {"title": "T", "author": "A", "custom_title": None, "custom_author": None}
        )
        assert result["title"] == "T"
        assert result["author"] == "A"
        assert result["native_title"] == "T"

    def test_partial_override_only_affects_that_field(self):
        result = db_module.apply_metadata_overrides(
            {"title": "T", "author": "A", "custom_title": "Nicer Title", "custom_author": None}
        )
        assert result["title"] == "Nicer Title"
        assert result["author"] == "A"


@pytest.fixture
def full_library_db(tmp_path, monkeypatch):
    """A temp library.db with the columns get_all_books selects, so the grid
    query (including the Phase 5 is_duplicate flag) can be exercised."""
    db_path = tmp_path / "library.db"
    con = sqlite3.connect(db_path)
    con.execute(
        "CREATE TABLE audiobooks ("
        "asin TEXT PRIMARY KEY, title TEXT, author TEXT, custom_title TEXT, custom_author TEXT, "
        "status TEXT, series TEXT, narrator TEXT, runtime_min INTEGER, release_date TEXT, "
        "date_added TEXT, source TEXT, is_duplicate INTEGER)"
    )
    con.executemany(
        "INSERT INTO audiobooks (asin, title, author, status, is_duplicate) VALUES (?, ?, ?, ?, ?)",
        [
            ("B001", "Dracula", "Bram Stoker", "DOWNLOADED", 1),
            ("B002", "Frankenstein", "Mary Shelley", "DOWNLOADED", 0),
            ("B003", "Legacy Row", "Old Author", "DOWNLOADED", None),
        ],
    )
    con.commit()
    con.close()
    monkeypatch.setattr(db_module, "DB_FILE", str(db_path))
    return db_path


class TestGetAllBooksDuplicateFlag:
    """Phase 5.1: get_all_books surfaces is_duplicate so the grid can badge and
    filter flagged duplicates; NULL (old rows) defaults to 0."""

    def test_is_duplicate_is_surfaced(self, full_library_db):
        by_asin = {b["asin"]: b for b in db_module.get_all_books()}
        assert by_asin["B001"]["is_duplicate"] == 1
        assert by_asin["B002"]["is_duplicate"] == 0

    def test_null_flag_defaults_to_zero(self, full_library_db):
        by_asin = {b["asin"]: b for b in db_module.get_all_books()}
        assert by_asin["B003"]["is_duplicate"] == 0

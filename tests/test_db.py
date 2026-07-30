# tests/test_db.py

import sqlite3
from threading import Event
from unittest import mock

import pytest

from audible_downloader import db as db_module
from audible_downloader import processing_logic

# Copied verbatim from the idempotent migration block in bin/start.sh: schema
# creation is owned by that script, so test fixtures must build the same table.
BOOK_FILES_DDL = (
    "CREATE TABLE IF NOT EXISTS book_files ("
    "asin TEXT NOT NULL, part_index INTEGER NOT NULL, filepath TEXT NOT NULL, "
    "PRIMARY KEY (asin, part_index))"
)

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
        # A processor per attempt, because that is what really happens
        # (run_book_processing_logic builds a fresh BookProcessor per call) and
        # because the failure write is latched to one report per run: two failures
        # only count twice when they belong to two attempts.
        with mock.patch.object(processing_logic, "_yield_progress"):
            processing_logic.BookProcessor(asin="B009", job_id=1)._update_db_on_failure("first failure")
            processing_logic.BookProcessor(asin="B009", job_id=2)._update_db_on_failure("second failure")

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

        # One processor per attempt, as the two jobs would really build them.
        with mock.patch.object(processing_logic, "_yield_progress"):
            processing_logic.BookProcessor(asin="B012", job_id=1)._update_db_on_failure("the manual attempt failed too")
        assert _read_row("B012")["retry_count"] == 1
        assert "B012" in _asins(db_module._get_books_by_status(["ERROR"]))

        with mock.patch.object(processing_logic, "_yield_progress"):
            processing_logic.BookProcessor(asin="B012", job_id=2)._update_db_on_failure(
                "and so did the automatic retry"
            )
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
    # Schema creation lives in bin/start.sh, so the fixture mirrors its
    # `book_files` migration verbatim (v0.24.0 per-chapter splitting).
    con.execute(BOOK_FILES_DDL)
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


class TestGetAllBooksFileCount:
    """v0.24.0 Phase 1: the grid query also reports how many part files a book
    owns, so a split book can be told from a single-file one. Books with no
    `book_files` rows (every book today) must report 0, not NULL."""

    def test_books_without_parts_report_zero(self, full_library_db):
        by_asin = {b["asin"]: b for b in db_module.get_all_books()}
        assert by_asin["B001"]["file_count"] == 0
        assert by_asin["B002"]["file_count"] == 0
        assert by_asin["B003"]["file_count"] == 0

    def test_split_book_reports_its_part_count(self, full_library_db):
        db_module.replace_book_files("B002", ["/data/one.m4b", "/data/two.m4b", "/data/three.m4b"])

        by_asin = {b["asin"]: b for b in db_module.get_all_books()}
        assert by_asin["B002"]["file_count"] == 3
        # Only the split book is affected; its neighbours stay single-file.
        assert by_asin["B001"]["file_count"] == 0
        assert by_asin["B003"]["file_count"] == 0

    def test_existing_row_shape_is_unchanged(self, full_library_db):
        # file_count is purely additive: every key existing consumers read is
        # still present, so routes and the frontend keep working untouched.
        book = next(b for b in db_module.get_all_books() if b["asin"] == "B001")
        for key in (
            "author",
            "title",
            "custom_title",
            "custom_author",
            "status",
            "asin",
            "series",
            "narrator",
            "runtime_min",
            "release_date",
            "date_added",
            "source",
            "is_duplicate",
            "native_title",
            "native_author",
            "cover_url",
        ):
            assert key in book
        assert book["cover_url"] == "/covers/B001_thumb.jpg"


@pytest.fixture
def book_files_db(tmp_path, monkeypatch):
    """A temp library.db holding only the `book_files` table, built from the
    same DDL bin/start.sh runs."""
    db_path = tmp_path / "library.db"
    con = sqlite3.connect(db_path)
    con.execute(BOOK_FILES_DDL)
    con.commit()
    con.close()
    monkeypatch.setattr(db_module, "DB_FILE", str(db_path))
    return db_path


def _paths(rows):
    return [row["filepath"] for row in rows]


class TestBookFiles:
    """v0.24.0 Phase 1: the part-file helpers a split book's rows go through.
    They are inert here — nothing writes parts yet — but the row set they
    maintain is what later phases treat as authoritative."""

    def test_replace_then_get_round_trip(self, book_files_db):
        db_module.replace_book_files("B001", ["/data/ch1.m4b", "/data/ch2.m4b"])

        rows = db_module.get_book_files("B001")
        assert _paths(rows) == ["/data/ch1.m4b", "/data/ch2.m4b"]
        # part_index is the zero-based position in the list passed in.
        assert [row["part_index"] for row in rows] == [0, 1]

    def test_get_returns_empty_for_unsplit_book(self, book_files_db):
        assert db_module.get_book_files("B999") == []

    def test_rows_come_back_in_part_order(self, book_files_db):
        # Insert deliberately out of order to prove the ORDER BY does the work
        # rather than the natural row order happening to be right.
        con = sqlite3.connect(book_files_db)
        con.executemany(
            "INSERT INTO book_files (asin, part_index, filepath) VALUES (?, ?, ?)",
            [("B001", 2, "/data/ch3.m4b"), ("B001", 0, "/data/ch1.m4b"), ("B001", 1, "/data/ch2.m4b")],
        )
        con.commit()
        con.close()

        assert _paths(db_module.get_book_files("B001")) == ["/data/ch1.m4b", "/data/ch2.m4b", "/data/ch3.m4b"]

    def test_replace_leaves_no_orphans_when_the_set_shrinks(self, book_files_db):
        # The failure this guards against: a re-download producing fewer chapters
        # leaving the tail of the previous run behind as phantom parts.
        db_module.replace_book_files("B001", [f"/data/old{i}.m4b" for i in range(5)])
        db_module.replace_book_files("B001", ["/data/new1.m4b", "/data/new2.m4b"])

        rows = db_module.get_book_files("B001")
        assert _paths(rows) == ["/data/new1.m4b", "/data/new2.m4b"]
        assert [row["part_index"] for row in rows] == [0, 1]

    def test_replace_with_empty_list_clears_the_book(self, book_files_db):
        db_module.replace_book_files("B001", ["/data/ch1.m4b", "/data/ch2.m4b"])
        db_module.replace_book_files("B001", [])
        assert db_module.get_book_files("B001") == []

    def test_replace_only_touches_the_given_asin(self, book_files_db):
        db_module.replace_book_files("B001", ["/data/a1.m4b", "/data/a2.m4b"])
        db_module.replace_book_files("B002", ["/data/b1.m4b"])

        db_module.replace_book_files("B001", ["/data/a-new.m4b"])
        assert _paths(db_module.get_book_files("B001")) == ["/data/a-new.m4b"]
        assert _paths(db_module.get_book_files("B002")) == ["/data/b1.m4b"]

    def test_replace_can_share_the_callers_transaction(self, book_files_db):
        # The finalize path writes the audiobooks row and the part rows together;
        # with a caller-owned connection nothing is visible until the commit.
        con = db_module.get_db_connection()
        try:
            db_module.replace_book_files("B001", ["/data/ch1.m4b"], con=con)
            # A separate connection still sees the pre-commit state.
            assert db_module.get_book_files("B001") == []
            con.commit()
        finally:
            con.close()

        assert _paths(db_module.get_book_files("B001")) == ["/data/ch1.m4b"]

    def test_shared_transaction_rolls_back_with_the_caller(self, book_files_db):
        db_module.replace_book_files("B001", ["/data/original.m4b"])

        con = db_module.get_db_connection()
        try:
            db_module.replace_book_files("B001", ["/data/replacement.m4b"], con=con)
            con.rollback()
        finally:
            con.close()

        # The caller abandoned its transaction, so the old part list survives.
        assert _paths(db_module.get_book_files("B001")) == ["/data/original.m4b"]

    def test_delete_removes_every_part(self, book_files_db):
        db_module.replace_book_files("B001", ["/data/ch1.m4b", "/data/ch2.m4b"])
        db_module.replace_book_files("B002", ["/data/other.m4b"])

        db_module.delete_book_files("B001")
        assert db_module.get_book_files("B001") == []
        assert _paths(db_module.get_book_files("B002")) == ["/data/other.m4b"]

    def test_delete_of_unsplit_book_is_a_no_op(self, book_files_db):
        db_module.delete_book_files("B999")
        assert db_module.get_book_files("B999") == []

    def test_all_tracked_part_paths_spans_every_book(self, book_files_db):
        db_module.replace_book_files("B001", ["/data/a1.m4b", "/data/a2.m4b"])
        db_module.replace_book_files("B002", ["/data/b1.m4b"])

        assert sorted(db_module.get_all_tracked_part_paths()) == ["/data/a1.m4b", "/data/a2.m4b", "/data/b1.m4b"]

    def test_all_tracked_part_paths_is_empty_without_parts(self, book_files_db):
        assert db_module.get_all_tracked_part_paths() == []

    def test_missing_db_file_returns_nothing(self, tmp_path, monkeypatch):
        monkeypatch.setattr(db_module, "DB_FILE", str(tmp_path / "does-not-exist.db"))
        assert db_module.get_book_files("B001") == []
        assert db_module.get_all_tracked_part_paths() == []

    def test_writers_do_not_create_a_stub_db_when_the_file_is_missing(self, tmp_path, monkeypatch):
        # sqlite3.connect creates the file it is pointed at, so an unguarded
        # writer would leave a 0-byte "database" behind that start.sh would then
        # try to migrate on the next boot. Both writers must no-op instead.
        db_path = tmp_path / "does-not-exist.db"
        monkeypatch.setattr(db_module, "DB_FILE", str(db_path))

        db_module.replace_book_files("B001", ["/data/ch1.m4b"])
        db_module.delete_book_files("B001")

        assert not db_path.exists()

    def test_shared_connection_write_is_unaffected_by_the_missing_file_guard(self, book_files_db, monkeypatch):
        # The guard only covers the connection this module opens itself; a
        # borrowed connection is already bound to a real database, so pointing
        # DB_FILE elsewhere must not stop the caller's write.
        con = db_module.get_db_connection()
        monkeypatch.setattr(db_module, "DB_FILE", str(book_files_db.parent / "gone.db"))
        try:
            db_module.replace_book_files("B001", ["/data/ch1.m4b"], con=con)
            con.commit()
        finally:
            con.close()

        monkeypatch.setattr(db_module, "DB_FILE", str(book_files_db))
        assert _paths(db_module.get_book_files("B001")) == ["/data/ch1.m4b"]

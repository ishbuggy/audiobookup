# tests/test_verification_logic.py
#
# v0.24.0 Phase 5: the library integrity audit once a book can own many files.
# A split book (one that has `book_files` rows) must have EVERY part on disk and
# its parts' SUMMED duration inside the same tolerance a single file gets (D10:
# no partial state — any missing part fails the whole book).
#
# Uses a real temp SQLite DB and stubs only the ffprobe subprocess, so the
# status/error_message/retry_count writes are exercised for real.

import sqlite3
from unittest import mock

import pytest

from audible_downloader import db as db_module
from audible_downloader import verification_logic

# Copied verbatim from the idempotent migration block in bin/start.sh: schema
# creation is owned by that script, so test fixtures must build the same table.
BOOK_FILES_DDL = (
    "CREATE TABLE IF NOT EXISTS book_files ("
    "asin TEXT NOT NULL, part_index INTEGER NOT NULL, filepath TEXT NOT NULL, "
    "PRIMARY KEY (asin, part_index))"
)


@pytest.fixture
def db(tmp_path, monkeypatch):
    """A temp library.db with the verification-relevant schema."""
    db_path = tmp_path / "library.db"
    con = sqlite3.connect(db_path)
    con.execute(
        "CREATE TABLE audiobooks ("
        "asin TEXT PRIMARY KEY, title TEXT, status TEXT NOT NULL DEFAULT 'NEW', "
        "runtime_min INTEGER, filepath TEXT, error_message TEXT, retry_count INTEGER DEFAULT 0)"
    )
    con.execute(BOOK_FILES_DDL)
    con.commit()
    con.close()
    monkeypatch.setattr(db_module, "DB_FILE", str(db_path))
    return db_path


def _seed_book(db, asin, *, runtime_min, filepath, parts=(), status="DOWNLOADED", retry_count=0):
    con = sqlite3.connect(db)
    con.execute(
        "INSERT INTO audiobooks (asin, title, status, runtime_min, filepath, retry_count) VALUES (?, ?, ?, ?, ?, ?)",
        (asin, f"Book {asin}", status, runtime_min, filepath, retry_count),
    )
    for index, part in enumerate(parts):
        con.execute("INSERT INTO book_files (asin, part_index, filepath) VALUES (?, ?, ?)", (asin, index, part))
    con.commit()
    con.close()


def _book(db, asin):
    con = sqlite3.connect(db)
    con.row_factory = sqlite3.Row
    row = dict(con.execute("SELECT * FROM audiobooks WHERE asin = ?", (asin,)).fetchone())
    con.close()
    return row


def _make_file(path):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"audio")
    return str(path)


def _ffprobe(durations, failures=()):
    """
    Stub subprocess.run for the duration probe: `durations` maps a filepath to
    the seconds ffprobe reports; a path in `failures` returns a non-zero exit
    (the "corrupt file" branch). The probed path is always the last argv item.
    """

    def run(cmd, *args, **kwargs):
        path = cmd[-1]
        if path in failures:
            return mock.Mock(returncode=1, stdout="", stderr="moov atom not found")
        return mock.Mock(returncode=0, stdout=f"{durations[path]}\n", stderr="")

    return mock.patch("audible_downloader.verification_logic.subprocess.run", side_effect=run)


class TestSplitBookVerification:
    def test_all_parts_present_and_durations_sum_within_tolerance_passes(self, db, tmp_path):
        folder = tmp_path / "Split Book"
        parts = [_make_file(folder / f"{n:02d}.m4b") for n in (1, 2, 3)]
        # 3 x 20 minutes = the book's full 60-minute runtime.
        _seed_book(db, "B0SPLIT123", runtime_min=60, filepath=str(folder), parts=parts)

        with _ffprobe(dict.fromkeys(parts, 1200.0)):
            verification_logic.run_verification_logic(1)

        row = _book(db, "B0SPLIT123")
        assert row["status"] == "DOWNLOADED"
        assert row["retry_count"] == 0

    def test_one_missing_part_errors_with_an_n_of_m_message(self, db, tmp_path):
        folder = tmp_path / "Split Book"
        present = [_make_file(folder / f"{n:02d}.m4b") for n in (1, 2)]
        gone = str(folder / "03.m4b")
        _seed_book(db, "B0SPLIT123", runtime_min=60, filepath=str(folder), parts=[*present, gone])

        # The folder itself still exists, so only a per-part check can catch this.
        with _ffprobe(dict.fromkeys(present, 1200.0)) as run:
            verification_logic.run_verification_logic(1)

        row = _book(db, "B0SPLIT123")
        assert row["status"] == "ERROR"
        assert row["error_message"] == "Integrity Check Failed: 1 of 3 parts missing from disk."
        run.assert_not_called()  # a missing part short-circuits before any probing

    def test_several_missing_parts_are_counted(self, db, tmp_path):
        folder = tmp_path / "Split Book"
        parts = [str(folder / f"{n:02d}.m4b") for n in range(1, 13)]
        _make_file(folder / "01.m4b")
        _seed_book(db, "B0SPLIT123", runtime_min=600, filepath=str(folder), parts=parts)

        with _ffprobe({}):
            verification_logic.run_verification_logic(1)

        assert _book(db, "B0SPLIT123")["error_message"] == "Integrity Check Failed: 11 of 12 parts missing from disk."

    def test_summed_duration_far_too_short_errors(self, db, tmp_path):
        folder = tmp_path / "Split Book"
        parts = [_make_file(folder / f"{n:02d}.m4b") for n in (1, 2)]
        # Expected 600 minutes, the parts only add up to 20 — a truncated split.
        _seed_book(db, "B0SPLIT123", runtime_min=600, filepath=str(folder), parts=parts)

        with _ffprobe(dict.fromkeys(parts, 600.0)):
            verification_logic.run_verification_logic(1)

        row = _book(db, "B0SPLIT123")
        assert row["status"] == "ERROR"
        assert row["error_message"] == (
            "Integrity Check Failed: Duration mismatch across 2 parts (Expected 600m, Got 20m)."
        )

    def test_small_shortfall_is_within_tolerance(self, db, tmp_path):
        # Same tolerance as a single file: a failure needs BOTH >5% short and
        # more than 10 minutes off. 5 minutes short of 600 is neither.
        folder = tmp_path / "Split Book"
        parts = [_make_file(folder / f"{n:02d}.m4b") for n in (1, 2)]
        _seed_book(db, "B0SPLIT123", runtime_min=600, filepath=str(folder), parts=parts)

        with _ffprobe(dict.fromkeys(parts, 17850.0)):  # 595 minutes total
            verification_logic.run_verification_logic(1)

        assert _book(db, "B0SPLIT123")["status"] == "DOWNLOADED"

    def test_a_longer_book_than_expected_is_not_a_failure(self, db, tmp_path):
        # Only a significantly SHORTER book fails; extra runtime (intro/outro
        # chapters) must not flag the book.
        folder = tmp_path / "Split Book"
        parts = [_make_file(folder / f"{n:02d}.m4b") for n in (1, 2)]
        _seed_book(db, "B0SPLIT123", runtime_min=60, filepath=str(folder), parts=parts)

        with _ffprobe(dict.fromkeys(parts, 3000.0)):  # 100 minutes total
            verification_logic.run_verification_logic(1)

        assert _book(db, "B0SPLIT123")["status"] == "DOWNLOADED"

    def test_every_part_is_probed_exactly_once(self, db, tmp_path):
        folder = tmp_path / "Split Book"
        parts = [_make_file(folder / f"{n:02d}.m4b") for n in range(1, 6)]
        _seed_book(db, "B0SPLIT123", runtime_min=50, filepath=str(folder), parts=parts)

        with _ffprobe(dict.fromkeys(parts, 600.0)) as run:
            verification_logic.run_verification_logic(1)

        probed = [call.args[0][-1] for call in run.call_args_list]
        assert probed == parts

    def test_a_corrupt_part_errors_naming_its_position(self, db, tmp_path):
        folder = tmp_path / "Split Book"
        parts = [_make_file(folder / f"{n:02d}.m4b") for n in (1, 2, 3)]
        _seed_book(db, "B0SPLIT123", runtime_min=60, filepath=str(folder), parts=parts)

        with _ffprobe(dict.fromkeys(parts, 1200.0), failures={parts[1]}) as run:
            verification_logic.run_verification_logic(1)

        row = _book(db, "B0SPLIT123")
        assert row["status"] == "ERROR"
        assert row["error_message"].startswith("Integrity Check Failed: Part 2 of 3 corrupt.")
        assert run.call_count == 2  # stopped at the bad part

    def test_split_book_with_no_runtime_skips_the_duration_check(self, db, tmp_path):
        folder = tmp_path / "Split Book"
        parts = [_make_file(folder / f"{n:02d}.m4b") for n in (1, 2)]
        _seed_book(db, "B0SPLIT123", runtime_min=0, filepath=str(folder), parts=parts)

        with _ffprobe({}) as run:
            verification_logic.run_verification_logic(1)

        assert _book(db, "B0SPLIT123")["status"] == "DOWNLOADED"
        run.assert_not_called()

    def test_an_unexpected_probe_error_does_not_flag_the_book(self, db, tmp_path):
        # Matches the single-file policy: an exception is logged, never turned
        # into an automatic ERROR.
        folder = tmp_path / "Split Book"
        parts = [_make_file(folder / f"{n:02d}.m4b") for n in (1, 2)]
        _seed_book(db, "B0SPLIT123", runtime_min=60, filepath=str(folder), parts=parts)

        with mock.patch("audible_downloader.verification_logic.subprocess.run", side_effect=OSError("boom")):
            verification_logic.run_verification_logic(1)

        assert _book(db, "B0SPLIT123")["status"] == "DOWNLOADED"


class TestSingleFileVerification:
    def test_present_file_of_the_right_length_passes(self, db, tmp_path):
        path = _make_file(tmp_path / "Book.m4b")
        _seed_book(db, "B0SINGLE12", runtime_min=60, filepath=path)

        with _ffprobe({path: 3600.0}):
            verification_logic.run_verification_logic(1)

        assert _book(db, "B0SINGLE12")["status"] == "DOWNLOADED"

    def test_missing_file_keeps_its_original_message(self, db, tmp_path):
        _seed_book(db, "B0SINGLE12", runtime_min=60, filepath=str(tmp_path / "gone.m4b"))

        with _ffprobe({}):
            verification_logic.run_verification_logic(1)

        row = _book(db, "B0SINGLE12")
        assert row["status"] == "ERROR"
        assert row["error_message"] == "Integrity Check Failed: File missing from disk."

    def test_truncated_file_keeps_its_original_message(self, db, tmp_path):
        path = _make_file(tmp_path / "Book.m4b")
        _seed_book(db, "B0SINGLE12", runtime_min=600, filepath=path)

        with _ffprobe({path: 1200.0}):
            verification_logic.run_verification_logic(1)

        row = _book(db, "B0SINGLE12")
        assert row["status"] == "ERROR"
        assert row["error_message"] == "Integrity Check Failed: Duration mismatch (Expected 600m, Got 20m)."

    def test_corrupt_file_keeps_its_original_message(self, db, tmp_path):
        path = _make_file(tmp_path / "Book.m4b")
        _seed_book(db, "B0SINGLE12", runtime_min=60, filepath=path)

        with _ffprobe({path: 3600.0}, failures={path}):
            verification_logic.run_verification_logic(1)

        assert _book(db, "B0SINGLE12")["error_message"].startswith("Integrity Check Failed: File corrupt.")


class TestMarkAsError:
    def test_error_write_arms_exactly_one_automatic_retry(self, db, tmp_path):
        # #27: the auto-process gate selects ERROR books with retry_count <= 1, so
        # a book flagged here at count 0 would otherwise get TWO automatic
        # re-downloads instead of the one the settings UI promises.
        _seed_book(db, "B0SINGLE12", runtime_min=60, filepath="", retry_count=0)

        verification_logic._mark_as_error("B0SINGLE12", "Integrity Check Failed: File missing from disk.")

        row = _book(db, "B0SINGLE12")
        assert row["status"] == "ERROR"
        assert row["retry_count"] == 1

    def test_repeated_failures_do_not_ratchet_the_counter_past_the_gate(self, db):
        # Set, not increment: a book failing verification over and over must stay
        # eligible for its one retry rather than climbing out of reach.
        _seed_book(db, "B0SINGLE12", runtime_min=60, filepath="", retry_count=1)

        verification_logic._mark_as_error("B0SINGLE12", "Integrity Check Failed: File missing from disk.")

        assert _book(db, "B0SINGLE12")["retry_count"] == 1

    def test_verification_failure_sets_the_counter_through_the_job(self, db, tmp_path):
        _seed_book(db, "B0SPLIT123", runtime_min=60, filepath=str(tmp_path / "Split"), parts=[str(tmp_path / "1.m4b")])

        with _ffprobe({}):
            verification_logic.run_verification_logic(1)

        assert _book(db, "B0SPLIT123")["retry_count"] == 1

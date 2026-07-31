# tests/test_sync_logic.py
#
# v0.24.0 Phase 5: the deep sync's filesystem scan and its DB reconcile, once a
# book can own MANY files. Both helpers are generators that return their result
# via StopIteration, so every test drains them through `_drain`.
#
# The scan is driven entirely off the on-disk `.file_scan_cache` (real mtimes,
# pre-seeded ASINs) so no ffprobe subprocess is ever spawned; the reconcile runs
# against a real temp SQLite DB carrying the production `audiobooks`/`book_files`
# shape.

import os
import sqlite3

import pytest

from audible_downloader import db as db_module
from audible_downloader import sync_logic

# Copied verbatim from the idempotent migration block in bin/start.sh: schema
# creation is owned by that script, so test fixtures must build the same table.
BOOK_FILES_DDL = (
    "CREATE TABLE IF NOT EXISTS book_files ("
    "asin TEXT NOT NULL, part_index INTEGER NOT NULL, filepath TEXT NOT NULL, "
    "PRIMARY KEY (asin, part_index))"
)


def _drain(generator):
    """Run a progress generator to completion and return its `return` value."""
    while True:
        try:
            next(generator)
        except StopIteration as stop:
            return stop.value


@pytest.fixture
def library(tmp_path, monkeypatch):
    """
    A temp /data root plus a temp CONFIG_DIR for the scan cache. Both are module
    constants resolved at call time, so patching them isolates the scan entirely.
    """
    data = tmp_path / "data"
    data.mkdir()
    config = tmp_path / "config"
    config.mkdir()
    monkeypatch.setattr(sync_logic, "AUDIOBOOK_LIBRARY_PATH", str(data))
    monkeypatch.setattr(sync_logic, "CONFIG_DIR", str(config))
    return data


def _make_audio(path, content=b"audio"):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)
    return str(path)


def _seed_scan_cache(config_dir, path_to_asin):
    """
    Write the `.file_scan_cache` lines (`mtime|asin|filepath`) for the given
    files using their REAL mtimes, so every file is a cache HIT and the scanner
    never shells out to ffprobe.
    """
    lines = []
    for path, asin in path_to_asin.items():
        mtime = str(int(os.path.getmtime(path)))
        lines.append(f"{mtime}|{asin}|{path}\n")
    with open(os.path.join(config_dir, ".file_scan_cache"), "w", encoding="utf-8") as f:
        f.writelines(lines)


@pytest.fixture
def db(tmp_path, monkeypatch):
    """A temp library.db with the reconcile-relevant schema."""
    db_path = tmp_path / "library.db"
    con = sqlite3.connect(db_path)
    con.execute(
        "CREATE TABLE audiobooks (asin TEXT PRIMARY KEY, title TEXT, status TEXT NOT NULL DEFAULT 'NEW', "
        "filepath TEXT, retry_count INTEGER DEFAULT 0)"
    )
    con.execute(BOOK_FILES_DDL)
    con.commit()
    con.close()
    monkeypatch.setattr(db_module, "DB_FILE", str(db_path))
    return db_path


def _seed_book(db, asin, status, filepath, parts=(), retry_count=0):
    con = sqlite3.connect(db)
    con.execute(
        "INSERT INTO audiobooks (asin, title, status, filepath, retry_count) VALUES (?, ?, ?, ?, ?)",
        (asin, f"Book {asin}", status, filepath, retry_count),
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


def _parts(db, asin):
    con = sqlite3.connect(db)
    rows = con.execute("SELECT filepath FROM book_files WHERE asin = ? ORDER BY part_index", (asin,)).fetchall()
    con.close()
    return [row[0] for row in rows]


class TestScanLocalFilesystem:
    def test_multiple_parts_of_one_book_all_map_to_that_asin(self, library, tmp_path):
        # The regression this phase exists for: N parts share ONE embedded ASIN,
        # and the old dict-valued map kept only the last file seen.
        folder = library / "Author" / "Split Book"
        paths = [_make_audio(folder / f"{n:02d} - Chapter {n}.m4b") for n in (1, 2, 3)]
        _seed_scan_cache(str(tmp_path / "config"), dict.fromkeys(paths, "B0SPLIT123"))

        found = _drain(sync_logic._scan_local_filesystem(1))

        assert set(found) == {"B0SPLIT123"}
        # Sorted, not filesystem order: the reconcile's "first file" pick for an
        # untracked multi-file ASIN must be reproducible run to run.
        assert found["B0SPLIT123"] == sorted(paths)

    def test_single_file_book_maps_to_a_one_element_list(self, library, tmp_path):
        path = _make_audio(library / "Author" / "Book.m4b")
        _seed_scan_cache(str(tmp_path / "config"), {path: "B0SINGLE12"})

        found = _drain(sync_logic._scan_local_filesystem(1))

        assert found == {"B0SINGLE12": [path]}

    def test_scan_order_is_sorted_not_filesystem_order(self, library, tmp_path):
        # Written in an order chosen to differ from the sorted one, so the assert
        # would fail if the scan simply followed os.walk.
        folder = library / "Author" / "Split Book"
        names = ["03 - Three.m4b", "01 - One.m4b", "02 - Two.m4b"]
        paths = [_make_audio(folder / name) for name in names]
        _seed_scan_cache(str(tmp_path / "config"), dict.fromkeys(paths, "B0SPLIT123"))

        found = _drain(sync_logic._scan_local_filesystem(1))

        assert found["B0SPLIT123"] == sorted(paths)
        assert found["B0SPLIT123"][0].endswith("01 - One.m4b")

    def test_distinct_books_stay_separate(self, library, tmp_path):
        one = _make_audio(library / "A" / "One.m4b")
        two = _make_audio(library / "B" / "Two.mp3")
        _seed_scan_cache(str(tmp_path / "config"), {one: "B0ONE00000", two: "B0TWO00000"})

        found = _drain(sync_logic._scan_local_filesystem(1))

        assert found == {"B0ONE00000": [one], "B0TWO00000": [two]}


class TestReconcileMissing:
    def test_split_book_is_marked_missing_when_a_part_is_gone(self, db, tmp_path):
        # The folder still exists (it holds the surviving parts), so the old
        # `os.path.exists(filepath)` check would have called this book fine.
        folder = tmp_path / "data" / "Split Book"
        kept = _make_audio(folder / "01.m4b")
        gone = str(folder / "02.m4b")
        _seed_book(db, "B0SPLIT123", "DOWNLOADED", str(folder), parts=[kept, gone])

        _drain(sync_logic._reconcile_database(1, {}))

        row = _book(db, "B0SPLIT123")
        assert row["status"] == "MISSING"
        assert row["filepath"] == ""
        # The part rows are KEPT: they record the expected set (so a later scan
        # can restore the book) and keep the import scanner from reporting the
        # surviving part as an untracked file.
        assert _parts(db, "B0SPLIT123") == [kept, gone]

    def test_split_book_with_every_part_present_is_left_alone(self, db, tmp_path):
        folder = tmp_path / "data" / "Split Book"
        parts = [_make_audio(folder / f"{n:02d}.m4b") for n in (1, 2)]
        _seed_book(db, "B0SPLIT123", "DOWNLOADED", str(folder), parts=parts)

        _drain(sync_logic._reconcile_database(1, {}))

        row = _book(db, "B0SPLIT123")
        assert row["status"] == "DOWNLOADED"
        assert row["filepath"] == str(folder)

    def test_single_file_book_with_missing_file_is_marked_missing(self, db, tmp_path):
        _seed_book(db, "B0SINGLE12", "DOWNLOADED", str(tmp_path / "data" / "gone.m4b"))

        _drain(sync_logic._reconcile_database(1, {}))

        row = _book(db, "B0SINGLE12")
        assert row["status"] == "MISSING"
        assert row["filepath"] == ""

    def test_single_file_book_still_on_disk_is_untouched(self, db, tmp_path):
        path = _make_audio(tmp_path / "data" / "Book.m4b")
        _seed_book(db, "B0SINGLE12", "DOWNLOADED", path)

        _drain(sync_logic._reconcile_database(1, {}))

        row = _book(db, "B0SINGLE12")
        assert row["status"] == "DOWNLOADED"
        assert row["filepath"] == path

    def test_empty_filepath_is_marked_missing(self, db):
        _seed_book(db, "B0EMPTY123", "DOWNLOADED", "")

        _drain(sync_logic._reconcile_database(1, {}))

        assert _book(db, "B0EMPTY123")["status"] == "MISSING"


class TestReconcileRepoint:
    def test_split_book_is_restored_to_its_folder_never_to_a_part(self, db, tmp_path):
        folder = tmp_path / "data" / "Split Book"
        parts = [_make_audio(folder / f"{n:02d}.m4b") for n in (1, 2, 3)]
        _seed_book(db, "B0SPLIT123", "MISSING", "", parts=parts)

        _drain(sync_logic._reconcile_database(1, {"B0SPLIT123": parts}))

        row = _book(db, "B0SPLIT123")
        assert row["status"] == "DOWNLOADED"
        assert row["filepath"] == str(folder)  # the folder, NOT parts[0]
        assert row["filepath"] not in parts

    def test_split_book_with_an_incomplete_part_set_is_left_alone(self, db, tmp_path):
        folder = tmp_path / "data" / "Split Book"
        kept = _make_audio(folder / "01.m4b")
        gone = str(folder / "02.m4b")
        _seed_book(db, "B0SPLIT123", "MISSING", "", parts=[kept, gone])

        # The scan found the surviving part; that must NOT be enough to restore
        # the book, and above all must not become its filepath. Nor can it be
        # read as a relocation — one file found against two tracked parts is not
        # the same book somewhere else.
        _drain(sync_logic._reconcile_database(1, {"B0SPLIT123": [kept]}))

        row = _book(db, "B0SPLIT123")
        assert row["status"] == "MISSING"
        assert row["filepath"] == ""

    def test_untracked_single_file_is_repointed_as_before(self, db, tmp_path):
        path = _make_audio(tmp_path / "data" / "Book.m4b")
        _seed_book(db, "B0SINGLE12", "NEW", "")

        _drain(sync_logic._reconcile_database(1, {"B0SINGLE12": [path]}))

        row = _book(db, "B0SINGLE12")
        assert row["status"] == "DOWNLOADED"
        assert row["filepath"] == path

    def test_multiple_files_without_part_rows_pick_the_first_listed(self, db, tmp_path):
        # An external multi-file folder the app didn't produce (no `book_files`
        # rows). Adopting whole folders is out of scope this release, so the
        # behavior stays single-file and reproducible (the scan sorts its files).
        folder = tmp_path / "data" / "Someone Elses Split"
        first = _make_audio(folder / "01.m4b")
        second = _make_audio(folder / "02.m4b")
        _seed_book(db, "B0EXTERN12", "NEW", "")

        _drain(sync_logic._reconcile_database(1, {"B0EXTERN12": [first, second]}))

        assert _book(db, "B0EXTERN12")["filepath"] == first

    def test_already_downloaded_book_is_not_repointed(self, db, tmp_path):
        path = _make_audio(tmp_path / "data" / "Book.m4b")
        other = _make_audio(tmp_path / "data" / "Copy.m4b")
        _seed_book(db, "B0SINGLE12", "DOWNLOADED", path)

        _drain(sync_logic._reconcile_database(1, {"B0SINGLE12": [other]}))

        assert _book(db, "B0SINGLE12")["filepath"] == path

    def test_repoint_resets_the_auto_retry_counter(self, db, tmp_path):
        # Adoption is a success write, so it must clear retry_count exactly as the
        # downloader's own success write does. A book left at retry_count 2 by two
        # failed downloads, then supplied by hand, would otherwise sit permanently
        # past the `retry_count <= 1` auto-retry gate the next time Verify flagged it.
        path = _make_audio(tmp_path / "data" / "Book.m4b")
        _seed_book(db, "B0SINGLE12", "MISSING", "", retry_count=2)

        _drain(sync_logic._reconcile_database(1, {"B0SINGLE12": [path]}))

        row = _book(db, "B0SINGLE12")
        assert row["status"] == "DOWNLOADED"
        assert row["retry_count"] == 0

    def test_split_book_restore_resets_the_auto_retry_counter(self, db, tmp_path):
        folder = tmp_path / "data" / "Split Book"
        parts = [_make_audio(folder / f"{n:02d}.m4b") for n in (1, 2)]
        _seed_book(db, "B0SPLIT123", "MISSING", "", parts=parts, retry_count=3)

        _drain(sync_logic._reconcile_database(1, {"B0SPLIT123": parts}))

        row = _book(db, "B0SPLIT123")
        assert row["status"] == "DOWNLOADED"
        assert row["retry_count"] == 0

    def test_unknown_asin_on_disk_creates_no_row(self, db, tmp_path):
        path = _make_audio(tmp_path / "data" / "Stranger.m4b")

        _drain(sync_logic._reconcile_database(1, {"B0UNKNOWN1": [path]}))

        con = sqlite3.connect(db)
        assert con.execute("SELECT COUNT(*) FROM audiobooks").fetchone()[0] == 0
        con.close()

    def test_reconcile_survives_a_database_without_the_child_table(self, tmp_path, monkeypatch):
        # A hand-restored library.db can predate the `book_files` migration; the
        # sync must degrade to single-file behavior rather than failing.
        db_path = tmp_path / "legacy.db"
        con = sqlite3.connect(db_path)
        con.execute("CREATE TABLE audiobooks (asin TEXT PRIMARY KEY, title TEXT, status TEXT, filepath TEXT)")
        con.execute(
            "INSERT INTO audiobooks VALUES ('B0LEGACY12', 'Legacy', 'DOWNLOADED', ?)",
            (str(tmp_path / "gone.m4b"),),
        )
        con.commit()
        con.close()
        monkeypatch.setattr(db_module, "DB_FILE", str(db_path))

        _drain(sync_logic._reconcile_database(1, {}))

        assert _book(db_path, "B0LEGACY12")["status"] == "MISSING"


class TestReconcileRestoresMovedSplitBooks:
    """A split book whose folder the user MOVED in a file manager: every tracked
    path is gone, but the scan positively identified all N parts at their new
    home. Single-file books have always been repointed in exactly this situation
    — restoring the split book is the same self-healing, just with the folder and
    the part rows rewritten together."""

    def _moved_book(self, db, tmp_path, names, *, tracked_order=None):
        """
        Seed a split book whose parts are recorded under `old/` but actually sit
        under `new/`. `tracked_order` (defaulting to `names`) fixes the part_index
        order of the DB rows, so a test can make it disagree with alphabetical
        order and prove the mapping is by NAME, not by position or sort.
        """
        old_folder = tmp_path / "data" / "Old Place" / "Book"
        new_folder = tmp_path / "data" / "New Place" / "Book"
        tracked = [str(old_folder / name) for name in (tracked_order or names)]
        found = [_make_audio(new_folder / name) for name in names]
        _seed_book(db, "B0SPLIT123", "MISSING", "", parts=tracked)
        return str(new_folder), tracked, found

    def test_all_parts_found_at_a_new_location_restores_the_book(self, db, tmp_path):
        new_folder, _tracked, found = self._moved_book(db, tmp_path, ["01.m4b", "02.m4b", "03.m4b"])

        _drain(sync_logic._reconcile_database(1, {"B0SPLIT123": found}))

        row = _book(db, "B0SPLIT123")
        assert row["status"] == "DOWNLOADED"
        assert row["filepath"] == new_folder  # the new folder, never a part
        assert _parts(db, "B0SPLIT123") == found

    def test_the_restore_maps_parts_by_name_so_playback_order_survives(self, db, tmp_path):
        # part_index order (03, 01, 02) deliberately differs from alphabetical
        # order, so a restore that used the scan's order or a plain sort would
        # silently renumber the book's chapters.
        names = ["01.m4b", "02.m4b", "03.m4b"]
        order = ["03.m4b", "01.m4b", "02.m4b"]
        new_folder, _tracked, found = self._moved_book(db, tmp_path, names, tracked_order=order)

        _drain(sync_logic._reconcile_database(1, {"B0SPLIT123": found}))

        assert _parts(db, "B0SPLIT123") == [
            str(tmp_path / "data" / "New Place" / "Book" / name) for name in ("03.m4b", "01.m4b", "02.m4b")
        ]
        assert _book(db, "B0SPLIT123")["filepath"] == new_folder

    def test_a_different_set_of_names_is_not_a_relocation(self, db, tmp_path):
        # Same count, same single folder, but the names don't match — this is some
        # other multi-file book carrying the ASIN, not ours moved.
        old_folder = tmp_path / "data" / "Old" / "Book"
        tracked = [str(old_folder / name) for name in ("01.m4b", "02.m4b")]
        found = [_make_audio(tmp_path / "data" / "New" / "Book" / name) for name in ("aa.m4b", "bb.m4b")]
        _seed_book(db, "B0SPLIT123", "MISSING", "", parts=tracked)

        _drain(sync_logic._reconcile_database(1, {"B0SPLIT123": found}))

        row = _book(db, "B0SPLIT123")
        assert row["status"] == "MISSING"
        assert row["filepath"] == ""
        assert _parts(db, "B0SPLIT123") == tracked  # rows untouched

    def test_two_copies_of_the_book_are_not_a_relocation(self, db, tmp_path):
        # The folder was COPIED, not moved (or an old copy lingers): 2N files
        # carry the ASIN, and there is no single right answer, so we do nothing.
        names = ["01.m4b", "02.m4b"]
        old_folder = tmp_path / "data" / "Old" / "Book"
        tracked = [str(old_folder / name) for name in names]
        found = [_make_audio(tmp_path / "data" / "Copy A" / name) for name in names]
        found += [_make_audio(tmp_path / "data" / "Copy B" / name) for name in names]
        _seed_book(db, "B0SPLIT123", "MISSING", "", parts=tracked)

        _drain(sync_logic._reconcile_database(1, {"B0SPLIT123": found}))

        assert _book(db, "B0SPLIT123")["status"] == "MISSING"
        assert _parts(db, "B0SPLIT123") == tracked

    def test_parts_scattered_across_folders_are_not_a_relocation(self, db, tmp_path):
        names = ["01.m4b", "02.m4b"]
        tracked = [str(tmp_path / "data" / "Old" / "Book" / name) for name in names]
        found = [
            _make_audio(tmp_path / "data" / "Here" / "01.m4b"),
            _make_audio(tmp_path / "data" / "There" / "02.m4b"),
        ]
        _seed_book(db, "B0SPLIT123", "MISSING", "", parts=tracked)

        _drain(sync_logic._reconcile_database(1, {"B0SPLIT123": found}))

        assert _book(db, "B0SPLIT123")["status"] == "MISSING"
        assert _parts(db, "B0SPLIT123") == tracked

    def test_a_moved_book_survives_the_missing_pass_in_the_same_sync(self, db, tmp_path):
        # End-to-end shape of one deep sync over a moved folder: loop 1 demotes the
        # book to MISSING (its tracked paths are gone), loop 2 restores it from the
        # scan results — so a single sync leaves it DOWNLOADED at the new folder.
        new_folder, _tracked, found = self._moved_book(db, tmp_path, ["01.m4b", "02.m4b"])
        con = sqlite3.connect(db)
        con.execute("UPDATE audiobooks SET status = 'DOWNLOADED', filepath = ? WHERE asin = 'B0SPLIT123'", ("/gone",))
        con.commit()
        con.close()

        _drain(sync_logic._reconcile_database(1, {"B0SPLIT123": found}))

        row = _book(db, "B0SPLIT123")
        assert row["status"] == "DOWNLOADED"
        assert row["filepath"] == new_folder
        assert _parts(db, "B0SPLIT123") == found


class TestBookFolderFromParts:
    def test_parts_in_one_folder_yield_that_folder(self):
        assert sync_logic._book_folder_from_parts(["/data/A/Book/01.m4b", "/data/A/Book/02.m4b"]) == "/data/A/Book"

    def test_no_parts_yields_an_empty_string(self):
        # Defensive: the helper must not raise for the next caller that forgets
        # to check emptiness first.
        assert sync_logic._book_folder_from_parts([]) == ""

    def test_nested_folders_yield_the_shallowest(self):
        parts = ["/data/A/Book/01.m4b", "/data/A/Book/extra/02.m4b"]
        assert sync_logic._book_folder_from_parts(parts) == "/data/A/Book"

    def test_sibling_folders_never_yield_their_shared_parent(self):
        # A common ancestor would be a directory holding OTHER books, which the
        # rename and the detail view would then treat as this book's folder.
        parts = ["/data/Author/Book A/01.m4b", "/data/Author/Book B/01.m4b"]
        assert sync_logic._book_folder_from_parts(parts) == "/data/Author/Book A"

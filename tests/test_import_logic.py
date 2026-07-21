# tests/test_import_logic.py
#
# Phase 6 (FR2): the shared adoption core, import_logic.adopt_file, plus the
# untracked-file scan. Uses a real temp SQLite DB (monkeypatched DB_FILE) and
# mocks only the ffprobe/ffmpeg helpers so the identity/idempotency logic is
# exercised end-to-end.

import os
import sqlite3
from unittest import mock

import pytest

from audible_downloader import db as db_module
from audible_downloader import import_logic

# Mirrors the columns adopt_file reads/writes (a subset of the production schema).
_SCHEMA = (
    "CREATE TABLE audiobooks ("
    "asin TEXT PRIMARY KEY, author TEXT, title TEXT, status TEXT, series TEXT, narrator TEXT, "
    "runtime_min INTEGER, release_date TEXT, filepath TEXT, publisher TEXT, language TEXT, "
    "purchase_date TEXT, summary TEXT, date_added TEXT, source TEXT DEFAULT 'audible')"
)


@pytest.fixture
def db(tmp_path, monkeypatch):
    """A temp library.db with the import-relevant schema; adopt_file writes here."""
    db_path = tmp_path / "library.db"
    con = sqlite3.connect(db_path)
    con.execute(_SCHEMA)
    con.commit()
    con.close()
    monkeypatch.setattr(db_module, "DB_FILE", str(db_path))
    return db_path


def _seed(db, **cols):
    con = sqlite3.connect(db)
    keys = ", ".join(cols)
    marks = ", ".join("?" for _ in cols)
    con.execute(f"INSERT INTO audiobooks ({keys}) VALUES ({marks})", tuple(cols.values()))
    con.commit()
    con.close()


def _rows(db):
    con = sqlite3.connect(db)
    con.row_factory = sqlite3.Row
    rows = [dict(r) for r in con.execute("SELECT * FROM audiobooks")]
    con.close()
    return rows


def _patch_meta(**over):
    """Patch _probe_metadata (and stub cover extraction) with the given fields."""
    meta = {"embedded_asin": None, "title": None, "author": None, "release_date": None, "runtime_min": 0}
    meta.update(over)
    return (
        mock.patch.object(import_logic, "_probe_metadata", return_value=meta),
        mock.patch.object(import_logic, "_extract_cover"),
    )


def _adopt(db, path, tmp_path, *, allow_reconcile=True, **meta):
    """Create a real file at `path` under tmp_path and adopt it with mocked metadata."""
    full = tmp_path / path
    full.parent.mkdir(parents=True, exist_ok=True)
    full.write_bytes(b"x")
    m_meta, m_cover = _patch_meta(**meta)
    with m_meta, m_cover as cover:
        result = import_logic.adopt_file(str(full), allow_reconcile=allow_reconcile)
    return result, str(full), cover


class TestAdoptFile:
    def test_untagged_file_is_imported_with_synthetic_key(self, db, tmp_path):
        result, path, cover = _adopt(db, "book.m4b", tmp_path, title="My Book", author="Someone", runtime_min=120)
        assert result["action"] == "imported"
        assert result["key"].startswith("IMPORT-")
        rows = _rows(db)
        assert len(rows) == 1
        assert rows[0]["title"] == "My Book"
        assert rows[0]["author"] == "Someone"
        assert rows[0]["status"] == "DOWNLOADED"
        assert rows[0]["source"] == "imported"
        assert rows[0]["filepath"] == path
        assert rows[0]["runtime_min"] == 120
        cover.assert_called_once()

    def test_title_falls_back_to_filename_and_author_to_unknown(self, db, tmp_path):
        result, _path, _cover = _adopt(db, "Nice Name.m4b", tmp_path)
        assert result["title"] == "Nice Name"
        assert result["author"] == "Unknown Author"

    def test_tagged_unknown_asin_uses_that_asin_as_key(self, db, tmp_path):
        result, _path, _cover = _adopt(db, "b.m4b", tmp_path, embedded_asin="B0NEW12345", title="Tagged")
        assert result["action"] == "imported"
        assert result["key"] == "B0NEW12345"
        assert _rows(db)[0]["source"] == "imported"

    def test_tagged_known_asin_is_reconciled_in_place(self, db, tmp_path):
        _seed(db, asin="B0KNOWN123", title="Known", author="A", status="MISSING", filepath="", source="audible")
        result, path, cover = _adopt(db, "known.m4b", tmp_path, embedded_asin="B0KNOWN123")
        assert result["action"] == "reconciled"
        assert result["key"] == "B0KNOWN123"
        row = _rows(db)[0]
        assert row["status"] == "DOWNLOADED"
        assert row["filepath"] == path
        assert row["source"] == "audible"  # provenance is untouched by reconcile
        cover.assert_not_called()  # reconcile does not re-extract a cover

    def test_reconcile_skipped_when_asin_already_linked_to_live_file(self, db, tmp_path):
        # A book already DOWNLOADED to a real file on disk must NOT be repointed
        # by a stray/hostile copy carrying the same embedded ASIN (H1: no clobber,
        # no hijack). The original filepath is preserved and no row is duplicated.
        existing = tmp_path / "canonical.m4b"
        existing.write_bytes(b"real")
        _seed(db, asin="B0KNOWN123", title="Known", status="DOWNLOADED", filepath=str(existing), source="audible")
        result, _path, cover = _adopt(db, "stray.m4b", tmp_path, embedded_asin="B0KNOWN123")
        assert result["action"] == "skipped"
        assert result["reason"] == "asin-already-linked"
        rows = _rows(db)
        assert len(rows) == 1
        assert rows[0]["filepath"] == str(existing)  # untouched
        assert rows[0]["source"] == "audible"
        cover.assert_not_called()

    def test_reconcile_proceeds_when_linked_file_is_gone(self, db, tmp_path):
        # If the tracked file no longer exists, the row IS the legitimate reconcile
        # target and gets repointed at the found file.
        _seed(db, asin="B0KNOWN123", title="Known", status="DOWNLOADED", filepath="/data/gone.m4b", source="audible")
        result, path, _cover = _adopt(db, "found.m4b", tmp_path, embedded_asin="B0KNOWN123")
        assert result["action"] == "reconciled"
        assert _rows(db)[0]["filepath"] == path

    def test_duplicate_asin_files_do_not_flip_flop(self, db, tmp_path):
        # Two distinct files under /data carrying the same ASIN: the first is
        # reconciled, the second is skipped, and re-adopting either is stable — the
        # filepath never oscillates between them across repeated scans.
        _seed(db, asin="B0DUP000000", title="Dup", status="MISSING", filepath="", source="audible")
        r1, path_a, _c = _adopt(db, "a/dup.m4b", tmp_path, embedded_asin="B0DUP000000")
        assert r1["action"] == "reconciled"
        assert _rows(db)[0]["filepath"] == path_a

        r2, _path_b, _c = _adopt(db, "b/dup.m4b", tmp_path, embedded_asin="B0DUP000000")
        assert r2["action"] == "skipped"
        assert r2["reason"] == "asin-already-linked"
        assert _rows(db)[0]["filepath"] == path_a  # still A, not flipped to B

        # Re-adopting A is the idempotent same-file case (still A).
        m_meta, m_cover = _patch_meta(embedded_asin="B0DUP000000")
        with m_meta, m_cover:
            r3 = import_logic.adopt_file(path_a)
        assert r3["action"] == "reconciled"
        assert _rows(db)[0]["filepath"] == path_a

    def test_allow_reconcile_false_forces_import_of_known_asin(self, db, tmp_path):
        _seed(db, asin="B0KNOWN123", title="Known", author="A", status="DOWNLOADED", filepath="/data/x.m4b")
        # Same ASIN key already exists -> the import branch UPDATEs rather than duplicating.
        result, path, _cover = _adopt(db, "k.m4b", tmp_path, allow_reconcile=False, embedded_asin="B0KNOWN123")
        assert result["key"] == "B0KNOWN123"
        rows = _rows(db)
        assert len(rows) == 1
        assert rows[0]["filepath"] == path
        assert rows[0]["source"] == "imported"

    def test_already_tracked_path_is_skipped(self, db, tmp_path):
        full = tmp_path / "dup.m4b"
        full.write_bytes(b"x")
        _seed(db, asin="IMPORT-abc", title="T", filepath=str(full), source="imported")
        m_meta, m_cover = _patch_meta()
        with m_meta, m_cover:
            result = import_logic.adopt_file(str(full))
        assert result["action"] == "skipped"
        assert result["reason"] == "already-tracked"
        assert result["key"] == "IMPORT-abc"
        assert len(_rows(db)) == 1  # no duplicate

    def test_rescan_is_idempotent(self, db, tmp_path):
        r1, _p, _c = _adopt(db, "book.m4b", tmp_path, title="Once")
        assert r1["action"] == "imported"
        # Second pass on the same path must not create a second row.
        m_meta, m_cover = _patch_meta(title="Once")
        with m_meta, m_cover:
            r2 = import_logic.adopt_file(os.path.join(str(tmp_path), "book.m4b"))
        assert r2["action"] == "skipped"
        assert len(_rows(db)) == 1

    def test_unsupported_extension_is_skipped(self, db, tmp_path):
        full = tmp_path / "song.mp3"
        full.write_bytes(b"x")
        result = import_logic.adopt_file(str(full))
        assert result["action"] == "skipped"
        assert result["reason"] == "unsupported-type"
        assert _rows(db) == []

    def test_missing_file_is_skipped(self, db, tmp_path):
        result = import_logic.adopt_file(str(tmp_path / "gone.m4b"))
        assert result["action"] == "skipped"
        assert result["reason"] == "not-found"

    def test_m4a_is_accepted(self, db, tmp_path):
        result, _path, _cover = _adopt(db, "book.m4a", tmp_path, title="AAC Book")
        assert result["action"] == "imported"

    def test_malicious_embedded_asin_uses_synthetic_key(self, db, tmp_path):
        # Full-stack: the real _probe_metadata must reject a hostile asin tag so
        # a crafted file can never drive the key (which feeds filesystem paths).
        full = tmp_path / "evil.m4b"
        full.write_bytes(b"x")
        raw = {"format": {"tags": {"asin": "../../config/x", "title": "T"}, "duration": "60"}}
        with (
            mock.patch.object(import_logic, "_run_ffprobe_json", return_value=raw),
            mock.patch.object(import_logic, "_extract_cover"),
        ):
            result = import_logic.adopt_file(str(full))
        assert result["key"].startswith("IMPORT-")
        assert _rows(db)[0]["asin"].startswith("IMPORT-")


class TestMetadataSanitization:
    """The embedded `asin` tag is untrusted file content; _probe_metadata only
    accepts a strict 10-char [A-Z0-9] ASIN and drops anything else."""

    def _probe(self, asin):
        raw = {"format": {"tags": {"asin": asin}, "duration": "60"}}
        with mock.patch.object(import_logic, "_run_ffprobe_json", return_value=raw):
            return import_logic._probe_metadata("/whatever.m4b")["embedded_asin"]

    def test_valid_asin_is_kept(self):
        assert self._probe("B0NEW12345") == "B0NEW12345"

    def test_path_traversal_asin_is_dropped(self):
        assert self._probe("../../config/x") is None

    def test_slash_asin_is_dropped(self):
        assert self._probe("B0/NEW/1234") is None

    def test_short_asin_is_dropped(self):
        assert self._probe("B0KNOWN") is None

    def test_lowercase_asin_is_dropped(self):
        assert self._probe("b0new12345") is None


class TestCoverKeyGuard:
    """_extract_cover is the last line of defense: even a bad key must not write
    outside COVERS_DIR."""

    def test_unsafe_key_bails_before_invoking_ffmpeg(self, tmp_path, monkeypatch):
        monkeypatch.setattr(import_logic, "COVERS_DIR", str(tmp_path))
        ran = []
        monkeypatch.setattr(
            import_logic.subprocess, "run", lambda *a, **k: ran.append(a) or mock.MagicMock(returncode=0)
        )
        import_logic._extract_cover("/some/file.m4b", "../evil")
        assert ran == []  # returned before touching ffmpeg / the disk


class TestAdoptUpload:
    """adopt_upload places a staged file via the naming template, then adopts it.
    build_base_output_path hard-codes /data, so it's patched to a temp path here
    (adopt_upload imports it lazily, so patching the source module suffices)."""

    def test_places_by_template_and_adopts_with_matching_key(self, db, tmp_path):
        staging = tmp_path / "stage.m4b"
        staging.write_bytes(b"x")
        target = str(tmp_path / "lib" / "Jane Doe" / "Great Book.m4b")

        m_meta, m_cover = _patch_meta(title="Great Book", author="Jane Doe")
        with (
            m_meta,
            m_cover,
            mock.patch("audible_downloader.processing_logic.build_base_output_path", return_value=target),
        ):
            result = import_logic.adopt_upload(str(staging), "orig.m4b", {})

        assert result["action"] == "imported"
        assert result["key"].startswith("IMPORT-")
        assert result["filepath"] == target
        assert not staging.exists()  # moved out of staging
        assert os.path.exists(target)
        row = _rows(db)[0]
        assert row["asin"] == result["key"]  # DB key matches the placed file's key
        assert row["title"] == "Great Book"
        assert row["source"] == "imported"

    def test_collision_appends_key_instead_of_overwriting(self, db, tmp_path):
        # Pre-create the file the template resolves to, owned by another book.
        target = tmp_path / "lib" / "Great Book.m4b"
        target.parent.mkdir(parents=True)
        target.write_bytes(b"existing")

        staging = tmp_path / "stage.m4b"
        staging.write_bytes(b"x")
        m_meta, m_cover = _patch_meta(title="Great Book", author="Jane Doe")
        with (
            m_meta,
            m_cover,
            mock.patch("audible_downloader.processing_logic.build_base_output_path", return_value=str(target)),
        ):
            result = import_logic.adopt_upload(str(staging), "orig.m4b", {})

        # Landed at a suffixed path, leaving the pre-existing file untouched.
        assert result["filepath"] != str(target)
        assert result["key"] in result["filepath"]
        assert target.read_bytes() == b"existing"

    def test_duplicate_upload_is_skipped_and_orphan_removed(self, db, tmp_path):
        # Uploading a file whose ASIN matches a book already linked to a live file
        # must not clobber the row (H1); adopt_upload also removes the redundant
        # copy it placed so no untracked orphan is left under /data.
        existing = tmp_path / "canonical.m4b"
        existing.write_bytes(b"real")
        _seed(db, asin="B0KNOWN123", title="Known", status="DOWNLOADED", filepath=str(existing), source="audible")

        staging = tmp_path / "stage.m4b"
        staging.write_bytes(b"x")
        target = str(tmp_path / "lib" / "Known.m4b")
        m_meta, m_cover = _patch_meta(embedded_asin="B0KNOWN123", title="Known")
        with (
            m_meta,
            m_cover,
            mock.patch("audible_downloader.processing_logic.build_base_output_path", return_value=target),
        ):
            result = import_logic.adopt_upload(str(staging), "orig.m4b", {})

        assert result["action"] == "skipped"
        assert result["reason"] == "asin-already-linked"
        assert result["filepath"] is None
        assert not os.path.exists(target)  # orphan removed
        rows = _rows(db)
        assert len(rows) == 1
        assert rows[0]["filepath"] == str(existing)  # canonical row untouched


class TestScanUntracked:
    def test_returns_only_untracked_importable_files(self, db, tmp_path, monkeypatch):
        data = tmp_path / "data"
        (data / "A").mkdir(parents=True)
        tracked = data / "A" / "tracked.m4b"
        untracked = data / "A" / "new.m4b"
        other = data / "A" / "notes.txt"
        for f in (tracked, untracked, other):
            f.write_bytes(b"x")
        staged = data / import_logic.IMPORT_STAGING_DIRNAME / "wip.m4b"
        staged.parent.mkdir(parents=True)
        staged.write_bytes(b"x")

        _seed(db, asin="B1", title="T", filepath=str(tracked))
        monkeypatch.setattr(import_logic, "DATA_DIR", str(data))

        found = import_logic.scan_data_dir_for_untracked()
        assert found == [str(untracked)]  # tracked, non-audio, and staged files excluded

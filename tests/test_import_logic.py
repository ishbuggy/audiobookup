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
    meta = {
        "embedded_asin": None,
        "title": None,
        "author": None,
        "release_date": None,
        "runtime_min": 0,
        # Default to "probed as valid media"; the unprobeable/junk cases override.
        "probe_ok": True,
    }
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

    def test_zero_byte_file_is_skipped_on_scan(self, db, tmp_path):
        # L11: a 0-byte file under /data must not become a junk row. The size guard
        # fires regardless of the probe result (probe_ok is True by default here).
        full = tmp_path / "empty.m4b"
        full.write_bytes(b"")
        m_meta, m_cover = _patch_meta()
        with m_meta, m_cover:
            result = import_logic.adopt_file(str(full))
        assert result["action"] == "skipped"
        assert result["reason"] == "unreadable-media"
        assert _rows(db) == []

    def test_unprobeable_file_is_skipped_on_scan(self, db, tmp_path):
        # L11: non-audio content that merely carries an importable extension yields
        # no ffprobe `format` (probe_ok False) and must be skipped, not adopted.
        full = tmp_path / "junk.m4b"
        full.write_bytes(b"not really audio")
        with (
            mock.patch.object(import_logic, "_run_ffprobe_json", return_value={}),
            mock.patch.object(import_logic, "_extract_cover"),
        ):
            result = import_logic.adopt_file(str(full))
        assert result["action"] == "skipped"
        assert result["reason"] == "unreadable-media"
        assert _rows(db) == []

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
        monkeypatch.setattr(import_logic.subprocess, "Popen", lambda *a, **k: ran.append(a) or mock.MagicMock())
        import_logic._extract_cover("/some/file.m4b", "../evil")
        assert ran == []  # returned before touching ffmpeg / the disk


class TestSubprocessRegistration:
    """M5 regression: import ffprobe/ffmpeg calls register with process_registry
    (via _run_registered) so a job cancel can SIGTERM them; job_id is threaded
    from the import worker through adopt_file down to the probe/cover helpers."""

    def test_run_registered_registers_and_unregisters(self, monkeypatch):
        proc = mock.MagicMock()
        proc.communicate.return_value = ("{}", "")
        proc.returncode = 0
        monkeypatch.setattr(import_logic.subprocess, "Popen", lambda *a, **k: proc)
        registered, unregistered = [], []
        monkeypatch.setattr(import_logic.process_registry, "register", lambda j, p: registered.append((j, p)))
        monkeypatch.setattr(import_logic.process_registry, "unregister", lambda j, p: unregistered.append((j, p)))

        import_logic._run_ffprobe_json("/some/file.m4b", job_id=7)
        assert registered == [(7, proc)]
        assert unregistered == [(7, proc)]

    def test_job_id_reaches_the_probe(self, db, tmp_path, monkeypatch):
        # adopt_file(..., job_id=N) must forward N to _probe_metadata so the
        # underlying ffprobe is registered under the job.
        full = tmp_path / "book.m4b"
        full.write_bytes(b"x")
        seen = {}
        meta = {"embedded_asin": None, "title": "T", "author": None, "release_date": None, "runtime_min": 0}

        def fake_probe(filepath, job_id=None):
            seen["job_id"] = job_id
            return meta

        monkeypatch.setattr(import_logic, "_probe_metadata", fake_probe)
        monkeypatch.setattr(import_logic, "_extract_cover", lambda *a, **k: None)
        import_logic.adopt_file(str(full), job_id=42)
        assert seen["job_id"] == 42


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

    def test_collision_with_a_sibling_extension_walks_to_the_next_candidate(self, db, tmp_path):
        # Backlog #17 regression: the template name is free *as a filename* (the
        # upload is an .m4a and the existing book is an .m4b) but NOT as a base —
        # both would hang their cover/PDF/cue/metadata off "lib/Great Book". The
        # allocator must treat the occupied base as a collision and suffix the key,
        # exactly as it would for a same-extension clash.
        existing = tmp_path / "lib" / "Great Book.m4b"
        existing.parent.mkdir(parents=True)
        existing.write_bytes(b"other-book")
        target = tmp_path / "lib" / "Great Book.m4a"

        staging = tmp_path / "stage.m4a"
        staging.write_bytes(b"uploaded")
        m_meta, m_cover = _patch_meta(title="Great Book", author="Jane Doe")
        with (
            m_meta,
            m_cover,
            mock.patch("audible_downloader.processing_logic.build_base_output_path", return_value=str(target)),
        ):
            result = import_logic.adopt_upload(str(staging), "orig.m4a", {})

        # Landed at a suffixed path, so the two books no longer share a base...
        assert result["filepath"] != str(target)
        assert result["key"] in result["filepath"]
        assert result["filepath"].endswith(".m4a")  # container extension preserved
        # ...and nothing was placed at the contested base under either extension.
        assert not target.exists()
        assert existing.read_bytes() == b"other-book"

    def test_upload_does_not_overwrite_a_linked_duplicate_at_the_suffixed_path(self, db, tmp_path):
        # H1 (blocking regression): the "_<ASIN>" name is exactly where a duplicate's
        # own prior download lives. Uploading a file carrying that ASIN when BOTH the
        # template name AND the "_<ASIN>" name are already occupied must NOT clobber
        # the linked duplicate's real file. adopt_upload walks to a further-suffixed
        # free path; adopt_file then declines to repoint the linked row and the
        # redundant copy is removed. A single unchecked suffix would overwrite `dup`.
        lib = tmp_path / "lib"
        lib.mkdir(parents=True)
        # The primary edition occupies the plain template name...
        primary = lib / "Known.m4b"
        primary.write_bytes(b"primary")
        # ...and the duplicate's real download occupies the "_<ASIN>" name.
        dup = lib / "Known_B0DUP123456.m4b"
        dup.write_bytes(b"real-dup")
        _seed(db, asin="B0DUP123456", title="Known", status="DOWNLOADED", filepath=str(dup), source="audible")

        staging = tmp_path / "stage.m4b"
        staging.write_bytes(b"uploaded")
        m_meta, m_cover = _patch_meta(embedded_asin="B0DUP123456", title="Known")
        with (
            m_meta,
            m_cover,
            mock.patch("audible_downloader.processing_logic.build_base_output_path", return_value=str(primary)),
        ):
            result = import_logic.adopt_upload(str(staging), "orig.m4b", {})

        # The linked duplicate's real file is intact — not overwritten by the upload.
        assert dup.read_bytes() == b"real-dup"
        assert primary.read_bytes() == b"primary"
        # adopt_file refused to repoint the linked row; the redundant copy is gone.
        assert result["action"] == "skipped"
        assert result["reason"] == "asin-already-linked"
        assert result["filepath"] is None
        assert not os.path.exists(lib / "Known_B0DUP123456_2.m4b")
        rows = _rows(db)
        assert len(rows) == 1
        assert rows[0]["filepath"] == str(dup)  # canonical row untouched

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

    def test_unreadable_upload_is_rejected_before_placement(self, db, tmp_path):
        # WF#6: a non-empty but unprobeable upload (renamed junk — importable
        # extension over non-audio bytes) must be rejected before anything is
        # placed under /data, not adopted as a bogus DOWNLOADED row. The staging
        # file is left unmoved for the endpoint to clean up.
        staging = tmp_path / "stage.m4b"
        staging.write_bytes(b"not really audio")
        m_meta, m_cover = _patch_meta(probe_ok=False)
        with (
            m_meta,
            m_cover,
            mock.patch("audible_downloader.processing_logic.build_base_output_path") as build,
        ):
            result = import_logic.adopt_upload(str(staging), "orig.m4b", {})

        assert result["action"] == "skipped"
        assert result["reason"] == "unreadable-media"
        assert result["filepath"] is None
        build.assert_not_called()  # bailed before computing a destination
        assert staging.exists()  # left unmoved for the endpoint to remove
        assert _rows(db) == []  # no DB row created

    def test_year_segment_comes_from_the_probed_release_date(self, db, tmp_path):
        # Backlog #7 regression: adopt_upload must hand the naming engine the same
        # values it stores on the DB row. With only the positional arguments, a
        # "{year}" template rendered an empty segment at import time but a filled
        # one on the next metadata edit — so apply_custom_to_filenames would move
        # the freshly imported book the first time it was edited.
        from audible_downloader import processing_logic

        real_build = processing_logic.build_base_output_path
        settings = {"naming": {"template": "{author}/{year}/{author} - {title}"}}

        def build_under_tmp(*args, **kwargs):
            # build_base_output_path hard-codes the /data root; re-root its result at
            # tmp_path so the real template rendering is exercised without the test
            # writing to the host filesystem.
            return str(tmp_path / "lib") + real_build(*args, **kwargs)[len("/data") :]

        staging = tmp_path / "stage.m4b"
        staging.write_bytes(b"x")
        m_meta, m_cover = _patch_meta(title="Great Book", author="Jane Doe", release_date="2011-05-04")
        with (
            m_meta,
            m_cover,
            mock.patch("audible_downloader.processing_logic.build_base_output_path", side_effect=build_under_tmp),
        ):
            result = import_logic.adopt_upload(str(staging), "orig.m4b", settings)

        assert result["filepath"] == str(tmp_path / "lib" / "Jane Doe" / "2011" / "Jane Doe - Great Book.m4b")
        # ...and the placed path agrees with the date actually written to the row.
        assert _rows(db)[0]["release_date"] == "2011-05-04"

    def test_upload_preserves_m4a_extension(self, db, tmp_path):
        # L8: an uploaded .m4a keeps its real container extension instead of being
        # stored as .m4b — adopt_upload passes the staged file's ext through to
        # build_base_output_path.
        staging = tmp_path / "stage.m4a"
        staging.write_bytes(b"x")
        target = str(tmp_path / "lib" / "Book.m4a")
        captured = {}

        def fake_build(settings, key, author, title, narrator, publisher, ext=".m4b", **tags):
            captured["ext"] = ext
            return target

        m_meta, m_cover = _patch_meta(title="Book", author="A")
        with (
            m_meta,
            m_cover,
            mock.patch("audible_downloader.processing_logic.build_base_output_path", side_effect=fake_build),
        ):
            result = import_logic.adopt_upload(str(staging), "orig.m4a", {})

        assert captured["ext"] == ".m4a"
        assert result["filepath"] == target


class TestFirstFreeOutputPath:
    """The upload allocator's collision walk (backlog #17): candidates are judged
    on the extension-stripped base, since that base owns the sidecar set."""

    def test_untouched_base_is_returned_unchanged(self, tmp_path):
        target = str(tmp_path / "Book.m4b")
        assert import_logic._first_free_output_path(target, "B0KEY12345") == target

    def test_same_extension_collision_appends_the_key(self, tmp_path):
        (tmp_path / "Book.m4b").write_bytes(b"x")
        target = str(tmp_path / "Book.m4b")
        assert import_logic._first_free_output_path(target, "B0KEY12345") == str(tmp_path / "Book_B0KEY12345.m4b")

    def test_sibling_extension_collision_appends_the_key(self, tmp_path):
        # The .mp3 is a different file name but the same base — a collision.
        (tmp_path / "Book.mp3").write_bytes(b"x")
        target = str(tmp_path / "Book.m4b")
        assert import_logic._first_free_output_path(target, "B0KEY12345") == str(tmp_path / "Book_B0KEY12345.m4b")

    def test_walk_skips_suffixed_candidates_occupied_by_a_sibling_extension(self, tmp_path):
        # Both the template base and the key-suffixed base are occupied, each by a
        # *different* extension than the one being written, so the walk has to run
        # past them rather than stopping at the first free-looking filename.
        (tmp_path / "Book.m4a").write_bytes(b"x")
        (tmp_path / "Book_B0KEY12345.mp3").write_bytes(b"x")
        target = str(tmp_path / "Book.m4b")
        assert import_logic._first_free_output_path(target, "B0KEY12345") == str(tmp_path / "Book_B0KEY12345_2.m4b")


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

    def test_staging_sibling_dir_is_still_scanned(self, db, tmp_path, monkeypatch):
        # L10: only the staging dir (and its contents) is skipped. A sibling whose
        # name merely shares the staging prefix (e.g. `.import_staging_old`) must
        # still be scanned — the skip matches on a separator boundary, not a bare
        # string prefix.
        data = tmp_path / "data"
        sibling = data / f"{import_logic.IMPORT_STAGING_DIRNAME}_old"
        sibling.mkdir(parents=True)
        kept = sibling / "kept.m4b"
        kept.write_bytes(b"x")
        staged = data / import_logic.IMPORT_STAGING_DIRNAME / "wip.m4b"
        staged.parent.mkdir(parents=True)
        staged.write_bytes(b"x")

        monkeypatch.setattr(import_logic, "DATA_DIR", str(data))
        found = import_logic.scan_data_dir_for_untracked()
        assert os.path.abspath(str(kept)) in found  # sibling dir scanned
        assert os.path.abspath(str(staged)) not in found  # real staging dir skipped

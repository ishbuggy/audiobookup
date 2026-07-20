# tests/test_processing_logic.py

from threading import Event
from unittest import mock

import pytest

from audible_downloader import processing_logic
from audible_downloader.processing_logic import BookProcessor, _sanitize_filename


@pytest.fixture(autouse=True)
def _clear_output_reservations():
    """The output-path reservation set is module-level state shared across
    BookProcessor instances; clear it around every test so reservations from
    one test can't leak into the next."""
    processing_logic._reserved_output_paths.clear()
    yield
    processing_logic._reserved_output_paths.clear()


class TestSanitizeFilename:
    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            ('a\\b/c:d*e?f"g<h>i|j', "a_b_c_d_e_f_g_h_i_j"),  # every forbidden character
            (" .The Title. ", "The Title"),  # leading/trailing spaces and dots stripped
            ("Vol. 1.", "Vol. 1"),  # inner dots kept, trailing dot stripped
            ("multi   space\tand\nmore", "multi space and more"),  # whitespace collapsed
            ("AC/DC: Live", "AC_DC_ Live"),
            ("", ""),
            (" ... ", ""),  # nothing left after stripping
        ],
    )
    def test_edge_cases(self, raw, expected):
        assert _sanitize_filename(raw) == expected


BOOK_ROW = {
    "author": "Bram Stoker",
    "title": "Dracula",
    "narrator": "Simon Vance",
    "publisher": "Audible Studios",
}


def _resolve_output_path(
    asin="B0OURS",
    template="{author}/{title}/{author} - {title}",
    book_row=BOOK_ROW,
    path_exists=False,
    tracked_row=None,
    embedded_asin=None,
    truncate_subtitle=False,
):
    """
    Drives BookProcessor._prepare_and_spawn_encode_tasks just far enough to
    decide the final output path, with every external boundary mocked:
    settings, the database, the filesystem, ffprobe, and the asset download
    (which returns a falsy context so the method stops right after the path
    decision). Returns the final_output_path the processor chose.
    """
    processor = BookProcessor(asin=asin, job_id=1)

    con = mock.MagicMock()
    con.__enter__.return_value = con

    def execute(query, params=None):
        cursor = mock.MagicMock()
        if "WHERE asin" in query:
            cursor.fetchone.return_value = book_row  # the book being processed
        elif "WHERE filepath" in query:
            cursor.fetchone.return_value = tracked_row  # DB row tracking the existing file
        return cursor

    con.execute.side_effect = execute

    with (
        mock.patch.object(
            processing_logic,
            "load_settings",
            return_value={"naming": {"template": template, "truncate_subtitle": truncate_subtitle}},
        ),
        mock.patch.object(processing_logic, "get_db_connection", return_value=con),
        mock.patch.object(processing_logic, "prepare_book_assets", return_value=(None, None)),
        mock.patch("os.path.exists", return_value=path_exists),
        mock.patch("os.makedirs"),
        mock.patch.object(processor, "_probe_file_asin", return_value=embedded_asin),
        mock.patch.object(processor, "_update_db_on_failure"),
    ):
        processor._prepare_and_spawn_encode_tasks()

    return processor.final_output_path


class TestNamingTemplate:
    def test_default_template(self):
        assert _resolve_output_path() == "/data/Bram Stoker/Dracula/Bram Stoker - Dracula.m4b"

    def test_all_placeholders_expand(self):
        path = _resolve_output_path(template="{author}/{title}/{narrator}/{publisher}/{asin}")
        assert path == "/data/Bram Stoker/Dracula/Simon Vance/Audible Studios/B0OURS.m4b"

    def test_metadata_is_sanitized_before_expansion(self):
        row = dict(BOOK_ROW, author="AC/DC: Band", title="Who? Me*")
        path = _resolve_output_path(template="{author} - {title}", book_row=row)
        assert path == "/data/AC_DC_ Band - Who_ Me_.m4b"

    def test_missing_metadata_uses_fallbacks(self):
        row = {"author": None, "title": None, "narrator": None, "publisher": None}
        path = _resolve_output_path(template="{author}/{title}", book_row=row)
        assert path == "/data/Unknown Author/Unknown Title.m4b"


class TestSubtitleTruncation:
    """FR6: optional stripping of a "Main Title: Subtitle" subtitle from the
    filename title. Pure helper plus its wiring into the naming path."""

    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            ("999: The Extraordinary Young Women of the First Transport", "999"),
            ("Star Wars: The Force Awakens", "Star Wars"),
            ("Dracula", "Dracula"),  # no subtitle
            ("12:00 High Noon", "12:00 High Noon"),  # colon without space is left alone
            (": Subtitle only", ": Subtitle only"),  # would be empty -> original kept
            ("A: B: C", "A"),  # splits on the first colon-space only
            ("", ""),
            (None, None),
        ],
    )
    def test_strip_subtitle(self, raw, expected):
        assert processing_logic._strip_subtitle(raw) == expected

    def test_naming_truncates_when_enabled(self):
        row = dict(BOOK_ROW, title="999: The Extraordinary Young Women")
        path = _resolve_output_path(template="{title}", book_row=row, truncate_subtitle=True)
        assert path == "/data/999.m4b"

    def test_naming_keeps_full_title_when_disabled(self):
        row = dict(BOOK_ROW, title="999: The Extraordinary Young Women")
        path = _resolve_output_path(template="{title}", book_row=row, truncate_subtitle=False)
        assert path == "/data/999_ The Extraordinary Young Women.m4b"


class TestCollisionHandling:
    """H5 regression: an existing file at the target path must only be
    overwritten when it verifiably belongs to the same book."""

    def test_no_existing_file_keeps_plain_name(self):
        assert _resolve_output_path(path_exists=False).endswith("/Bram Stoker - Dracula.m4b")

    def test_tracked_file_same_asin_is_overwritten(self):
        path = _resolve_output_path(path_exists=True, tracked_row={"asin": "B0OURS"})
        assert path == "/data/Bram Stoker/Dracula/Bram Stoker - Dracula.m4b"

    def test_tracked_file_different_asin_gets_suffix(self):
        path = _resolve_output_path(path_exists=True, tracked_row={"asin": "B0OTHER"})
        assert path == "/data/Bram Stoker/Dracula/Bram Stoker - Dracula_B0OURS.m4b"

    def test_untracked_file_with_matching_embedded_asin_is_overwritten(self):
        path = _resolve_output_path(path_exists=True, tracked_row=None, embedded_asin="B0OURS")
        assert path == "/data/Bram Stoker/Dracula/Bram Stoker - Dracula.m4b"

    def test_untracked_file_with_foreign_embedded_asin_gets_suffix(self):
        path = _resolve_output_path(path_exists=True, tracked_row=None, embedded_asin="B0FOREIGN")
        assert path == "/data/Bram Stoker/Dracula/Bram Stoker - Dracula_B0OURS.m4b"

    def test_untracked_file_without_embedded_asin_gets_suffix(self):
        path = _resolve_output_path(path_exists=True, tracked_row=None, embedded_asin=None)
        assert path == "/data/Bram Stoker/Dracula/Bram Stoker - Dracula_B0OURS.m4b"


class TestConcurrentDuplicateHandling:
    """Bug 1 (bulk ingestion): two different books with the same author+title
    processed in the same job must not both claim the same output path. In a
    bulk job both run PREPARE before either has written its file, so the
    on-disk check can't see the conflict — the in-process reservation must."""

    def test_second_in_flight_duplicate_gets_asin_suffix(self):
        # Book A claims the base path first; nothing is on disk yet.
        path_a = _resolve_output_path(asin="B0AAAA")
        # Book B (same author+title) prepares before A has written its file.
        path_b = _resolve_output_path(asin="B0BBBB")
        assert path_a == "/data/Bram Stoker/Dracula/Bram Stoker - Dracula.m4b"
        assert path_b == "/data/Bram Stoker/Dracula/Bram Stoker - Dracula_B0BBBB.m4b"

    def test_third_in_flight_duplicate_also_stays_unique(self):
        paths = [_resolve_output_path(asin=a) for a in ("B0AAAA", "B0BBBB", "B0CCCC")]
        assert len(set(paths)) == 3

    def test_released_reservation_frees_the_plain_name(self):
        # After the first book finishes (reservation released), a later book
        # reclaims the plain name instead of being suffixed.
        path_first = _resolve_output_path(asin="B0AAAA")
        processing_logic._reserved_output_paths.discard(path_first)
        path_next = _resolve_output_path(asin="B0BBBB")
        assert path_next == "/data/Bram Stoker/Dracula/Bram Stoker - Dracula.m4b"


class TestDuplicateFlag:
    """A book whose name had to be ASIN-suffixed is marked is_duplicate, and
    that flag is persisted (as an int, clean=0) by the success UPDATE."""

    def test_clean_path_is_not_flagged(self):
        processor = BookProcessor(asin="B0AAAA", job_id=1)
        with mock.patch("os.path.exists", return_value=False):
            processor._reserve_output_path("/data/A/Title/Title.m4b", "B0AAAA")
        assert processor.is_duplicate is False

    def test_in_flight_collision_sets_flag(self):
        first = BookProcessor(asin="B0AAAA", job_id=1)
        second = BookProcessor(asin="B0BBBB", job_id=1)
        with mock.patch("os.path.exists", return_value=False):
            first._reserve_output_path("/data/A/Title/Title.m4b", "B0AAAA")
            second._reserve_output_path("/data/A/Title/Title.m4b", "B0BBBB")
        assert first.is_duplicate is False
        assert second.is_duplicate is True

    def test_success_update_persists_flag_as_int(self):
        processor = BookProcessor(asin="B0BBBB", job_id=1)
        processor.is_duplicate = True
        processor.final_output_path = "/data/A/Title/Title_B0BBBB.m4b"

        con = mock.MagicMock()
        con.__enter__.return_value = con
        con.execute.return_value.fetchone.return_value = None  # no runtime row

        with (
            mock.patch.object(processing_logic, "merge_book_chunks", return_value=True),
            mock.patch.object(processing_logic, "get_db_connection", return_value=con),
            mock.patch.object(processing_logic, "record_conversion_time"),
            mock.patch.object(processing_logic, "_yield_progress"),
            mock.patch.object(processor, "_verify_output_file", return_value=(True, None)),
        ):
            processor._merge_and_finalize()

        update_calls = [call for call in con.execute.call_args_list if "status = 'DOWNLOADED'" in call.args[0]]
        assert len(update_calls) == 1
        assert update_calls[0].args[1] == ("/data/A/Title/Title_B0BBBB.m4b", 1, "B0BBBB")


class TestFailureReporting:
    """Bug 7: a failed download reports the real underlying cause instead of a
    generic 'Failed during asset download/preparation.'"""

    def _run_prepare_with(self, prepare_return):
        processor = BookProcessor(asin="B0OURS", job_id=1)
        con = mock.MagicMock()
        con.__enter__.return_value = con
        con.execute.return_value.fetchone.return_value = BOOK_ROW
        with (
            mock.patch.object(processing_logic, "load_settings", return_value={"naming": {}}),
            mock.patch.object(processing_logic, "get_db_connection", return_value=con),
            mock.patch.object(processing_logic, "prepare_book_assets", return_value=prepare_return),
            mock.patch("os.path.exists", return_value=False),
            mock.patch("os.makedirs"),
            mock.patch.object(processor, "_update_db_on_failure") as fail,
        ):
            processor._prepare_and_spawn_encode_tasks()
        return fail

    def test_real_reason_is_surfaced(self):
        fail = self._run_prepare_with((None, "Title no longer available"))
        fail.assert_called_once_with("Title no longer available")

    def test_missing_reason_uses_generic_fallback(self):
        fail = self._run_prepare_with((None, None))
        fail.assert_called_once_with("Failed during asset download/preparation.")


class TestOutputVerification:
    """Bugs 2 & FR3: a book is only marked DOWNLOADED after its finished file is
    confirmed present, non-trivial in size, and not truncated."""

    def _verify_with(self, exists=True, size=10 * 1024 * 1024, runtime_min=60, duration_sec=3600.0):
        processor = BookProcessor(asin="B0OURS", job_id=1)
        processor.final_output_path = "/data/A/Title/Title.m4b"
        con = mock.MagicMock()
        con.__enter__.return_value = con
        con.execute.return_value.fetchone.return_value = {"runtime_min": runtime_min}
        with (
            mock.patch("os.path.exists", return_value=exists),
            mock.patch("os.path.getsize", return_value=size),
            mock.patch.object(processing_logic, "get_db_connection", return_value=con),
            mock.patch.object(processing_logic, "_probe_duration_seconds", return_value=duration_sec),
        ):
            return processor._verify_output_file()

    def test_missing_file_fails(self):
        ok, reason = self._verify_with(exists=False)
        assert ok is False
        assert "no output file" in reason

    def test_tiny_file_fails(self):
        ok, reason = self._verify_with(size=1000)
        assert ok is False
        assert "implausibly small" in reason

    def test_unreadable_duration_fails(self):
        ok, reason = self._verify_with(duration_sec=None)
        assert ok is False
        assert "could not be read back" in reason

    def test_truncated_file_fails(self):
        ok, reason = self._verify_with(runtime_min=60, duration_sec=1200.0)  # 20m of a 60m book
        assert ok is False
        assert "truncated" in reason

    def test_full_length_file_passes(self):
        assert self._verify_with(runtime_min=60, duration_sec=3600.0) == (True, None)

    def test_unknown_runtime_skips_duration_check(self):
        # No expected runtime -> existence + size are enough, duration not checked.
        assert self._verify_with(runtime_min=None, duration_sec=None) == (True, None)

    def _run_merge(self, verify_result):
        processor = BookProcessor(asin="B0OURS", job_id=1)
        processor.final_output_path = "/data/A/Title/Title.m4b"
        con = mock.MagicMock()
        con.__enter__.return_value = con
        con.execute.return_value.fetchone.return_value = None
        with (
            mock.patch.object(processing_logic, "merge_book_chunks", return_value=True),
            mock.patch.object(processing_logic, "get_db_connection", return_value=con),
            mock.patch.object(processing_logic, "record_conversion_time"),
            mock.patch.object(processing_logic, "_yield_progress"),
            mock.patch.object(processor, "_verify_output_file", return_value=verify_result),
            mock.patch.object(processor, "_update_db_on_failure") as fail,
        ):
            processor._merge_and_finalize()
        downloaded = [call for call in con.execute.call_args_list if "status = 'DOWNLOADED'" in call.args[0]]
        return downloaded, fail

    def test_valid_output_is_marked_downloaded(self):
        downloaded, fail = self._run_merge((True, None))
        fail.assert_not_called()
        assert len(downloaded) == 1

    def test_invalid_output_is_not_marked_downloaded(self):
        downloaded, fail = self._run_merge((False, "Conversion reported success but no output file was found on disk."))
        fail.assert_called_once_with("Conversion reported success but no output file was found on disk.")
        assert downloaded == []


class TestSupplementaryPdf:
    """FR11: a companion PDF, when present, is copied next to the audiobook
    with a matching name; best-effort and never fatal."""

    def _processor(self, pdf_file):
        processor = BookProcessor(asin="B0X", job_id=1)
        processor.context = {"pdf_file": pdf_file}
        processor.final_output_path = "/data/A/Title/Arthur - Title.m4b"
        return processor

    def test_no_pdf_is_noop(self):
        processor = self._processor(None)
        with mock.patch.object(processing_logic.shutil, "copy2") as copy2:
            processor._place_supplementary_pdf()
        copy2.assert_not_called()

    def test_pdf_copied_next_to_audiobook_with_matching_name(self):
        processor = self._processor("/tmp/book/booklet.pdf")
        with (
            mock.patch("os.path.exists", return_value=True),
            mock.patch.object(processing_logic.shutil, "copy2") as copy2,
        ):
            processor._place_supplementary_pdf()
        copy2.assert_called_once_with("/tmp/book/booklet.pdf", "/data/A/Title/Arthur - Title.pdf")

    def test_copy_failure_is_non_fatal(self):
        processor = self._processor("/tmp/book/booklet.pdf")
        with (
            mock.patch("os.path.exists", return_value=True),
            mock.patch.object(processing_logic.shutil, "copy2", side_effect=OSError("disk full")),
        ):
            processor._place_supplementary_pdf()  # must not raise


class TestCancellation:
    """M4 regression: once the job's stop_event is set, queued tasks must
    become no-ops that unblock the processor instead of starting fresh work."""

    def _cancelled_processor(self):
        stop_event = Event()
        stop_event.set()
        return BookProcessor(asin="B0OURS", job_id=1, stop_event=stop_event)

    def test_prepare_task_skips_work_and_unblocks(self):
        processor = self._cancelled_processor()
        processor.download_complete_event = Event()
        with mock.patch.object(processing_logic, "load_settings") as load_settings:
            processor._prepare_and_spawn_encode_tasks()
        load_settings.assert_not_called()
        assert processor._completion_event.is_set()
        assert processor.download_complete_event.is_set()

    def test_encode_task_skips_work_and_unblocks(self):
        processor = self._cancelled_processor()
        with mock.patch.object(processing_logic, "encode_chapter_chunk") as encode:
            processor._encode_and_track_chunk({"index": 0})
        encode.assert_not_called()
        assert processor._completion_event.is_set()

    def test_merge_task_skips_work_and_unblocks(self):
        processor = self._cancelled_processor()
        with mock.patch.object(processing_logic, "merge_book_chunks") as merge:
            processor._merge_and_finalize()
        merge.assert_not_called()
        assert processor._completion_event.is_set()

    def test_no_stop_event_means_never_cancelled(self):
        processor = BookProcessor(asin="B0OURS", job_id=1)
        assert processor._cancelled() is False
        assert not processor._completion_event.is_set()

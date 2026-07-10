# tests/test_processing_logic.py

from threading import Event
from unittest import mock

import pytest

from audible_downloader import processing_logic
from audible_downloader.processing_logic import BookProcessor, _sanitize_filename


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
        mock.patch.object(processing_logic, "load_settings", return_value={"naming": {"template": template}}),
        mock.patch.object(processing_logic, "get_db_connection", return_value=con),
        mock.patch.object(processing_logic, "prepare_book_assets", return_value={}),
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

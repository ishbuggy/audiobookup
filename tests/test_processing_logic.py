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
    "custom_title": None,
    "custom_author": None,
}


def _resolve_output_path(
    asin="B0OURS",
    template="{author}/{title}/{author} - {title}",
    book_row=BOOK_ROW,
    path_exists=False,
    tracked_row=None,
    embedded_asin=None,
    truncate_subtitle=False,
    apply_custom_to_filenames=False,
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
            return_value={
                "naming": {
                    "template": template,
                    "truncate_subtitle": truncate_subtitle,
                    "apply_custom_to_filenames": apply_custom_to_filenames,
                }
            },
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


class TestApplyCustomToFilenames:
    """Phase 5.5: custom title/author drive the filename only when opted in."""

    def test_custom_used_when_enabled(self):
        row = dict(BOOK_ROW, custom_title="Dracula (Curry)", custom_author="B. Stoker")
        path = _resolve_output_path(template="{author}/{title}", book_row=row, apply_custom_to_filenames=True)
        assert path == "/data/B. Stoker/Dracula (Curry).m4b"

    def test_native_used_when_disabled(self):
        row = dict(BOOK_ROW, custom_title="Dracula (Curry)", custom_author="B. Stoker")
        path = _resolve_output_path(template="{author}/{title}", book_row=row, apply_custom_to_filenames=False)
        assert path == "/data/Bram Stoker/Dracula.m4b"

    def test_partial_custom_falls_back_per_field(self):
        row = dict(BOOK_ROW, custom_title=None, custom_author="B. Stoker")
        path = _resolve_output_path(template="{author}/{title}", book_row=row, apply_custom_to_filenames=True)
        assert path == "/data/B. Stoker/Dracula.m4b"


class TestRenameToMatchMetadata:
    """Phase 5.5: an edit renames the on-disk file only when opted in, and never
    overwrites another book."""

    CURRENT = "/data/Bram Stoker/Dracula/Bram Stoker - Dracula.m4b"

    def _row(self, **over):
        row = {
            "author": "Bram Stoker",
            "title": "Dracula",
            "narrator": "N",
            "publisher": "P",
            "custom_title": None,
            "custom_author": None,
            "filepath": self.CURRENT,
            "status": "DOWNLOADED",
        }
        row.update(over)
        return row

    def _run(self, *, apply=True, row=None, target="/data/New/New.m4b", target_exists=False, target_owner=None):
        row = row if row is not None else self._row()
        con = mock.MagicMock()
        con.__enter__.return_value = con

        def execute(query, params=None):
            cursor = mock.MagicMock()
            if "WHERE filepath" in query:
                cursor.fetchone.return_value = {"asin": target_owner} if target_owner else None
            else:
                cursor.fetchone.return_value = row
            return cursor

        con.execute.side_effect = execute

        def exists(path):
            if path == self.CURRENT:
                return True
            if path == target:
                return target_exists
            return False

        with (
            mock.patch.object(
                processing_logic, "load_settings", return_value={"naming": {"apply_custom_to_filenames": apply}}
            ),
            mock.patch.object(processing_logic, "get_db_connection", return_value=con),
            mock.patch.object(processing_logic, "build_base_output_path", return_value=target),
            mock.patch("os.path.exists", side_effect=exists),
            mock.patch("os.makedirs"),
            mock.patch.object(processing_logic.shutil, "move") as move,
            mock.patch.object(processing_logic, "_cleanup_empty_dirs"),
        ):
            result = processing_logic.rename_book_to_match_metadata("B0OURS")
        return result, move

    def test_disabled_setting_is_noop(self):
        result, move = self._run(apply=False)
        assert result is None
        move.assert_not_called()

    def test_not_downloaded_is_noop(self):
        result, move = self._run(row=self._row(status="NEW"))
        assert result is None
        move.assert_not_called()

    def test_missing_filepath_is_noop(self):
        result, move = self._run(row=self._row(filepath=None))
        assert result is None
        move.assert_not_called()

    def test_unchanged_name_is_noop(self):
        # build_base_output_path returns the current path -> nothing to do.
        result, move = self._run(target=self.CURRENT)
        assert result is None
        move.assert_not_called()

    def test_happy_path_moves_and_returns_target(self):
        result, move = self._run(target="/data/New/New.m4b")
        assert result == "/data/New/New.m4b"
        move.assert_any_call(self.CURRENT, "/data/New/New.m4b")

    def test_collision_with_other_book_appends_asin(self):
        result, move = self._run(target="/data/New/New.m4b", target_exists=True, target_owner="B0OTHER")
        assert result == "/data/New/New_B0OURS.m4b"
        move.assert_any_call(self.CURRENT, "/data/New/New_B0OURS.m4b")

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


class TestNoReencodePathSelection:
    """Phase 8 (FR12): with conversion.no_reencode on, a book whose fast
    AAC-copy decrypt succeeded (an .m4b master) skips the per-chapter encode and
    goes straight to a single lossless remux task. Off, or when the decrypt fell
    back to FLAC, the normal re-encode path runs unchanged."""

    def _run(self, no_reencode, audio_file, chapters=None):
        chapters = chapters if chapters is not None else [{"start_offset_ms": 0, "length_ms": 1000}]
        processor = BookProcessor(asin="B0OURS", job_id=1)
        processor.download_complete_event = Event()

        con = mock.MagicMock()
        con.__enter__.return_value = con
        con.execute.return_value.fetchone.return_value = BOOK_ROW

        context = {"audio_file": audio_file, "chapters": chapters}
        submitted = []

        with (
            mock.patch.object(
                processing_logic,
                "load_settings",
                return_value={"naming": {}, "conversion": {"no_reencode": no_reencode}},
            ),
            mock.patch.object(processing_logic, "get_db_connection", return_value=con),
            mock.patch.object(processing_logic, "prepare_book_assets", return_value=(context, None)) as prepare,
            mock.patch("os.path.exists", return_value=False),
            mock.patch("os.makedirs"),
            mock.patch.object(processing_logic, "_yield_progress"),
            mock.patch.object(processing_logic.task_runner, "submit_task", side_effect=submitted.append),
        ):
            processor._prepare_and_spawn_encode_tasks()

        return processor, submitted, prepare

    def test_lossless_with_aac_master_submits_single_remux_task(self):
        processor, submitted, prepare = self._run(
            no_reencode=True,
            audio_file="/tmp/x/master_intermediate.m4b",
            chapters=[{"start_offset_ms": 0, "length_ms": 1000}, {"start_offset_ms": 1000, "length_ms": 1000}],
        )
        assert len(submitted) == 1
        assert submitted[0].func == processor._remux_and_finalize
        # No per-chapter encode work was set up.
        assert processor.total_chunks == 0

    def test_lossless_passes_flag_into_prepare(self):
        _, _, prepare = self._run(no_reencode=True, audio_file="/tmp/x/master_intermediate.m4b")
        assert prepare.call_args.kwargs.get("lossless") is True

    def test_lossless_with_flac_master_falls_back_to_encode(self):
        processor, submitted, _ = self._run(
            no_reencode=True,
            audio_file="/tmp/x/master_intermediate.flac",
            chapters=[{"start_offset_ms": 0, "length_ms": 1000}, {"start_offset_ms": 1000, "length_ms": 1000}],
        )
        assert processor.total_chunks == 2
        assert all(t.func == processor._encode_and_track_chunk for t in submitted)

    def test_disabled_uses_encode_path(self):
        processor, submitted, prepare = self._run(
            no_reencode=False,
            audio_file="/tmp/x/master_intermediate.m4b",
            chapters=[{"start_offset_ms": 0, "length_ms": 1000}],
        )
        assert prepare.call_args.kwargs.get("lossless") is False
        assert processor.total_chunks == 1
        assert all(t.func == processor._encode_and_track_chunk for t in submitted)


class TestRemuxFinalize:
    """The lossless remux task shares the same success finalization (verify +
    DOWNLOADED + PDF) as the merge task, and reports a distinct failure."""

    def _run(self, remux_success, verify_result=(True, None)):
        processor = BookProcessor(asin="B0OURS", job_id=1)
        processor.final_output_path = "/data/A/Title/Title.m4b"
        con = mock.MagicMock()
        con.__enter__.return_value = con
        con.execute.return_value.fetchone.return_value = {"runtime_min": 60}
        with (
            mock.patch.object(processing_logic, "remux_book_lossless", return_value=remux_success),
            mock.patch.object(processing_logic, "get_db_connection", return_value=con),
            mock.patch.object(processing_logic, "record_conversion_time") as record_eta,
            mock.patch.object(processing_logic, "_yield_progress"),
            mock.patch.object(processor, "_verify_output_file", return_value=verify_result),
            mock.patch.object(processor, "_place_supplementary_pdf") as pdf,
            mock.patch.object(processor, "_update_db_on_failure") as fail,
        ):
            processor._remux_and_finalize()
        downloaded = [c for c in con.execute.call_args_list if "status = 'DOWNLOADED'" in c.args[0]]
        return downloaded, fail, pdf, record_eta, processor

    def test_success_marks_downloaded(self):
        downloaded, fail, pdf, _record_eta, processor = self._run(remux_success=True)
        fail.assert_not_called()
        assert len(downloaded) == 1
        pdf.assert_called_once()
        assert processor._completion_event.is_set()

    def test_success_does_not_pollute_eta_history(self):
        # L2: a remux is far faster than a re-encode; its duration must not be
        # fed into the shared conversion-rate model (which would skew estimates
        # and the timeout for later re-encode jobs).
        _downloaded, _fail, _pdf, record_eta, _processor = self._run(remux_success=True)
        record_eta.assert_not_called()

    def test_remux_failure_reports_distinct_reason(self):
        downloaded, fail, _pdf, _record_eta, processor = self._run(remux_success=False)
        fail.assert_called_once_with("Lossless remux failed.")
        assert downloaded == []
        assert processor._completion_event.is_set()

    def test_failed_verification_blocks_downloaded(self):
        downloaded, fail, _pdf, _record_eta, _processor = self._run(
            remux_success=True, verify_result=(False, "truncated")
        )
        fail.assert_called_once_with("truncated")
        assert downloaded == []

    def test_reencode_merge_still_records_eta(self):
        # Contrast with the remux path: the re-encode merge SHOULD feed the ETA
        # model, so the L2 fix didn't disable estimation for the default path.
        processor = BookProcessor(asin="B0OURS", job_id=1)
        processor.final_output_path = "/data/A/Title/Title.m4b"
        con = mock.MagicMock()
        con.__enter__.return_value = con
        con.execute.return_value.fetchone.return_value = {"runtime_min": 60}
        with (
            mock.patch.object(processing_logic, "merge_book_chunks", return_value=True),
            mock.patch.object(processing_logic, "get_db_connection", return_value=con),
            mock.patch.object(processing_logic, "record_conversion_time") as record_eta,
            mock.patch.object(processing_logic, "_yield_progress"),
            mock.patch.object(processor, "_verify_output_file", return_value=(True, None)),
            mock.patch.object(processor, "_place_supplementary_pdf"),
        ):
            processor._merge_and_finalize()
        record_eta.assert_called_once()


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

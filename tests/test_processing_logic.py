# tests/test_processing_logic.py

import json
import os
from datetime import datetime
from threading import Event
from unittest import mock

import pytest

from audible_downloader import processing_logic
from audible_downloader.processing_logic import (
    BookProcessor,
    _parse_timestamp_date,
    _sanitize_filename,
    build_base_output_path,
    build_metadata_json,
    generate_cue_sheet,
)


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
    "series": "N/A",
    "series_sequence": "N/A",
    "release_date": "N/A",
    "language": "N/A",
}


def _run_prepare(
    asin="B0OURS",
    template="{author}/{title}/{author} - {title}",
    book_row=BOOK_ROW,
    path_exists=False,
    tracked_row=None,
    embedded_asin=None,
    truncate_subtitle=False,
    apply_custom_to_filenames=False,
    folder_template="",
    file_template="",
):
    """
    Drives BookProcessor._prepare_and_spawn_encode_tasks just far enough to
    decide the final output path, with every external boundary mocked:
    settings, the database, the filesystem, ffprobe, and the asset download
    (which returns a falsy context so the method stops right after the path
    decision). Returns the processor itself, so tests can assert on any state
    PREPARE lifted out of the DB row and not just the chosen path.
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
                    "folder_template": folder_template,
                    "file_template": file_template,
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

    return processor


def _resolve_output_path(**kwargs):
    """The common case of _run_prepare: only the chosen output path matters."""
    return _run_prepare(**kwargs).final_output_path


class TestNamingTemplate:
    def test_default_template(self):
        assert _resolve_output_path() == "/data/Bram Stoker/Dracula/Bram Stoker - Dracula.m4b"

    def test_all_placeholders_expand(self):
        path = _resolve_output_path(template="{author}/{title}/{narrator}/{publisher}/{asin}")
        assert path == "/data/Bram Stoker/Dracula/Simon Vance/Audible Studios/B0OURS.m4b"

    def test_metadata_is_sanitized_before_expansion(self):
        # "Who? Me*" sanitizes to "Who_ Me_"; the drop-segment cleanup (§4) then
        # strips the trailing underscore left by sanitizing the forbidden '*'.
        row = dict(BOOK_ROW, author="AC/DC: Band", title="Who? Me*")
        path = _resolve_output_path(template="{author} - {title}", book_row=row)
        assert path == "/data/AC_DC_ Band - Who_ Me.m4b"

    def test_missing_metadata_uses_fallbacks(self):
        row = {
            "author": None,
            "title": None,
            "narrator": None,
            "publisher": None,
            "custom_title": None,
            "custom_author": None,
            "series": "N/A",
            "series_sequence": "N/A",
            "release_date": "N/A",
            "language": "N/A",
        }
        path = _resolve_output_path(template="{author}/{title}", book_row=row)
        assert path == "/data/Unknown Author/Unknown Title.m4b"


class TestNamingPlaceholderExpansion:
    """v0.22.0 Phase 3: the new {series} {series_part} {year} {language} tags,
    the missing-value rule (None/""/"N/A" render empty), and the drop-segment
    path cleanup that removes folder levels and trailing separators left behind."""

    # A book that is part of a series, with a real release date and language.
    SERIES_ROW = dict(
        BOOK_ROW,
        series="Dune",
        series_sequence="1",
        release_date="2019-06-04",
        language="English",
    )

    def test_series_tag_renders(self):
        path = _resolve_output_path(template="{series}", book_row=self.SERIES_ROW)
        assert path == "/data/Dune.m4b"

    def test_series_part_tag_renders(self):
        path = _resolve_output_path(template="{series_part}", book_row=self.SERIES_ROW)
        assert path == "/data/1.m4b"

    def test_language_tag_renders(self):
        path = _resolve_output_path(template="{language}", book_row=self.SERIES_ROW)
        assert path == "/data/English.m4b"

    def test_year_derived_from_release_date(self):
        path = _resolve_output_path(template="{year}/{title}", book_row=self.SERIES_ROW)
        assert path == "/data/2019/Dracula.m4b"

    def test_full_series_template(self):
        path = _resolve_output_path(template="{author}/{series}/{series_part} - {title}", book_row=self.SERIES_ROW)
        assert path == "/data/Bram Stoker/Dune/1 - Dracula.m4b"

    @pytest.mark.parametrize("missing", ["N/A", "", None])
    def test_missing_series_drops_folder_level(self, missing):
        row = dict(BOOK_ROW, series=missing)
        path = _resolve_output_path(template="{author}/{series}/{title}", book_row=row)
        assert path == "/data/Bram Stoker/Dracula.m4b"

    def test_missing_trailing_tag_strips_separator(self):
        # "{author} - {series}" with no series -> "Bram Stoker - " -> "Bram Stoker".
        row = dict(BOOK_ROW, series="N/A")
        path = _resolve_output_path(template="{author} - {series}", book_row=row)
        assert path == "/data/Bram Stoker.m4b"

    def test_empty_filename_falls_back_to_author_title(self):
        # Filename segment renders empty (missing series) -> "<author> - <title>".
        row = dict(BOOK_ROW, series="N/A")
        path = _resolve_output_path(template="{author}/{series}", book_row=row)
        assert path == "/data/Bram Stoker/Bram Stoker - Dracula.m4b"

    @pytest.mark.parametrize(
        ("release_date", "expected"),
        [
            ("2019-06-04", "/data/2019.m4b"),  # valid ISO date
            ("2019", "/data/2019.m4b"),  # bare year
            ("N/A", "/data/Bram Stoker - Dracula.m4b"),  # missing -> empty -> fallback
            ("Mar 2019", "/data/Bram Stoker - Dracula.m4b"),  # non-numeric first 4 chars
            ("19-06", "/data/Bram Stoker - Dracula.m4b"),  # fewer than 4 leading digits
        ],
    )
    def test_year_extraction_rules(self, release_date, expected):
        row = dict(BOOK_ROW, release_date=release_date)
        path = _resolve_output_path(template="{year}", book_row=row)
        assert path == expected

    def test_series_value_cannot_create_directories(self):
        # A '/' inside a value is sanitized so it can't inject a folder level.
        row = dict(BOOK_ROW, series="Book 1/2")
        path = _resolve_output_path(template="{series}", book_row=row)
        assert path == "/data/Book 1_2.m4b"

    def test_existing_tags_unaffected_by_new_params(self):
        # Regression: with all new tags missing the default template is unchanged.
        path = _resolve_output_path(book_row=BOOK_ROW)
        assert path == "/data/Bram Stoker/Dracula/Bram Stoker - Dracula.m4b"


class TestFolderFileTemplateSplit:
    """Phase 9: `folder_template` + `file_template` compose into the effective
    template and win over `template`, but only as a complete pair."""

    SERIES_ROW = dict(
        BOOK_ROW,
        series="Dune",
        series_sequence="1",
        release_date="2019-06-04",
        language="English",
    )

    def test_both_set_composes_and_overrides_template(self):
        path = _resolve_output_path(
            template="{asin}",
            folder_template="{author}/{series}",
            file_template="{series_part} - {title}",
            book_row=self.SERIES_ROW,
        )
        assert path == "/data/Bram Stoker/Dune/1 - Dracula.m4b"

    @pytest.mark.parametrize(
        ("folder_template", "file_template"),
        [
            ("{author}", ""),  # folder only
            ("", "{title}"),  # file only
            ("", ""),  # neither (the shipped default)
            ("   ", "   "),  # whitespace-only counts as empty
        ],
    )
    def test_incomplete_pair_falls_back_to_template(self, folder_template, file_template):
        path = _resolve_output_path(folder_template=folder_template, file_template=file_template)
        assert path == "/data/Bram Stoker/Dracula/Bram Stoker - Dracula.m4b"

    def test_missing_keys_fall_back_to_template(self):
        # Regression for old settings.json files predating the split keys: the
        # naming dict has no folder_template/file_template at all.
        settings = {"naming": {"template": "{author}/{title}"}}
        path = build_base_output_path(settings, "B0OURS", "Bram Stoker", "Dracula", "Simon Vance", "Audible Studios")
        assert path == "/data/Bram Stoker/Dracula.m4b"

    def test_composed_template_gets_drop_segment_cleanup(self):
        # A missing {series} folder level is dropped from the composed template
        # exactly as it is from a single `template`.
        path = _resolve_output_path(
            folder_template="{author}/{series}",
            file_template="{title}",
            book_row=BOOK_ROW,  # series == "N/A"
        )
        assert path == "/data/Bram Stoker/Dracula.m4b"

    def test_composed_empty_filename_falls_back_to_author_title(self):
        path = _resolve_output_path(
            folder_template="{author}",
            file_template="{series}",
            book_row=BOOK_ROW,  # series == "N/A" -> filename segment empty
        )
        assert path == "/data/Bram Stoker/Bram Stoker - Dracula.m4b"


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

    def test_prepare_carries_custom_title_to_the_processor(self):
        # PREPARE lifts custom_title off the DB row so the sidecar writers can
        # reach it at finalize time. It is carried regardless of
        # apply_custom_to_filenames, which governs the filename only: the
        # embedded tags and the sidecars always prefer the custom title.
        row = dict(BOOK_ROW, custom_title="Dracula (Curry)")
        assert _run_prepare(book_row=row).custom_title == "Dracula (Curry)"
        assert _run_prepare(book_row=row, apply_custom_to_filenames=True).custom_title == "Dracula (Curry)"

    def test_prepare_leaves_custom_title_unset_when_the_column_is_null(self):
        assert _run_prepare(book_row=dict(BOOK_ROW, custom_title=None)).custom_title is None


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
            "series": "N/A",
            "series_sequence": "N/A",
            "release_date": "N/A",
            "language": "N/A",
        }
        row.update(over)
        return row

    def _run(
        self,
        *,
        apply=True,
        row=None,
        target="/data/New/New.m4b",
        target_exists=False,
        target_owner=None,
        makedirs_error=None,
        also_present=(),
        move_side_effect=None,
    ):
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

        current = row["filepath"]

        def exists(path):
            if path == current:
                return True
            if path == target:
                return target_exists
            return path in also_present

        with (
            mock.patch.object(
                processing_logic, "load_settings", return_value={"naming": {"apply_custom_to_filenames": apply}}
            ),
            mock.patch.object(processing_logic, "get_db_connection", return_value=con),
            mock.patch.object(processing_logic, "build_base_output_path", return_value=target),
            mock.patch("os.path.exists", side_effect=exists),
            mock.patch("os.makedirs", side_effect=makedirs_error),
            mock.patch.object(processing_logic.shutil, "move", side_effect=move_side_effect) as move,
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

    def test_value_error_from_makedirs_is_swallowed(self):
        # W4 regression: a NUL byte survives _sanitize_filename, and os.makedirs
        # raises ValueError("embedded null byte") — not OSError. The rename is
        # best-effort and runs after the metadata edit is committed, so it must
        # never escape as a 500.
        result, move = self._run(makedirs_error=ValueError("embedded null byte"))
        assert result is None
        move.assert_not_called()

    def test_os_error_from_makedirs_is_still_swallowed(self):
        result, move = self._run(makedirs_error=OSError("read-only filesystem"))
        assert result is None
        move.assert_not_called()

    def test_collision_with_other_book_appends_asin(self):
        result, move = self._run(target="/data/New/New.m4b", target_exists=True, target_owner="B0OTHER")
        assert result == "/data/New/New_B0OURS.m4b"
        move.assert_any_call(self.CURRENT, "/data/New/New_B0OURS.m4b")

    """v0.23.0 #6: a metadata edit must respect the in-flight reservations a
    DOWNLOAD job takes at PREPARE time, and must judge "taken" on the
    extension-stripped base the sidecars share rather than the full path."""

    def test_collision_at_sibling_extension_appends_asin(self):
        # The .m4b target itself is free, but another book's .mp3 already occupies
        # the same base — our .pdf/.cue/.metadata.json would overwrite its ones.
        result, move = self._run(
            target="/data/New/New.m4b",
            also_present=("/data/New/New.mp3",),
            target_owner="B0OTHER",
        )
        assert result == "/data/New/New_B0OURS.m4b"
        move.assert_any_call(self.CURRENT, "/data/New/New_B0OURS.m4b")

    def test_collision_at_m4a_sibling_extension_appends_asin(self):
        # W2 regression: ".m4a" is a real library extension (import keeps an
        # upload's own container), so it must be probed like ".mp3" is.
        result, move = self._run(
            target="/data/New/New.m4b",
            also_present=("/data/New/New.m4a",),
            target_owner="B0OTHER",
        )
        assert result == "/data/New/New_B0OURS.m4b"
        move.assert_any_call(self.CURRENT, "/data/New/New_B0OURS.m4b")

    def test_m4a_book_still_checks_its_own_siblings(self):
        # W2 regression, the other direction: an imported ".m4a" book keeps its
        # extension through the rename, and the ".m4b" sibling of its target is
        # another book's — the old two-entry map examined no sibling at all here.
        current = "/data/Old/Old.m4a"
        result, move = self._run(
            row=self._row(filepath=current),
            target="/data/New/New.m4a",
            also_present=(current, "/data/New/New.m4b"),
            target_owner="B0OTHER",
        )
        assert result == "/data/New/New_B0OURS.m4a"
        move.assert_any_call(current, "/data/New/New_B0OURS.m4a")

    def test_sibling_extension_owned_by_this_book_is_not_a_collision(self):
        # Our own earlier download in the previous output format: not foreign.
        result, move = self._run(
            target="/data/New/New.m4b",
            also_present=("/data/New/New.mp3",),
            target_owner="B0OURS",
        )
        assert result == "/data/New/New.m4b"
        move.assert_any_call(self.CURRENT, "/data/New/New.m4b")

    def test_target_reserved_by_in_flight_book_appends_asin(self):
        # The reserving book has run PREPARE but not yet written its file, so
        # neither the on-disk nor the DB check can see the conflict.
        processing_logic._reserved_output_paths.add("/data/New/New")
        result, move = self._run(target="/data/New/New.m4b")
        assert result == "/data/New/New_B0OURS.m4b"
        move.assert_any_call(self.CURRENT, "/data/New/New_B0OURS.m4b")

    def test_rename_reserves_the_target_only_for_the_duration_of_the_move(self):
        # The claim has to be HELD across the move (that is the half of #6 that
        # stops an in-flight download from claiming this base mid-rename), so the
        # assertion happens INSIDE the patched move — checking the set afterwards
        # can't tell "claimed then released" from "never claimed at all".
        held = []

        def check_claim_at_move_time(src, dst):
            if src == self.CURRENT:
                held.append(os.path.splitext(dst)[0] in processing_logic._reserved_output_paths)

        result, _move = self._run(target="/data/New/New.m4b", move_side_effect=check_claim_at_move_time)
        assert result == "/data/New/New.m4b"
        assert held == [True]
        # ...and released once the move and the DB update are done.
        assert processing_logic._reserved_output_paths == set()

    def test_reservation_is_released_when_the_rename_fails(self):
        # Same claim-at-move-time assertion, then the move raises: the release must
        # happen on the failure path too.
        held = []

        def fail_after_checking_claim(src, dst):
            held.append(os.path.splitext(dst)[0] in processing_logic._reserved_output_paths)
            raise OSError("read-only filesystem")

        result, _move = self._run(target="/data/New/New.m4b", move_side_effect=fail_after_checking_claim)
        assert result is None
        assert held == [True]
        assert processing_logic._reserved_output_paths == set()

    def test_preserves_mp3_extension(self):
        # Phase 5: an .mp3 book must be renamed to an .mp3 target, so the file's
        # real extension is passed through to build_base_output_path (not the
        # default .m4b).
        current = "/data/Old/Old.mp3"
        row = self._row(filepath=current)
        con = mock.MagicMock()
        con.__enter__.return_value = con

        def execute(query, params=None):
            cursor = mock.MagicMock()
            cursor.fetchone.return_value = None if "WHERE filepath" in query else row
            return cursor

        con.execute.side_effect = execute
        bbop = mock.MagicMock(return_value="/data/New/New.mp3")
        with (
            mock.patch.object(
                processing_logic, "load_settings", return_value={"naming": {"apply_custom_to_filenames": True}}
            ),
            mock.patch.object(processing_logic, "get_db_connection", return_value=con),
            mock.patch.object(processing_logic, "build_base_output_path", bbop),
            mock.patch("os.path.exists", side_effect=lambda p: p == current),
            mock.patch("os.makedirs"),
            mock.patch.object(processing_logic.shutil, "move"),
            mock.patch.object(processing_logic, "_cleanup_empty_dirs"),
        ):
            processing_logic.rename_book_to_match_metadata("B0OURS")
        assert bbop.call_args.kwargs["ext"] == ".mp3"

    def test_moves_all_present_sidecars(self):
        # Phase 5: a rename carries every sidecar sharing the old base name to the
        # new base name; sidecars that aren't on disk are skipped.
        target = "/data/New/New.m4b"
        old_base = "/data/Bram Stoker/Dracula/Bram Stoker - Dracula"
        new_base = "/data/New/New"
        present = {
            self.CURRENT,
            old_base + ".pdf",
            old_base + ".jpg",
            old_base + ".cue",
            old_base + ".metadata.json",
            old_base + ".voucher",
        }
        con = mock.MagicMock()
        con.__enter__.return_value = con

        def execute(query, params=None):
            cursor = mock.MagicMock()
            cursor.fetchone.return_value = None if "WHERE filepath" in query else self._row()
            return cursor

        con.execute.side_effect = execute
        with (
            mock.patch.object(
                processing_logic, "load_settings", return_value={"naming": {"apply_custom_to_filenames": True}}
            ),
            mock.patch.object(processing_logic, "get_db_connection", return_value=con),
            mock.patch.object(processing_logic, "build_base_output_path", return_value=target),
            mock.patch("os.path.exists", side_effect=lambda p: p in present),
            mock.patch("os.makedirs"),
            mock.patch.object(processing_logic.shutil, "move") as move,
            mock.patch.object(processing_logic, "_cleanup_empty_dirs"),
        ):
            processing_logic.rename_book_to_match_metadata("B0OURS")

        moved = {call.args for call in move.call_args_list}
        # The audiobook itself plus each present sidecar moved to the new base.
        assert (self.CURRENT, target) in moved
        for suffix in (".pdf", ".jpg", ".cue", ".metadata.json", ".voucher"):
            assert (old_base + suffix, new_base + suffix) in moved
        # Absent sidecars (.png, .aax, .aaxc) are never moved.
        for suffix in (".png", ".aax", ".aaxc"):
            assert (old_base + suffix, new_base + suffix) not in moved

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
        # reclaims the plain name instead of being suffixed. Reservations are
        # keyed by the extension-stripped base, which is what `run` discards.
        path_first = _resolve_output_path(asin="B0AAAA")
        processing_logic._reserved_output_paths.discard(os.path.splitext(path_first)[0])
        path_next = _resolve_output_path(asin="B0BBBB")
        assert path_next == "/data/Bram Stoker/Dracula/Bram Stoker - Dracula.m4b"

    def test_reservation_is_keyed_by_the_extension_stripped_base(self):
        # v0.23.0 #5: every sidecar hangs off the extension-stripped base, so an
        # .mp3 book claiming the same base as an in-flight .m4b book IS a
        # collision — the audio files differ but the .pdf/.cue/.metadata.json
        # would silently overwrite each other.
        first = BookProcessor(asin="B0AAAA", job_id=1)
        second = BookProcessor(asin="B0BBBB", job_id=1)
        with mock.patch("os.path.exists", return_value=False):
            path_a = first._reserve_output_path("/data/A/Title/Title.m4b", "B0AAAA")
            path_b = second._reserve_output_path("/data/A/Title/Title.mp3", "B0BBBB")
        assert path_a == "/data/A/Title/Title.m4b"
        assert path_b == "/data/A/Title/Title_B0BBBB.mp3"
        assert first.is_duplicate is False
        assert second.is_duplicate is True


class TestSiblingExtensionOnDisk:
    """v0.23.0 #5, on-disk half: a file already sitting at the SIBLING audio
    extension shares our sidecar base, so it collides — unless it verifiably
    belongs to this same book (our own earlier download in the other format),
    which the finalize-time stale-file cleanup is what removes."""

    def _reserve(self, base_output_path, present, owners):
        """Reserve `base_output_path` with `present` paths on disk, each tracked
        in the DB under the ASIN given in `owners` (absent = untracked)."""
        processor = BookProcessor(asin="B0OURS", job_id=1)
        con = mock.MagicMock()
        con.__enter__.return_value = con

        def execute(query, params=None):
            cursor = mock.MagicMock()
            tracked = owners.get(params[0]) if params else None
            cursor.fetchone.return_value = {"asin": tracked} if tracked else None
            return cursor

        con.execute.side_effect = execute

        with (
            mock.patch.object(processing_logic, "get_db_connection", return_value=con),
            mock.patch("os.path.exists", side_effect=lambda p: p in present),
        ):
            path = processor._reserve_output_path(base_output_path, "B0OURS")
        return processor, path

    def test_foreign_file_at_sibling_extension_forces_suffix(self):
        processor, path = self._reserve(
            "/data/A/Title/Title.m4b",
            present={"/data/A/Title/Title.mp3"},
            owners={"/data/A/Title/Title.mp3": "B0OTHER"},
        )
        assert path == "/data/A/Title/Title_B0OURS.m4b"
        assert processor.is_duplicate is True

    def test_foreign_m4a_file_at_sibling_extension_forces_suffix(self):
        # W2 regression: an imported ".m4a" book occupying the base is just as much
        # a collision as an ".mp3" one — every audio extension is probed, not just
        # the other output format.
        processor, path = self._reserve(
            "/data/A/Title/Title.m4b",
            present={"/data/A/Title/Title.m4a"},
            owners={"/data/A/Title/Title.m4a": "B0OTHER"},
        )
        assert path == "/data/A/Title/Title_B0OURS.m4b"
        assert processor.is_duplicate is True

    def test_same_asin_file_at_sibling_extension_keeps_the_plain_name(self):
        processor, path = self._reserve(
            "/data/A/Title/Title.mp3",
            present={"/data/A/Title/Title.m4b"},
            owners={"/data/A/Title/Title.m4b": "B0OURS"},
        )
        assert path == "/data/A/Title/Title.mp3"
        assert processor.is_duplicate is False

    def test_untracked_file_at_sibling_extension_forces_suffix(self):
        # Untracked and unprobeable (the ffprobe mock returns no embedded ASIN),
        # so it can't be proven ours — the safe answer is a unique name.
        processor = BookProcessor(asin="B0OURS", job_id=1)
        con = mock.MagicMock()
        con.__enter__.return_value = con
        con.execute.return_value.fetchone.return_value = None
        with (
            mock.patch.object(processing_logic, "get_db_connection", return_value=con),
            mock.patch("os.path.exists", side_effect=lambda p: p == "/data/A/Title/Title.mp3"),
            mock.patch.object(processor, "_probe_file_asin", return_value=None),
        ):
            path = processor._reserve_output_path("/data/A/Title/Title.m4b", "B0OURS")
        assert path == "/data/A/Title/Title_B0OURS.m4b"
        assert processor.is_duplicate is True


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

    def test_cross_extension_in_flight_collision_sets_flag(self):
        # v0.23.0 #5: reservations are keyed by the extension-stripped base, so
        # the duplicate flag fires across a format difference too — here the
        # in-flight .mp3 book claims the base an .m4b book then wants.
        first = BookProcessor(asin="B0AAAA", job_id=1)
        second = BookProcessor(asin="B0BBBB", job_id=1)
        with mock.patch("os.path.exists", return_value=False):
            first._reserve_output_path("/data/A/Title/Title.mp3", "B0AAAA")
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


class TestOutputFormatPathSelection:
    """Phase 5: resolve_output_format routes the finalize path. "mp3" submits a
    single MP3 encode task (no chunk/merge) and builds a .mp3 target; "original"
    still remuxes; "m4b" still chunk-encodes."""

    def _run(self, output_format, audio_file="/tmp/x/master_intermediate.m4b", chapters=None):
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
                return_value={"naming": {}, "conversion": {"output_format": output_format}},
            ),
            mock.patch.object(processing_logic, "get_db_connection", return_value=con),
            mock.patch.object(processing_logic, "prepare_book_assets", return_value=(context, None)) as prepare,
            mock.patch.object(processing_logic, "build_base_output_path", return_value="/data/A/T/T") as bbop,
            mock.patch("os.path.exists", return_value=False),
            mock.patch("os.makedirs"),
            mock.patch.object(processing_logic, "_yield_progress"),
            mock.patch.object(processing_logic.task_runner, "submit_task", side_effect=submitted.append),
        ):
            processor._prepare_and_spawn_encode_tasks()

        return processor, submitted, prepare, bbop

    def test_mp3_submits_single_encode_task(self):
        processor, submitted, prepare, _ = self._run(
            "mp3",
            chapters=[{"start_offset_ms": 0, "length_ms": 1000}, {"start_offset_ms": 1000, "length_ms": 1000}],
        )
        assert len(submitted) == 1
        assert submitted[0].func == processor._encode_mp3_and_finalize
        # It runs at ENCODE_CHAPTER priority (it *is* the encode work).
        assert submitted[0].priority == processing_logic.TaskPriority.ENCODE_CHAPTER
        # No per-chapter chunking was set up.
        assert processor.total_chunks == 0
        # MP3 is a re-encode, so prepare is NOT told to skip auto-chunking.
        assert prepare.call_args.kwargs.get("lossless") is False

    def test_mp3_builds_mp3_extension_path(self):
        _, _, _, bbop = self._run("mp3")
        assert bbop.call_args.kwargs["ext"] == ".mp3"

    def test_m4b_builds_m4b_extension_path(self):
        _, _, _, bbop = self._run("m4b")
        assert bbop.call_args.kwargs["ext"] == ".m4b"

    def test_original_builds_m4b_extension_path(self):
        # Original remux still lands in an .m4b container.
        _, _, _, bbop = self._run("original")
        assert bbop.call_args.kwargs["ext"] == ".m4b"

    def test_original_with_aac_master_remuxes(self):
        processor, submitted, prepare, _ = self._run("original")
        assert len(submitted) == 1
        assert submitted[0].func == processor._remux_and_finalize
        assert prepare.call_args.kwargs.get("lossless") is True


class TestRemuxFinalize:
    """The lossless remux task shares the same success finalization (verify +
    DOWNLOADED + PDF) as the merge task, and reports a distinct failure."""

    def _run(self, remux_success, verify_result=(True, None)):
        processor = BookProcessor(asin="B0OURS", job_id=1)
        processor.final_output_path = "/data/A/Title/Title.m4b"
        con = mock.MagicMock()
        con.__enter__.return_value = con
        con.execute.return_value.fetchone.return_value = {"runtime_min": 60, "filepath": None}
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
        con.execute.return_value.fetchone.return_value = {"runtime_min": 60, "filepath": None}
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


class TestMp3Finalize:
    """Phase 5: the single-pass MP3 encode task shares the same success
    finalization (verify + DOWNLOADED + PDF) as the other paths, reports a
    distinct failure, and — like the remux — keeps its (single-threaded LAME)
    duration out of the shared re-encode ETA model."""

    def _run(self, encode_success, verify_result=(True, None)):
        processor = BookProcessor(asin="B0OURS", job_id=1)
        processor.final_output_path = "/data/A/Title/Title.mp3"
        con = mock.MagicMock()
        con.__enter__.return_value = con
        con.execute.return_value.fetchone.return_value = {"runtime_min": 60, "filepath": None}
        with (
            mock.patch.object(processing_logic, "encode_book_mp3", return_value=encode_success),
            mock.patch.object(processing_logic, "get_db_connection", return_value=con),
            mock.patch.object(processing_logic, "record_conversion_time") as record_eta,
            mock.patch.object(processing_logic, "_yield_progress"),
            mock.patch.object(processor, "_verify_output_file", return_value=verify_result),
            mock.patch.object(processor, "_place_supplementary_pdf") as pdf,
            mock.patch.object(processor, "_place_sidecar_files"),
            mock.patch.object(processor, "_update_db_on_failure") as fail,
        ):
            processor._encode_mp3_and_finalize()
        downloaded = [c for c in con.execute.call_args_list if "status = 'DOWNLOADED'" in c.args[0]]
        return downloaded, fail, pdf, record_eta, processor

    def test_success_marks_downloaded(self):
        downloaded, fail, pdf, _record_eta, processor = self._run(encode_success=True)
        fail.assert_not_called()
        assert len(downloaded) == 1
        pdf.assert_called_once()
        assert processor._completion_event.is_set()

    def test_success_does_not_pollute_eta_history(self):
        _downloaded, _fail, _pdf, record_eta, _processor = self._run(encode_success=True)
        record_eta.assert_not_called()

    def test_encode_failure_reports_distinct_reason(self):
        downloaded, fail, _pdf, _record_eta, processor = self._run(encode_success=False)
        fail.assert_called_once_with("MP3 encode failed.")
        assert downloaded == []
        assert processor._completion_event.is_set()

    def test_failed_verification_blocks_downloaded(self):
        downloaded, fail, _pdf, _record_eta, _processor = self._run(
            encode_success=True, verify_result=(False, "truncated")
        )
        fail.assert_called_once_with("truncated")
        assert downloaded == []


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

    def test_cancellation_does_not_mark_error(self):
        # (None, None) is the cancellation signal, not a failure: the book must
        # NOT be marked ERROR — it stays in its prior status (NEW/MISSING) for a
        # later retry instead of being stranded in ERROR with a bogus message.
        fail = self._run_prepare_with((None, None))
        fail.assert_not_called()


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


class TestCancellationMidConversion:
    """L1: a book cancelled *while its ffmpeg is running* (merge/remux/encode
    report the -15 SIGTERM as a plain False/None, indistinguishable from a real
    error) must NOT be stranded in ERROR. The mid-subprocess cancel is treated
    like the prepare-phase (None, None) cancel: the book's status is left
    untouched for a later retry."""

    def _processor(self):
        # stop_event is UNSET at task entry (so _cancelled() lets the work start)
        # and is set while the subprocess "runs", exactly like a real cancel.
        processor = BookProcessor(asin="B0OURS", job_id=1, stop_event=Event())
        processor.final_output_path = "/data/A/Title/Title.m4b"
        return processor

    def test_merge_cancel_does_not_mark_error(self):
        processor = self._processor()

        def _cancel_then_fail(*_a, **_k):
            processor.stop_event.set()
            return False

        with (
            mock.patch.object(processing_logic, "merge_book_chunks", side_effect=_cancel_then_fail),
            mock.patch.object(processor, "_update_db_on_failure") as fail,
        ):
            processor._merge_and_finalize()
        fail.assert_not_called()
        assert processor._completion_event.is_set()

    def test_remux_cancel_does_not_mark_error(self):
        processor = self._processor()

        def _cancel_then_fail(*_a, **_k):
            processor.stop_event.set()
            return False

        with (
            mock.patch.object(processing_logic, "remux_book_lossless", side_effect=_cancel_then_fail),
            mock.patch.object(processor, "_update_db_on_failure") as fail,
        ):
            processor._remux_and_finalize()
        fail.assert_not_called()
        assert processor._completion_event.is_set()

    def test_encode_chunk_cancel_does_not_mark_error(self):
        processor = self._processor()
        processor.total_chunks = 1

        def _cancel_then_fail(*_a, **_k):
            processor.stop_event.set()
            return None  # chunk "failed" (really: SIGTERM'd)

        with (
            mock.patch.object(processing_logic, "encode_chapter_chunk", side_effect=_cancel_then_fail),
            mock.patch.object(processor, "_update_db_on_failure") as fail,
        ):
            processor._encode_and_track_chunk({"index": 0})
        fail.assert_not_called()
        assert processor._completion_event.is_set()

    def test_genuine_merge_failure_still_marks_error(self):
        # Contrast: with no cancellation the failure is real and IS recorded.
        processor = BookProcessor(asin="B0OURS", job_id=1, stop_event=Event())
        processor.final_output_path = "/data/A/Title/Title.m4b"
        with (
            mock.patch.object(processing_logic, "merge_book_chunks", return_value=False),
            mock.patch.object(processor, "_update_db_on_failure") as fail,
        ):
            processor._merge_and_finalize()
        fail.assert_called_once_with("Final merge of chapter chunks failed.")


class TestFailedVerificationCleanup:
    """L4: when merge/remux succeed but the output fails verification, the bad
    (truncated/corrupt) artifact is removed from disk so it doesn't linger at the
    final path looking like a real book until a later retry overwrites it."""

    def test_failed_verification_removes_output_file(self):
        processor = BookProcessor(asin="B0OURS", job_id=1)
        processor.final_output_path = "/data/A/Title/Title.m4b"
        with (
            mock.patch.object(processing_logic, "merge_book_chunks", return_value=True),
            mock.patch.object(processor, "_verify_output_file", return_value=(False, "truncated")),
            mock.patch.object(processor, "_update_db_on_failure"),
            mock.patch("os.path.exists", return_value=True),
            mock.patch("os.remove") as remove,
        ):
            processor._merge_and_finalize()
        remove.assert_called_once_with("/data/A/Title/Title.m4b")

    def test_remove_failure_is_non_fatal(self):
        # An unremovable bad file still marks the book ERROR; it doesn't raise.
        processor = BookProcessor(asin="B0OURS", job_id=1)
        processor.final_output_path = "/data/A/Title/Title.m4b"
        with (
            mock.patch.object(processing_logic, "merge_book_chunks", return_value=True),
            mock.patch.object(processor, "_verify_output_file", return_value=(False, "truncated")),
            mock.patch.object(processor, "_update_db_on_failure") as fail,
            mock.patch("os.path.exists", return_value=True),
            mock.patch("os.remove", side_effect=OSError("busy")),
        ):
            processor._merge_and_finalize()
        fail.assert_called_once_with("truncated")


class TestVerificationCancelPreservesFile:
    """WF#1 (adversarial review): a cancel firing during the final verification
    probe surfaces as a verification failure — the SIGTERM'd ffprobe returns no
    duration, so verification reports the file 'could not be read back.' That is a
    cancel, not corruption: the finished file must be left on disk and the book's
    status left untouched, NOT deleted and marked ERROR."""

    def test_cancel_during_verification_keeps_file_and_status(self):
        processor = BookProcessor(asin="B0OURS", job_id=1, stop_event=Event())
        processor.final_output_path = "/data/A/Title/Title.m4b"

        def _cancel_then_fail_verify(*_a, **_k):
            # The cancel fires while the verification probe runs.
            processor.stop_event.set()
            return (False, "Output file could not be read back (corrupt or unreadable).")

        with (
            mock.patch.object(processing_logic, "merge_book_chunks", return_value=True),
            mock.patch.object(processor, "_verify_output_file", side_effect=_cancel_then_fail_verify),
            mock.patch.object(processor, "_update_db_on_failure") as fail,
            mock.patch("os.path.exists", return_value=True),
            mock.patch("os.remove") as remove,
        ):
            processor._merge_and_finalize()
        remove.assert_not_called()
        fail.assert_not_called()
        assert processor._completion_event.is_set()

    def test_verification_failure_without_cancel_still_removes_and_errors(self):
        # Contrast: with the stop_event UNSET a genuine verification failure still
        # deletes the bad artifact and marks the book ERROR (the new cancel branch
        # doesn't swallow real corruption).
        processor = BookProcessor(asin="B0OURS", job_id=1, stop_event=Event())
        processor.final_output_path = "/data/A/Title/Title.m4b"
        with (
            mock.patch.object(processing_logic, "merge_book_chunks", return_value=True),
            mock.patch.object(processor, "_verify_output_file", return_value=(False, "truncated")),
            mock.patch.object(processor, "_update_db_on_failure") as fail,
            mock.patch("os.path.exists", return_value=True),
            mock.patch("os.remove") as remove,
        ):
            processor._merge_and_finalize()
        remove.assert_called_once_with("/data/A/Title/Title.m4b")
        fail.assert_called_once_with("truncated")


class TestProbeDurationRegistration:
    """L3: the output-verification duration probe runs as a registered
    subprocess so a job cancel can SIGTERM it."""

    def test_probe_registers_and_unregisters(self):
        proc = mock.MagicMock()
        proc.returncode = 0
        proc.communicate.return_value = ("3600.0", "")
        with (
            mock.patch.object(processing_logic.subprocess, "Popen", return_value=proc),
            mock.patch.object(processing_logic.process_registry, "register") as register,
            mock.patch.object(processing_logic.process_registry, "unregister") as unregister,
        ):
            result = processing_logic._probe_duration_seconds("/x.m4b", job_id=7)
        assert result == 3600.0
        register.assert_called_once_with(7, proc)
        unregister.assert_called_once_with(7, proc)


class TestBuildMetadataJson:
    """Phase 2: the curated metadata.json sidecar shaping (pure function)."""

    FULL_BOOK_INFO = {
        "asin": "B0OURS",
        "title": "Dracula",
        "subtitle": "The Original Classic",
        "authors": [{"name": "Bram Stoker"}, {"name": "Ghost Writer"}],
        "narrators": [{"name": "Simon Vance"}],
        "series": [{"title": "Gothic Classics", "sequence": "3"}],
        "release_date": "1897-05-26",
        "purchase_date": "2020-01-02",
        "publisher_name": "Audible Studios",
        "language": "english",
        "category_ladders": [
            {"ladder": [{"name": "Fiction"}, {"name": "Horror"}]},
            {"ladder": [{"name": "Classics"}]},
        ],
        "runtime_length_min": 950,
        "merchandising_summary": "<p>A <br />vampire tale.</p>",
        "copy_right": "Public Domain",
    }

    def test_full_mapping(self):
        result = build_metadata_json(self.FULL_BOOK_INFO)
        assert result == {
            "asin": "B0OURS",
            "title": "Dracula",
            "subtitle": "The Original Classic",
            "authors": ["Bram Stoker", "Ghost Writer"],
            "narrators": ["Simon Vance"],
            "series": {"title": "Gothic Classics", "sequence": "3"},
            "release_date": "1897-05-26",
            "purchase_date": "2020-01-02",
            "publisher": "Audible Studios",
            "language": "english",
            "genres": ["Horror", "Classics"],  # last (most specific) rung of each ladder
            "runtime_length_min": 950,
            "description": "A \nvampire tale.",  # <p>/<br /> stripped, </p> -> newline, trimmed
            "copyright": "Public Domain",
        }

    def test_missing_keys_default_cleanly(self):
        # A sparse item (no series, authors, ladders, summary) still produces the
        # full key set with None / empty-list defaults — no KeyError.
        result = build_metadata_json({"asin": "B1", "title": "Bare"})
        assert result["asin"] == "B1"
        assert result["title"] == "Bare"
        assert result["authors"] == []
        assert result["narrators"] == []
        assert result["series"] is None
        assert result["genres"] == []
        assert result["description"] == ""
        assert result["publisher"] is None
        assert result["copyright"] is None

    def test_none_book_info_is_safe(self):
        # Defensive: a None book_info must not raise (best-effort sidecar).
        result = build_metadata_json(None)
        assert result["asin"] is None
        assert result["authors"] == []
        assert result["series"] is None

    def test_author_entries_without_name_are_dropped(self):
        result = build_metadata_json({"authors": [{"name": "A"}, {}, {"name": ""}]})
        assert result["authors"] == ["A"]

    def test_is_json_serializable(self):
        # The whole point is a file on disk: the dict must round-trip through json.
        text = json.dumps(build_metadata_json(self.FULL_BOOK_INFO))
        assert "Dracula" in text

    def test_title_override_replaces_the_api_title(self):
        # The caller resolves the effective title (custom title / "(Unabridged)"
        # cleanup) so the sidecar matches the embedded tags; every other field
        # still comes straight from the API item.
        result = build_metadata_json(self.FULL_BOOK_INFO, title_override="Dracula (Stripped)")
        assert result["title"] == "Dracula (Stripped)"
        assert result["subtitle"] == "The Original Classic"

    def test_title_override_omitted_keeps_the_api_title(self):
        assert build_metadata_json(self.FULL_BOOK_INFO)["title"] == "Dracula"
        assert build_metadata_json(self.FULL_BOOK_INFO, title_override=None)["title"] == "Dracula"


class TestGenerateCueSheet:
    """Phase 2: the .cue sidecar renderer (pure function)."""

    CHAPTERS = [
        {"title": "Opening", "start_offset_ms": 0},
        {"title": "Chapter 1", "start_offset_ms": 65_500},  # 1:05 and 500ms -> 37 frames
        {"title": "Chapter 2", "start_offset_ms": 3_600_000},  # exactly 60:00:00
    ]

    def test_header_and_file_type_m4b(self):
        cue = generate_cue_sheet(self.CHAPTERS, "Dracula.m4b", "Dracula", "Bram Stoker", "B0OURS")
        assert cue.startswith("REM ASIN B0OURS\n")
        assert 'PERFORMER "Bram Stoker"' in cue
        assert 'TITLE "Dracula"' in cue
        assert 'FILE "Dracula.m4b" WAVE' in cue
        assert cue.endswith("\n")

    def test_mp3_declares_mp3_file_type(self):
        cue = generate_cue_sheet(self.CHAPTERS, "Dracula.mp3", "Dracula", "Bram Stoker", "B0OURS")
        assert 'FILE "Dracula.mp3" MP3' in cue

    def test_track_numbering_and_index_times(self):
        cue = generate_cue_sheet(self.CHAPTERS, "x.m4b", "T", "A", "B0")
        assert "TRACK 01 AUDIO" in cue
        assert "TRACK 02 AUDIO" in cue
        assert "TRACK 03 AUDIO" in cue
        # 0 ms -> 00:00:00
        assert "INDEX 01 00:00:00" in cue
        # 65_500 ms -> 1 min, 5 sec, int(500 * 75 / 1000) = 37 frames
        assert "INDEX 01 01:05:37" in cue
        # 3_600_000 ms -> exactly 60 minutes (MM allowed to exceed 99 range)
        assert "INDEX 01 60:00:00" in cue

    def test_minutes_exceed_99(self):
        # A long book: 2h 5m 3s -> MM = 125, rendered as-is (not wrapped).
        chapters = [{"title": "Late", "start_offset_ms": (125 * 60 + 3) * 1000}]
        cue = generate_cue_sheet(chapters, "x.m4b", "T", "A", "B0")
        assert "INDEX 01 125:03:00" in cue

    def test_track_numbers_past_99_use_three_digits(self):
        chapters = [{"title": f"Ch {n}", "start_offset_ms": n * 1000} for n in range(105)]
        cue = generate_cue_sheet(chapters, "x.m4b", "T", "A", "B0")
        assert "TRACK 99 AUDIO" in cue
        assert "TRACK 100 AUDIO" in cue
        assert "TRACK 105 AUDIO" in cue

    def test_embedded_quotes_are_escaped(self):
        chapters = [{"title": 'The "Big" Chapter', "start_offset_ms": 0}]
        cue = generate_cue_sheet(chapters, "x.m4b", 'A "Quoted" Title', 'Au "thor"', "B0")
        assert 'TITLE "The \\"Big\\" Chapter"' in cue
        assert 'TITLE "A \\"Quoted\\" Title"' in cue
        assert 'PERFORMER "Au \\"thor\\""' in cue

    def test_missing_chapter_title_falls_back(self):
        cue = generate_cue_sheet([{"start_offset_ms": 0}], "x.m4b", "T", "A", "B0")
        assert 'TITLE "Chapter"' in cue


class TestPlaceSidecarFiles:
    """Phase 2: BookProcessor._place_sidecar_files wiring — best-effort placement
    gated on the four sidecar settings, next to the finished audiobook."""

    def _processor(self, tmp_path, context):
        processor = BookProcessor(asin="B0OURS", job_id=1, stop_event=Event())
        processor.final_output_path = str(tmp_path / "out" / "Dracula.m4b")
        (tmp_path / "out").mkdir()
        processor.context = context
        return processor

    def _settings(self, **conv):
        base = {
            "save_cover_alongside": False,
            "save_metadata_json": False,
            "create_cue_sheet": False,
            "retain_aax": False,
        }
        base.update(conv)
        return {"conversion": base}

    def test_all_off_writes_nothing(self, tmp_path):
        cover = tmp_path / "cover.jpg"
        cover.write_bytes(b"img")
        processor = self._processor(tmp_path, {"cover_file": str(cover), "book_info": {"title": "X"}})
        with mock.patch.object(processing_logic, "load_settings", return_value=self._settings()):
            processor._place_sidecar_files()
        assert list((tmp_path / "out").iterdir()) == []

    def test_all_on_writes_four_sidecars(self, tmp_path):
        cover = tmp_path / "cover.png"
        cover.write_bytes(b"img")
        raw = tmp_path / "book.aaxc"
        raw.write_bytes(b"raw")
        voucher = tmp_path / "book.voucher"
        voucher.write_bytes(b"{}")
        context = {
            "cover_file": str(cover),
            "raw_audio_file": str(raw),
            "voucher_file": str(voucher),
            "book_info": {"title": "Dracula", "authors": [{"name": "Bram Stoker"}]},
            "chapters": [{"title": "One", "start_offset_ms": 0}],
        }
        processor = self._processor(tmp_path, context)
        settings = self._settings(
            save_cover_alongside=True,
            save_metadata_json=True,
            create_cue_sheet=True,
            retain_aax=True,
        )
        with mock.patch.object(processing_logic, "load_settings", return_value=settings):
            processor._place_sidecar_files()
        out = tmp_path / "out"
        assert (out / "Dracula.png").exists()  # cover keeps its real extension
        assert (out / "Dracula.metadata.json").exists()
        assert (out / "Dracula.cue").exists()
        assert (out / "Dracula.aaxc").exists()  # raw master keeps its real extension
        assert (out / "Dracula.voucher").exists()
        # The metadata sidecar is the curated JSON.
        saved = json.loads((out / "Dracula.metadata.json").read_text(encoding="utf-8"))
        assert saved["title"] == "Dracula"
        assert saved["authors"] == ["Bram Stoker"]

    def test_retain_without_voucher_is_fine(self, tmp_path):
        # AAX titles have no voucher: raw is copied, no .voucher is produced.
        raw = tmp_path / "book.aax"
        raw.write_bytes(b"raw")
        context = {"raw_audio_file": str(raw), "voucher_file": None}
        processor = self._processor(tmp_path, context)
        with mock.patch.object(processing_logic, "load_settings", return_value=self._settings(retain_aax=True)):
            processor._place_sidecar_files()
        out = tmp_path / "out"
        assert (out / "Dracula.aax").exists()
        assert not (out / "Dracula.voucher").exists()

    def test_one_failure_does_not_block_others(self, tmp_path):
        # A missing cover file (enabled but absent) must not stop the cue sheet.
        context = {
            "cover_file": str(tmp_path / "does-not-exist.jpg"),
            "book_info": {"title": "Dracula", "authors": [{"name": "Bram Stoker"}]},
            "chapters": [{"title": "One", "start_offset_ms": 0}],
        }
        processor = self._processor(tmp_path, context)
        settings = self._settings(save_cover_alongside=True, create_cue_sheet=True)
        with mock.patch.object(processing_logic, "load_settings", return_value=settings):
            processor._place_sidecar_files()
        out = tmp_path / "out"
        assert not (out / "Dracula.jpg").exists()
        assert (out / "Dracula.cue").exists()

    def _title_settings(self, chapters=None):
        """Both title-bearing sidecars on, with an optional chapters block."""
        settings = self._settings(save_metadata_json=True, create_cue_sheet=True)
        if chapters is not None:
            settings["conversion"]["chapters"] = chapters
        return settings

    def _write_titled_sidecars(self, tmp_path, settings, custom_title=None):
        """Run the two title-bearing sidecars and return (json title, cue text)."""
        context = {
            "book_info": {"title": "Dracula (Unabridged)", "authors": [{"name": "Bram Stoker"}]},
            "chapters": [{"title": "One", "start_offset_ms": 0}],
        }
        processor = self._processor(tmp_path, context)
        processor.custom_title = custom_title
        with mock.patch.object(processing_logic, "load_settings", return_value=settings):
            processor._place_sidecar_files()
        out = tmp_path / "out"
        saved = json.loads((out / "Dracula.metadata.json").read_text(encoding="utf-8"))
        return saved["title"], (out / "Dracula.cue").read_text(encoding="utf-8")

    def test_strip_unabridged_on_strips_both_sidecar_titles(self, tmp_path):
        # The bug: the FFMETADATA tags were stripped but the sidecars were not,
        # so the file said "Dracula" and the sidecars next to it said
        # "Dracula (Unabridged)".
        json_title, cue = self._write_titled_sidecars(tmp_path, self._title_settings({"strip_unabridged": True}))
        assert json_title == "Dracula"
        assert 'TITLE "Dracula"' in cue

    def test_strip_unabridged_off_leaves_both_sidecar_titles_alone(self, tmp_path):
        json_title, cue = self._write_titled_sidecars(tmp_path, self._title_settings({"strip_unabridged": False}))
        assert json_title == "Dracula (Unabridged)"
        assert 'TITLE "Dracula (Unabridged)"' in cue

    def test_missing_chapters_block_defaults_to_off(self, tmp_path):
        # Old settings.json files have no conversion.chapters block at all.
        json_title, cue = self._write_titled_sidecars(tmp_path, self._title_settings())
        assert json_title == "Dracula (Unabridged)"
        assert 'TITLE "Dracula (Unabridged)"' in cue

    def test_custom_title_is_never_stripped(self, tmp_path):
        # A user's explicit title wins outright and is never transformed, even
        # when it carries "(Unabridged)" itself and the setting is on.
        json_title, cue = self._write_titled_sidecars(
            tmp_path,
            self._title_settings({"strip_unabridged": True}),
            custom_title="My Dracula (Unabridged)",
        )
        assert json_title == "My Dracula (Unabridged)"
        assert 'TITLE "My Dracula (Unabridged)"' in cue


class TestParseTimestampDate:
    """Phase 9: the pure date parser behind conversion.file_timestamp_source."""

    def test_bare_release_date(self):
        assert _parse_timestamp_date("2019-06-27") == datetime(2019, 6, 27).timestamp()

    def test_full_iso_purchase_date_uses_leading_ten_chars(self):
        assert _parse_timestamp_date("2023-04-05T06:07:08.000Z") == datetime(2023, 4, 5).timestamp()

    @pytest.mark.parametrize("value", [None, "", "N/A", "garbage", "2019-13-99", "2019", 0])
    def test_unusable_values_return_none(self, value):
        assert _parse_timestamp_date(value) is None


class TestApplyFileTimestamps:
    """Phase 9: BookProcessor._apply_file_timestamps — off by default, stamps the
    audiobook plus every sidecar that actually exists when switched on."""

    def _processor(self, tmp_path, book_info):
        processor = BookProcessor(asin="B0OURS", job_id=1, stop_event=Event())
        (tmp_path / "out").mkdir()
        processor.final_output_path = str(tmp_path / "out" / "Dracula.m4b")
        (tmp_path / "out" / "Dracula.m4b").write_bytes(b"audio")
        processor.context = {"book_info": book_info}
        return processor

    def _run(self, processor, source):
        settings = {"conversion": {"file_timestamp_source": source}}
        with mock.patch.object(processing_logic, "load_settings", return_value=settings):
            processor._apply_file_timestamps()

    @pytest.mark.parametrize("source", ["none", "bogus"])
    def test_disabled_leaves_mtime_alone(self, tmp_path, source):
        processor = self._processor(tmp_path, {"release_date": "2019-06-27"})
        book = tmp_path / "out" / "Dracula.m4b"
        before = book.stat().st_mtime
        self._run(processor, source)
        assert book.stat().st_mtime == before

    def test_missing_setting_defaults_to_off(self, tmp_path):
        # Old settings.json files have no conversion.file_timestamp_source key.
        processor = self._processor(tmp_path, {"release_date": "2019-06-27"})
        book = tmp_path / "out" / "Dracula.m4b"
        before = book.stat().st_mtime
        with mock.patch.object(processing_logic, "load_settings", return_value={"conversion": {}}):
            processor._apply_file_timestamps()
        assert book.stat().st_mtime == before

    def test_release_date_stamps_book_and_existing_sidecars(self, tmp_path):
        processor = self._processor(tmp_path, {"release_date": "2019-06-27"})
        out = tmp_path / "out"
        (out / "Dracula.pdf").write_bytes(b"pdf")
        (out / "Dracula.cue").write_text("cue", encoding="utf-8")
        self._run(processor, "release_date")
        expected = datetime(2019, 6, 27).timestamp()
        assert (out / "Dracula.m4b").stat().st_mtime == expected
        assert (out / "Dracula.pdf").stat().st_mtime == expected
        assert (out / "Dracula.cue").stat().st_mtime == expected
        # Sidecars that were never produced are simply skipped, not created.
        assert not (out / "Dracula.jpg").exists()
        assert not (out / "Dracula.voucher").exists()

    def test_purchase_date_source(self, tmp_path):
        processor = self._processor(
            tmp_path, {"release_date": "2019-06-27", "purchase_date": "2023-04-05T06:07:08.000Z"}
        )
        self._run(processor, "purchase_date")
        assert (tmp_path / "out" / "Dracula.m4b").stat().st_mtime == datetime(2023, 4, 5).timestamp()

    def test_unparseable_date_skips_silently(self, tmp_path):
        processor = self._processor(tmp_path, {"release_date": "N/A"})
        book = tmp_path / "out" / "Dracula.m4b"
        before = book.stat().st_mtime
        self._run(processor, "release_date")
        assert book.stat().st_mtime == before

    def test_missing_book_info_skips_silently(self, tmp_path):
        processor = self._processor(tmp_path, None)
        book = tmp_path / "out" / "Dracula.m4b"
        before = book.stat().st_mtime
        self._run(processor, "release_date")
        assert book.stat().st_mtime == before


class TestFinalizeSuccessStampOrdering:
    """Phase 9 (W2): _apply_file_timestamps must run AFTER the sidecars are
    placed. The stamp only touches files that already exist on disk, so a
    reordering that groups it with the other settings-gated steps would leave
    every .cue / .metadata.json / retained .aax carrying the download time while
    the audiobook itself carries the release date — with the rest of the suite
    still green. This test pins the ordering itself."""

    def test_timestamps_applied_after_sidecars(self):
        processor = BookProcessor(asin="B0OURS", job_id=1)
        processor.final_output_path = "/data/A/Title/Title.m4b"
        con = mock.MagicMock()
        con.__enter__.return_value = con

        # A single parent mock records the finalization calls in one shared
        # sequence, so mock_calls reflects the real invocation order.
        recorder = mock.MagicMock()
        with (
            mock.patch.object(processing_logic, "get_db_connection", return_value=con),
            mock.patch.object(processing_logic, "_yield_progress"),
            mock.patch.object(processor, "_verify_output_file", return_value=(True, None)),
            mock.patch.object(processor, "_place_supplementary_pdf") as pdf,
            mock.patch.object(processor, "_place_sidecar_files") as sidecars,
            mock.patch.object(processor, "_apply_file_timestamps") as stamp,
        ):
            recorder.attach_mock(pdf, "pdf")
            recorder.attach_mock(sidecars, "sidecars")
            recorder.attach_mock(stamp, "stamp")
            processor._finalize_success(conversion_start_time=0, record_eta=False)

        called = [name for name, _args, _kwargs in recorder.mock_calls]
        assert called == ["pdf", "sidecars", "stamp"]


class TestCleanupStaleFiles:
    """v0.23.0 #2 (D5): a re-download re-derives its output path from the current
    settings, so a changed format or naming template lands the new file somewhere
    else and leaves the old one behind untracked. That leftover is deleted only
    with consent (the job's prompt answer or the saved setting), only inside the
    output root, and never when the "old" sidecars are this run's own."""

    NEW = "/data/Author/Title/Title.m4b"
    OLD = "/data/Author/Old Title/Old Title.m4b"

    def _run(
        self,
        previous_path,
        *,
        new_path=None,
        param=None,
        setting=False,
        present=None,
        remove_error=None,
        other_books=(),
    ):
        """Drive _cleanup_stale_files with the filesystem faked out, and return
        the set of paths it tried to unlink plus the empty-dir cleanup mock. The
        paths are deliberately fake /data ones: the guard is hard-coded to the
        real output root, so nothing here may touch a real file.

        `other_books` are (asin, filepath) pairs for OTHER tracked books, which is
        what the shared-base check reads to decide whether the old base's sidecars
        are jointly owned."""
        processor = BookProcessor(asin="B0OURS", job_id=1)
        processor.final_output_path = new_path or self.NEW
        processor.cleanup_stale_files = param

        if present is None:
            present = {previous_path} if previous_path else set()
        settings = {"job": {"download": {"cleanup_stale_files": setting}}}

        con = mock.MagicMock()
        con.__enter__.return_value = con
        tracked = [{"asin": "B0OURS", "filepath": previous_path}] if previous_path else []
        tracked += [{"asin": asin, "filepath": path} for asin, path in other_books]
        con.execute.return_value.fetchall.return_value = tracked

        with (
            mock.patch.object(processing_logic, "load_settings", return_value=settings),
            mock.patch.object(processing_logic, "get_db_connection", return_value=con),
            mock.patch("os.path.exists", side_effect=lambda p: p in present),
            mock.patch("os.remove", side_effect=remove_error) as remove,
            mock.patch.object(processing_logic, "_cleanup_empty_dirs") as cleanup_dirs,
        ):
            processor._cleanup_stale_files(previous_path)

        return {call.args[0] for call in remove.call_args_list}, cleanup_dirs

    def test_different_base_deletes_old_audio_and_sidecars(self):
        old_base = "/data/Author/Old Title/Old Title"
        removed, cleanup_dirs = self._run(
            self.OLD,
            param=True,
            present={self.OLD, old_base + ".pdf", old_base + ".cue", old_base + ".metadata.json"},
        )
        assert removed == {self.OLD, old_base + ".pdf", old_base + ".cue", old_base + ".metadata.json"}
        # Sidecars that weren't there are not touched.
        assert old_base + ".jpg" not in removed
        cleanup_dirs.assert_called_once_with("/data/Author/Old Title")

    def test_extension_only_change_keeps_the_shared_sidecars(self):
        # The critical subtlety: "Title.mp3" -> "Title.m4b" leaves the base equal,
        # so the "old" sidecars ARE the ones this run just wrote. Only the old
        # audio file may go.
        old = "/data/Author/Title/Title.mp3"
        base = "/data/Author/Title/Title"
        removed, cleanup_dirs = self._run(
            old,
            param=True,
            present={old, base + ".pdf", base + ".cue", base + ".metadata.json"},
        )
        assert removed == {old}
        cleanup_dirs.assert_called_once_with("/data/Author/Title")

    def test_shared_base_in_the_db_keeps_the_sidecars(self):
        # B1 regression: a library made before same-base collisions were prevented
        # can hold two books at one base under different audio extensions. The other
        # book's row still points there, so those sidecars are its ONLY cover / PDF /
        # cue / metadata — the old audio file goes, the sidecars stay.
        old_base = "/data/Author/Old Title/Old Title"
        removed, cleanup_dirs = self._run(
            self.OLD,
            param=True,
            present={self.OLD, old_base + ".jpg", old_base + ".pdf", old_base + ".metadata.json"},
            other_books=(("B0OTHER", old_base + ".mp3"),),
        )
        assert removed == {self.OLD}
        cleanup_dirs.assert_called_once_with("/data/Author/Old Title")

    def test_shared_base_on_disk_keeps_the_sidecars(self):
        # B1 regression, the untracked half: nothing in the DB shares the base, but
        # a sibling audio file is still sitting on it, so the sidecars are not
        # provably ours alone.
        old_base = "/data/Author/Old Title/Old Title"
        removed, _cleanup_dirs = self._run(
            self.OLD,
            param=True,
            present={self.OLD, old_base + ".m4a", old_base + ".jpg", old_base + ".cue"},
        )
        assert removed == {self.OLD}

    def test_unshared_base_still_sweeps_the_sidecars(self):
        # The control for the two above: other books exist, but none of them lives
        # at the old base, so the sweep proceeds exactly as before.
        old_base = "/data/Author/Old Title/Old Title"
        removed, _cleanup_dirs = self._run(
            self.OLD,
            param=True,
            present={self.OLD, old_base + ".jpg", old_base + ".cue"},
            other_books=(("B0OTHER", "/data/Author/Other/Other.m4b"),),
        )
        assert removed == {self.OLD, old_base + ".jpg", old_base + ".cue"}

    def test_symlinked_alias_of_the_new_path_is_never_deleted(self, tmp_path):
        # W4 regression: /data/Author as a symlink to another mount is a plausible
        # layout. The tracked previous path and this run's output are then the same
        # file reached two ways — abspath strings differ while realpath agrees, and
        # deleting would destroy the file this run just wrote. Real files here (no
        # os.path.exists fake), with only os.remove stubbed so nothing is lost if
        # the guard regresses.
        real_dir = tmp_path / "library" / "Author"
        real_dir.mkdir(parents=True)
        book = real_dir / "Title.m4b"
        book.write_bytes(b"audio")
        alias_dir = tmp_path / "Author"
        alias_dir.symlink_to(real_dir)

        processor = BookProcessor(asin="B0OURS", job_id=1)
        processor.final_output_path = str(book)
        processor.cleanup_stale_files = True

        with (
            mock.patch.object(processing_logic, "load_settings", return_value={}),
            mock.patch("os.remove") as remove,
            mock.patch.object(processing_logic, "_cleanup_empty_dirs") as cleanup_dirs,
        ):
            processor._cleanup_stale_files(str(alias_dir / "Title.m4b"))

        remove.assert_not_called()
        cleanup_dirs.assert_not_called()

    def test_missing_old_file_is_a_noop(self):
        removed, cleanup_dirs = self._run(self.OLD, param=True, present=set())
        assert removed == set()
        cleanup_dirs.assert_not_called()

    def test_no_previous_path_is_a_noop(self):
        removed, cleanup_dirs = self._run(None, param=True)
        assert removed == set()
        cleanup_dirs.assert_not_called()

    def test_same_path_is_a_noop(self):
        # The re-download overwrote its own file; nothing was left behind.
        removed, cleanup_dirs = self._run(self.NEW, param=True)
        assert removed == set()
        cleanup_dirs.assert_not_called()

    def test_path_outside_the_output_root_is_never_deleted(self):
        outside = "/mnt/somewhere/else/Title.m4b"
        removed, cleanup_dirs = self._run(outside, param=True, present={outside})
        assert removed == set()
        cleanup_dirs.assert_not_called()

    def test_switch_off_deletes_nothing(self):
        # No job param and the setting off: status quo, the old file stays.
        removed, cleanup_dirs = self._run(self.OLD, param=None, setting=False)
        assert removed == set()
        cleanup_dirs.assert_not_called()

    def test_setting_on_without_a_job_param_deletes(self):
        # A scheduled job carries no params at all, so the saved setting has to be
        # honored on its own.
        removed, _cleanup_dirs = self._run(self.OLD, param=None, setting=True)
        assert removed == {self.OLD}

    def test_explicit_decline_vetoes_the_setting(self):
        # D5: "declining the prompt = leave files". The flag is tri-state, so an
        # unticked checkbox (False) is not the same as no answer (None) and must not
        # fall through to the saved setting, even with it switched on.
        removed, cleanup_dirs = self._run(self.OLD, param=False, setting=True)
        assert removed == set()
        cleanup_dirs.assert_not_called()

    def test_missing_setting_key_defaults_to_off(self):
        # Old settings.json files have no job.download.cleanup_stale_files key.
        processor = BookProcessor(asin="B0OURS", job_id=1)
        processor.final_output_path = self.NEW
        with (
            mock.patch.object(processing_logic, "load_settings", return_value={"job": {"download": {}}}),
            mock.patch("os.path.exists", return_value=True),
            mock.patch("os.remove") as remove,
        ):
            processor._cleanup_stale_files(self.OLD)
        remove.assert_not_called()

    def test_unlink_failure_does_not_propagate(self):
        # Best-effort: a finished book is never failed over a cleanup problem.
        old_base = "/data/Author/Old Title/Old Title"
        removed, cleanup_dirs = self._run(
            self.OLD,
            param=True,
            present={self.OLD, old_base + ".cue"},
            remove_error=OSError("permission denied"),
        )
        # Every unlink was still attempted, and the empty-dir sweep still ran.
        assert removed == {self.OLD, old_base + ".cue"}
        cleanup_dirs.assert_called_once_with("/data/Author/Old Title")

    def test_finalize_captures_the_previous_path_before_the_update(self):
        # The UPDATE overwrites filepath, so reading it afterwards would only ever
        # see the NEW path and the cleanup would never fire. This pins the order.
        processor = BookProcessor(asin="B0OURS", job_id=1)
        processor.final_output_path = self.NEW

        con = mock.MagicMock()
        con.__enter__.return_value = con
        con.execute.return_value.fetchone.return_value = {"filepath": self.OLD}

        with (
            mock.patch.object(processing_logic, "get_db_connection", return_value=con),
            mock.patch.object(processing_logic, "_yield_progress"),
            mock.patch.object(processor, "_verify_output_file", return_value=(True, None)),
            mock.patch.object(processor, "_place_supplementary_pdf"),
            mock.patch.object(processor, "_place_sidecar_files"),
            mock.patch.object(processor, "_apply_file_timestamps"),
            mock.patch.object(processor, "_cleanup_stale_files") as cleanup,
        ):
            processor._finalize_success(conversion_start_time=0, record_eta=False)

        cleanup.assert_called_once_with(self.OLD)

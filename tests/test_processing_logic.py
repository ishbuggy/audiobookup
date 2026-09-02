# tests/test_processing_logic.py

import contextlib
import json
import logging
import os
import shutil
import sqlite3
import time
from datetime import datetime
from threading import Barrier, Event, Lock, Thread
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
    render_chapter_filename,
)


@pytest.fixture(autouse=True)
def _clear_output_reservations():
    """The output-path reservation set is module-level state shared across
    BookProcessor instances; clear it around every test so reservations from
    one test can't leak into the next."""
    processing_logic._reserved_output_paths.clear()
    yield
    processing_logic._reserved_output_paths.clear()


class _FakeCursor:
    """The two-method slice of sqlite3.Cursor the code under test uses."""

    def __init__(self, rows):
        self.rows = list(rows)

    def fetchone(self):
        return self.rows[0] if self.rows else None

    def fetchall(self):
        return self.rows


class _FakeDb:
    """
    A stand-in for the connection `processing_logic.get_db_connection` hands out,
    answering the handful of queries the lifecycle paths make from plain lists.

    Richer than a bare MagicMock because the split-aware paths ask several
    DIFFERENT questions of one connection — the book row, this book's part rows,
    who owns a given path, and the two whole-table ownership scans — and a test
    that answers them all with one canned result can't tell them apart.
    """

    def __init__(self, book_row=None, part_rows=(), tracked=(), tracked_parts=(), update_error=None):
        self.book_row = book_row
        self.part_rows = list(part_rows)  # this book's own part filepaths
        self.tracked = list(tracked)  # (asin, filepath) rows of `audiobooks`
        self.tracked_parts = list(tracked_parts)  # (asin, filepath) rows of `book_files`
        # Raised by the `UPDATE audiobooks` write, for the "the files moved and
        # then the database refused them" case (a briefly locked library.db).
        self.update_error = update_error
        self.executed = []
        self.commits = 0

    # --- connection protocol -------------------------------------------------
    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        return False

    def commit(self):
        self.commits += 1

    def close(self):
        pass

    # --- queries -------------------------------------------------------------
    def execute(self, query, params=None):
        normalized = " ".join(query.split())
        if self.update_error is not None and normalized.startswith("UPDATE audiobooks"):
            raise self.update_error  # before recording it: the write never landed.
        self.executed.append((normalized, params))
        return _FakeCursor(self._answer(normalized, params))

    def executemany(self, query, rows):
        self.executed.append((" ".join(query.split()), list(rows)))
        return _FakeCursor([])

    def _answer(self, query, params):
        if query.startswith("SELECT filepath FROM book_files WHERE asin"):
            return [{"filepath": path} for path in self.part_rows]
        if query.startswith("SELECT asin FROM audiobooks WHERE filepath"):
            return [{"asin": asin} for asin, path in self.tracked if path == params[0]]
        if query.startswith("SELECT asin FROM book_files WHERE filepath"):
            return [{"asin": asin} for asin, path in self.tracked_parts if path == params[0]]
        if query.startswith("SELECT asin, filepath FROM audiobooks"):
            return [{"asin": asin, "filepath": path} for asin, path in self.tracked]
        if query.startswith("SELECT asin, filepath FROM book_files"):
            return [{"asin": asin, "filepath": path} for asin, path in self.tracked_parts]
        if query.startswith("SELECT"):
            return [self.book_row] if self.book_row else []
        return []

    def inserted_parts(self):
        """The (asin, part_index, filepath) rows a replace_book_files write made."""
        for query, rows in self.executed:
            if query.startswith("INSERT INTO book_files"):
                return rows
        return []

    def updates(self):
        """The parameter tuples of every `UPDATE audiobooks` this connection saw."""
        return [params for query, params in self.executed if query.startswith("UPDATE audiobooks")]


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

    `path_exists` means "a file is sitting at the book's TARGET path", not "every
    path in the universe exists": the collision allocator now re-validates the
    ASIN-suffixed name it falls back to (#28), and a universe where that name is
    occupied too would legitimately keep walking to "_2".
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

    settings = {
        "naming": {
            "template": template,
            "truncate_subtitle": truncate_subtitle,
            "apply_custom_to_filenames": apply_custom_to_filenames,
            "folder_template": folder_template,
            "file_template": file_template,
        }
    }
    # The one path the fake filesystem knows about, derived the same way PREPARE
    # derives it so the two can't drift apart.
    author, title = processing_logic._effective_naming_names(book_row, settings)
    target_path = build_base_output_path(
        settings,
        asin,
        author,
        title,
        book_row["narrator"],
        book_row["publisher"],
        series=book_row["series"],
        series_sequence=book_row["series_sequence"],
        release_date=book_row["release_date"],
        language=book_row["language"],
    )

    with (
        mock.patch.object(processing_logic, "load_settings", return_value=settings),
        mock.patch.object(processing_logic, "get_db_connection", return_value=con),
        mock.patch.object(processing_logic, "prepare_book_assets", return_value=(None, None)),
        mock.patch("os.path.exists", side_effect=lambda p: path_exists and p == target_path),
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

    def test_fallback_names_the_file_when_author_and_title_both_sanitize_away(self):
        # M10: " . " is truthy, so it never takes the "Unknown ..." branch, but it
        # sanitizes to nothing — the fallback used to produce a file called " - ".
        path = build_base_output_path({"naming": {"template": "{author}/{title}"}}, "B0OURS", " . ", " .. ", None, None)
        assert path == "/data/Unknown Author - Unknown Title.m4b"

    def test_fallback_names_the_file_when_only_the_author_sanitizes_away(self):
        # The one-sided case, for the same reason: "- Dracula" is not a name.
        path = build_base_output_path({"naming": {"template": "{author}"}}, "B0OURS", "...", "Dracula", None, None)
        assert path == "/data/Unknown Author - Dracula.m4b"

    def test_fallback_is_generic_when_the_assembled_name_also_strips_away(self):
        # M10, the half the first fix left open: "-" survives _sanitize_filename
        # and is truthy, so neither half takes the "Unknown ..." branch, but the
        # assembled "- - -" reduces to nothing under the same strip set — the
        # fallback used to rebuild and emit the very name it had just rejected.
        path = build_base_output_path({"naming": {"template": "{author}/{title}"}}, "B0OURS", "-", "-", None, None)
        assert path == "/data/Unknown Author - Unknown Title.m4b"

    def test_partially_strippable_fallback_is_left_alone(self):
        # The other direction: only a fallback that strips to nothing is replaced.
        # Here the title survives, so the assembled name still says something and
        # is used as-is rather than swapped for the generic one.
        path = build_base_output_path(
            {"naming": {"template": "{author}/{author}"}}, "B0OURS", "-", "Dracula", None, None
        )
        assert path == "/data/- - Dracula.m4b"

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


def _render_chapter(
    template="{title} - {ch} - {ch_title}",
    part_number=1,
    part_total=1,
    chapter_title="Chapter One",
    **overrides,
):
    """
    Call render_chapter_filename with the same book BOOK_ROW describes, so a test
    only has to name the part of the input it cares about. Book tags are passed
    by keyword, matching how a caller with a DB row in hand would do it.
    `part_number` is 1-based, like the renderer's own parameter (and unlike the
    zero-based `part_index` column db.replace_book_files writes).
    """
    book = {
        "asin": "B0OURS",
        "author": "Bram Stoker",
        "title": "Dracula",
        "narrator": "Simon Vance",
        "publisher": "Audible Studios",
    }
    book.update(overrides)
    return render_chapter_filename(template, part_number, part_total, chapter_title, **book)


class TestRenderChapterFilename:
    """v0.24.0 Phase 1 (D4): the pure per-part filename renderer. It returns a
    bare filename — no directory, no extension — that a later phase pairs with
    the directory and extension from build_base_output_path."""

    def test_default_template(self):
        assert _render_chapter() == "Dracula - 1 - Chapter One"

    def test_returns_a_bare_name_with_no_directory_or_extension(self):
        name = _render_chapter(template="{author}/{title} - {ch}")
        # Only the final segment survives: a template carrying folder levels must
        # not inject directories into what is one filename segment.
        assert name == "Dracula - 1"
        assert "/" not in name
        assert not name.endswith(".m4b")

    @pytest.mark.parametrize(
        ("part_number", "part_total", "expected_ch"),
        [
            (1, 1, "1"),  # single part -> width 1
            (1, 9, "1"),  # 9 parts -> still width 1
            (9, 9, "9"),
            (1, 10, "01"),  # 10 parts -> width 2
            (10, 10, "10"),  # the boundary case: part 10 of 10 is "10", not "010"
            (1, 150, "001"),  # 150 parts -> width 3
            (99, 150, "099"),
            (150, 150, "150"),
        ],
    )
    def test_ch_is_zero_padded_to_the_width_of_the_part_count(self, part_number, part_total, expected_ch):
        name = _render_chapter(template="{ch}", part_number=part_number, part_total=part_total)
        assert name == expected_ch

    def test_ch_total_is_not_padded(self):
        name = _render_chapter(template="{ch} of {ch_total}", part_number=7, part_total=120)
        assert name == "007 of 120"

    def test_missing_ch_placeholder_is_appended(self, caplog):
        with caplog.at_level(logging.WARNING):
            name = _render_chapter(template="{title} - {ch_title}", part_number=2, part_total=12)
        assert name == "Dracula - Chapter One - 02"
        assert "{ch}" in caplog.text

    def test_missing_ch_placeholder_logs_a_warning(self, caplog):
        with caplog.at_level(logging.WARNING):
            _render_chapter(template="{title}")
        warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
        assert len(warnings) == 1
        assert "B0OURS" in warnings[0].getMessage()

    def test_present_ch_placeholder_logs_nothing(self, caplog):
        with caplog.at_level(logging.WARNING):
            _render_chapter()
        assert [r for r in caplog.records if r.levelno == logging.WARNING] == []

    def test_chapter_title_is_sanitized_like_a_book_title(self):
        # Same rules as the book path: forbidden characters become '_', and the
        # drop-segment cleanup strips the trailing '_' left by the '?'.
        name = _render_chapter(template="{ch} - {ch_title}", chapter_title="AC/DC: Live?")
        assert name == "1 - AC_DC_ Live"

    def test_chapter_title_cannot_create_directories(self):
        name = _render_chapter(template="{ch_title} - {ch}", chapter_title="Part 1/2")
        assert name == "Part 1_2 - 1"

    def test_empty_chapter_title_leaves_no_dangling_separator(self):
        assert _render_chapter(chapter_title=None) == "Dracula - 1"
        assert _render_chapter(chapter_title="") == "Dracula - 1"

    def test_book_placeholders_render_alongside_chapter_ones(self):
        name = _render_chapter(
            template="{author} - {series} {series_part} ({year}, {language}, {asin}) - {ch} - {ch_title}",
            part_number=3,
            part_total=40,
            series="Dune",
            series_sequence="1",
            release_date="2019-06-04",
            language="English",
        )
        assert name == "Bram Stoker - Dune 1 (2019, English, B0OURS) - 03 - Chapter One"

    def test_book_placeholder_fallbacks_match_the_book_path(self):
        # The "Unknown ..." fallbacks and the empty-on-missing optional tags are
        # the same map build_base_output_path renders from.
        name = _render_chapter(
            template="{author} - {narrator} - {publisher}{series} - {ch}",
            author=None,
            narrator=None,
            publisher=None,
            series="N/A",
        )
        assert name == "Unknown Author - Unknown Narrator - Unknown Publisher - 1"

    def test_subtitle_truncation_is_honoured_when_requested(self):
        assert _render_chapter(title="Dracula: The Un-Dead", truncate_subtitle=True) == "Dracula - 1 - Chapter One"
        assert (
            _render_chapter(title="Dracula: The Un-Dead", truncate_subtitle=False)
            == "Dracula_ The Un-Dead - 1 - Chapter One"
        )

    def test_ch_in_a_directory_segment_does_not_satisfy_the_guard(self, caplog):
        # WF1 regression. Only the last '/'-separated segment becomes the
        # filename, so a {ch} sitting in a folder level is thrown away. Checking
        # the guard against the whole template let "{ch}/{title}" through and
        # rendered the identical name for every part of the book.
        with caplog.at_level(logging.WARNING):
            names = [_render_chapter(template="{ch}/{title}", part_number=n, part_total=12) for n in range(1, 13)]

        assert names == [f"Dracula - {n:02d}" for n in range(1, 13)]
        assert len(set(names)) == 12
        assert "{ch}" in caplog.text
        # The warning quotes the template the user configured, not the stripped
        # segment they would not recognise.
        assert "{ch}/{title}" in caplog.text

    def test_guard_looks_past_every_directory_level(self):
        # Deeper nesting is the same defect: {ch} in any segment but the last is
        # discarded, so the guard has to append its own.
        name = _render_chapter(template="{author}/{ch}/{ch_title}", part_number=4, part_total=10)
        assert name == "Chapter One - 04"

    def test_directory_only_template_still_renders_a_numbered_name(self):
        # "{ch}/{series}" with no series: the filename segment is empty AND has
        # no {ch}, so the guard supplies the number and the part is still named.
        name = _render_chapter(template="{ch}/{series}", part_number=4, part_total=10, series="N/A")
        assert name == "04"

    def test_is_pure_no_settings_or_filesystem_access(self):
        # Guards the "later phases call this, it reads nothing" contract.
        with (
            mock.patch.object(processing_logic, "load_settings", side_effect=AssertionError("settings read")),
            mock.patch.object(processing_logic, "get_db_connection", side_effect=AssertionError("db access")),
            mock.patch("os.path.exists", side_effect=AssertionError("filesystem access")),
        ):
            assert _render_chapter() == "Dracula - 1 - Chapter One"


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
        db_error=None,
        shared_error=None,
    ):
        row = row if row is not None else self._row()
        con = mock.MagicMock()
        con.__enter__.return_value = con
        # Kept on the test instance so a test can assert what the database was
        # (and was not) told; pytest builds a fresh instance per test.
        self.con = con

        def execute(query, params=None):
            if db_error is not None and query.strip().startswith("UPDATE audiobooks"):
                raise db_error
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

        # `shared_error` makes the #20 shared-base check raise (a briefly locked
        # library.db); otherwise it behaves exactly as it does unpatched.
        real_base_is_shared = processing_logic._output_base_is_shared

        def base_is_shared(*args, **kwargs):
            if shared_error is not None:
                raise shared_error
            return real_base_is_shared(*args, **kwargs)

        with (
            mock.patch.object(
                processing_logic, "load_settings", return_value={"naming": {"apply_custom_to_filenames": apply}}
            ),
            mock.patch.object(processing_logic, "get_db_connection", return_value=con),
            mock.patch.object(processing_logic, "build_base_output_path", return_value=target),
            mock.patch.object(processing_logic, "_output_base_is_shared", side_effect=base_is_shared),
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

    def test_a_failed_database_write_puts_the_file_and_its_sidecars_back(self):
        # W2: the move happens first and the row is rewritten afterwards, so a
        # library.db that is briefly locked used to leave the files at their new
        # home with the row still naming the old one — a divergence nothing
        # downstream repairs (the book reads as MISSING on the next Verify while
        # its files sit intact one directory away). The files follow the database.
        cover = "/data/Bram Stoker/Dracula/Bram Stoker - Dracula.jpg"
        result, move = self._run(
            target="/data/New/New.m4b",
            also_present=(cover,),
            db_error=sqlite3.OperationalError("database is locked"),
        )
        assert result is None
        # Everything that moved out moved back, audio and sidecar alike...
        assert move.call_args_list[-2].args == ("/data/New/New.m4b", self.CURRENT)
        assert move.call_args_list[-1].args == ("/data/New/New.jpg", cover)
        # ...and nothing was committed, so the row still names the old path.
        self.con.commit.assert_not_called()

    def test_a_raising_shared_base_check_moves_nothing_at_all(self):
        # W2: the #20 shared-base question opens the database, and it used to be
        # asked AFTER the audiobook had already moved. A library.db locked for
        # that instant raised into the outer handler, which logs and returns
        # WITHOUT undoing the move — leaving the file at its new home and the row
        # still naming the old one, the very divergence the rollback below exists
        # to prevent. Asked before the first move, there is nothing to undo.
        result, move = self._run(
            target="/data/New/New.m4b",
            shared_error=sqlite3.OperationalError("database is locked"),
        )
        assert result is None
        move.assert_not_called()
        self.con.commit.assert_not_called()

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

    def _run_on_disk(self, tmp_path, *, sidecars=(), target_name="New.m4b"):
        """Drive the rename against REAL files under tmp_path (only the DB and the
        naming engine are faked), so the sidecar sweep has a real directory to
        match against. Returns (result, new_dir, old_dir, executed queries)."""
        old_dir = tmp_path / "old"
        old_dir.mkdir()
        new_dir = tmp_path / "new"
        current = old_dir / "Old.m4b"
        current.write_bytes(b"audio")
        for name in sidecars:
            (old_dir / name).write_bytes(b"sidecar")

        target = str(new_dir / target_name)
        row = self._row(filepath=str(current))
        con = mock.MagicMock()
        con.__enter__.return_value = con
        executed = []

        def execute(query, params=None):
            executed.append((query, params))
            cursor = mock.MagicMock()
            cursor.fetchone.return_value = None if "WHERE filepath" in query else row
            return cursor

        con.execute.side_effect = execute

        with (
            mock.patch.object(
                processing_logic, "load_settings", return_value={"naming": {"apply_custom_to_filenames": True}}
            ),
            mock.patch.object(processing_logic, "get_db_connection", return_value=con),
            mock.patch.object(processing_logic, "build_base_output_path", return_value=target),
            mock.patch.object(processing_logic, "_cleanup_empty_dirs"),
        ):
            result = processing_logic.rename_book_to_match_metadata("B0OURS")
        return result, new_dir, old_dir, executed

    def test_moves_sidecars_whose_extension_is_uppercase(self, tmp_path):
        # M12: files on disk carry whatever case they were created with — the cover
        # keeps Audible's own extension, and a hand-placed ".PDF" is common enough.
        # The lowercase-only match orphaned them at the old base.
        result, new_dir, old_dir, _executed = self._run_on_disk(
            tmp_path, sidecars=("Old.JPG", "Old.PDF", "Old.cue", "Old.Metadata.JSON")
        )
        assert result == str(new_dir / "New.m4b")
        # Each sidecar moved, keeping its own spelling.
        for name in ("New.JPG", "New.PDF", "New.cue", "New.Metadata.JSON"):
            assert (new_dir / name).exists()
        assert sorted(p.name for p in old_dir.iterdir()) == []

    def test_unrelated_neighbours_are_left_alone(self, tmp_path):
        # The guard on the case-insensitive match: only an exact sidecar suffix
        # counts, so a differently-named book in the same folder is never swept up.
        result, new_dir, old_dir, _executed = self._run_on_disk(
            tmp_path, sidecars=("Old 2.jpg", "Older.m4b", "Old.txt")
        )
        assert result == str(new_dir / "New.m4b")
        assert sorted(p.name for p in old_dir.iterdir()) == ["Old 2.jpg", "Old.txt", "Older.m4b"]
        assert sorted(p.name for p in new_dir.iterdir()) == ["New.m4b"]

    """Backlog #20: the sidecar sweep needs the same shared-base guard the
    stale-file cleanup has — two books can share one extension-stripped base
    under different audio extensions, and those sidecars are not ours to take."""

    def test_shared_base_keeps_the_sidecars_where_they_are(self, tmp_path):
        old_dir = tmp_path / "old"
        old_dir.mkdir()
        new_dir = tmp_path / "new"
        current = old_dir / "Old.m4b"
        current.write_bytes(b"audio")
        # Another book living at the same base under a different audio extension:
        # the cover and PDF here may be its only ones.
        (old_dir / "Old.mp3").write_bytes(b"other book")
        (old_dir / "Old.jpg").write_bytes(b"cover")
        (old_dir / "Old.pdf").write_bytes(b"pdf")

        target = str(new_dir / "New.m4b")
        row = self._row(filepath=str(current))
        con = _FakeDb(book_row=row, tracked=[("B0OTHER", str(old_dir / "Old.mp3"))])

        with (
            mock.patch.object(
                processing_logic, "load_settings", return_value={"naming": {"apply_custom_to_filenames": True}}
            ),
            mock.patch.object(processing_logic, "get_db_connection", return_value=con),
            mock.patch.object(processing_logic, "build_base_output_path", return_value=target),
            mock.patch.object(processing_logic, "_cleanup_empty_dirs"),
        ):
            result = processing_logic.rename_book_to_match_metadata("B0OURS")

        # The book itself still moves — only the jointly-owned sidecars stay.
        assert result == target
        assert (new_dir / "New.m4b").exists()
        assert (old_dir / "Old.jpg").exists()
        assert (old_dir / "Old.pdf").exists()
        assert not (new_dir / "New.jpg").exists()

    def test_unshared_base_still_moves_the_sidecars(self, tmp_path):
        # The control for the guard above: nothing else lives at the old base, so
        # the sweep behaves exactly as it always has.
        result, new_dir, _old_dir, _executed = self._run_on_disk(tmp_path, sidecars=("Old.jpg", "Old.pdf"))
        assert result == str(new_dir / "New.m4b")
        assert (new_dir / "New.jpg").exists()
        assert (new_dir / "New.pdf").exists()

    """Backlog #28: the ASIN-suffixed fallback name is re-validated instead of
    assumed free."""

    def test_taken_suffixed_target_walks_to_the_next_candidate(self):
        # Both the plain target AND "<base>_<asin>" are occupied by other books —
        # the old code wrote straight over the second one.
        result, move = self._run(
            target="/data/New/New.m4b",
            target_exists=True,
            also_present=("/data/New/New_B0OURS.m4b",),
            target_owner="B0OTHER",
        )
        assert result == "/data/New/New_B0OURS_2.m4b"
        move.assert_any_call(self.CURRENT, "/data/New/New_B0OURS_2.m4b")

    def test_suffixed_target_taken_by_an_in_flight_book_walks_too(self):
        processing_logic._reserved_output_paths.add("/data/New/New")
        processing_logic._reserved_output_paths.add("/data/New/New_B0OURS")
        result, _move = self._run(target="/data/New/New.m4b")
        assert result == "/data/New/New_B0OURS_2.m4b"

    def test_free_suffixed_target_is_used_as_before(self):
        # The control: nothing occupies the suffixed name, so the first candidate
        # is taken and the walk is invisible.
        result, _move = self._run(target="/data/New/New.m4b", target_exists=True, target_owner="B0OTHER")
        assert result == "/data/New/New_B0OURS.m4b"

    """v0.23.0 M11: the ASIN-suffix collision branch has to record the duplicate
    flag, the same way the download path's _finalize_success does — otherwise a
    book renamed onto a taken name is silently suffixed with nothing in the UI."""

    def _rename_update(self, **kwargs):
        """The parameters of the rename's filepath UPDATE."""
        row = kwargs.pop("row", None) or self._row()
        con = mock.MagicMock()
        con.__enter__.return_value = con
        executed = []

        target = kwargs.pop("target", "/data/New/New.m4b")
        target_owner = kwargs.pop("target_owner", None)
        also_present = kwargs.pop("also_present", ())

        def execute(query, params=None):
            executed.append((query, params))
            cursor = mock.MagicMock()
            if "WHERE filepath" in query:
                cursor.fetchone.return_value = {"asin": target_owner} if target_owner else None
            else:
                cursor.fetchone.return_value = row
            return cursor

        con.execute.side_effect = execute
        present = {row["filepath"], *also_present}

        with (
            mock.patch.object(
                processing_logic, "load_settings", return_value={"naming": {"apply_custom_to_filenames": True}}
            ),
            mock.patch.object(processing_logic, "get_db_connection", return_value=con),
            mock.patch.object(processing_logic, "build_base_output_path", return_value=target),
            mock.patch("os.path.exists", side_effect=lambda p: p in present),
            mock.patch("os.makedirs"),
            mock.patch.object(processing_logic.shutil, "move"),
            mock.patch.object(processing_logic, "_cleanup_empty_dirs"),
        ):
            processing_logic.rename_book_to_match_metadata("B0OURS")

        updates = [params for query, params in executed if query.startswith("UPDATE audiobooks SET filepath")]
        assert len(updates) == 1
        return updates[0]

    def test_collision_records_the_duplicate_flag(self):
        params = self._rename_update(also_present=("/data/New/New.m4b",), target_owner="B0OTHER")
        assert params == ("/data/New/New_B0OURS.m4b", 1, "B0OURS")

    def test_in_flight_collision_records_the_duplicate_flag(self):
        processing_logic._reserved_output_paths.add("/data/New/New")
        params = self._rename_update()
        assert params == ("/data/New/New_B0OURS.m4b", 1, "B0OURS")

    def test_clean_rename_clears_a_stale_duplicate_flag(self):
        # Written explicitly (not only on collision), mirroring _finalize_success:
        # a book that no longer needs the suffix must stop being flagged.
        params = self._rename_update()
        assert params == ("/data/New/New.m4b", 0, "B0OURS")

    def test_noop_rename_onto_its_own_suffixed_name_touches_nothing(self):
        # B1 regression: the book already sits at its ASIN-suffixed name (it
        # collided at download time), and the other book still holds the plain
        # base — so re-deriving the target and re-applying the suffix lands on the
        # path the book is already at. The old code moved the file onto itself,
        # logged a phantom "Moved file", and re-wrote is_duplicate = 1, undoing a
        # "Resolve duplicate -> Keep" that had just cleared the flag.
        current = "/data/New/New_B0OURS.m4b"
        row = self._row(filepath=current)
        con = mock.MagicMock()
        con.__enter__.return_value = con
        executed = []

        def execute(query, params=None):
            executed.append((query, params))
            cursor = mock.MagicMock()
            if "WHERE filepath" in query:
                cursor.fetchone.return_value = {"asin": "B0OTHER"}
            else:
                cursor.fetchone.return_value = row
            return cursor

        con.execute.side_effect = execute
        present = {current, "/data/New/New.m4b"}

        with (
            mock.patch.object(
                processing_logic, "load_settings", return_value={"naming": {"apply_custom_to_filenames": True}}
            ),
            mock.patch.object(processing_logic, "get_db_connection", return_value=con),
            mock.patch.object(processing_logic, "build_base_output_path", return_value="/data/New/New.m4b"),
            mock.patch("os.path.exists", side_effect=lambda p: p in present),
            mock.patch("os.makedirs") as makedirs,
            mock.patch.object(processing_logic.shutil, "move") as move,
            mock.patch.object(processing_logic, "_cleanup_empty_dirs") as cleanup,
        ):
            result = processing_logic.rename_book_to_match_metadata("B0OURS")

        assert result is None
        move.assert_not_called()
        makedirs.assert_not_called()
        cleanup.assert_not_called()
        # No DB write at all: the is_duplicate flag keeps whatever value the
        # duplicate resolver left in the row.
        assert [query for query, _params in executed if query.startswith("UPDATE audiobooks")] == []
        # The claim taken while resolving the collision is still released.
        assert processing_logic._reserved_output_paths == set()

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

    """Backlog #28: the ASIN-suffixed fallback is re-validated, not assumed free."""

    def _reserve_with(self, present, tracked):
        processor = BookProcessor(asin="B0BBBB", job_id=1)
        con = _FakeDb(tracked=tracked)
        with (
            mock.patch.object(processing_logic, "get_db_connection", return_value=con),
            mock.patch("os.path.exists", side_effect=lambda p: p in present),
            mock.patch.object(processor, "_probe_file_asin", return_value=None),
        ):
            return processor, processor._reserve_output_path("/data/A/Title/Title.m4b", "B0BBBB")

    def test_a_taken_suffixed_name_walks_to_the_next_candidate(self):
        # Both the plain base and "<base>_<asin>" belong to other books — the old
        # code handed the second one straight back and overwrote it.
        _processor, path = self._reserve_with(
            present={"/data/A/Title/Title.m4b", "/data/A/Title/Title_B0BBBB.m4b"},
            tracked=[("B0OTHER", "/data/A/Title/Title.m4b"), ("B0THIRD", "/data/A/Title/Title_B0BBBB.m4b")],
        )
        assert path == "/data/A/Title/Title_B0BBBB_2.m4b"

    def test_a_suffixed_name_taken_at_a_sibling_extension_walks_too(self):
        # The suffixed candidate is judged in the same extension-stripped currency
        # as the plain one: another book's .mp3 there shares our sidecar base.
        _processor, path = self._reserve_with(
            present={"/data/A/Title/Title.m4b", "/data/A/Title/Title_B0BBBB.mp3"},
            tracked=[("B0OTHER", "/data/A/Title/Title.m4b"), ("B0THIRD", "/data/A/Title/Title_B0BBBB.mp3")],
        )
        assert path == "/data/A/Title/Title_B0BBBB_2.m4b"

    def test_a_reserved_suffixed_name_walks_too(self):
        # This book's own in-flight force re-download already holds the suffixed
        # base... which is exactly why the walk must not stop there for a THIRD
        # book, while a book re-deriving its own name still gets it back.
        processing_logic._reserved_output_paths.add("/data/A/Title/Title")
        processing_logic._reserved_output_paths.add("/data/A/Title/Title_B0BBBB")
        _processor, path = self._reserve_with(present=set(), tracked=[])
        assert path == "/data/A/Title/Title_B0BBBB_2.m4b"

    def test_our_own_file_at_the_suffixed_name_is_kept(self):
        # The control: a previous collision of THIS book already put a file at the
        # suffixed name, and re-downloading it must land on the same name again
        # rather than churning to "_2" every time.
        _processor, path = self._reserve_with(
            present={"/data/A/Title/Title.m4b", "/data/A/Title/Title_B0BBBB.m4b"},
            tracked=[("B0OTHER", "/data/A/Title/Title.m4b"), ("B0BBBB", "/data/A/Title/Title_B0BBBB.m4b")],
        )
        assert path == "/data/A/Title/Title_B0BBBB.m4b"


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


class TestNoUsableChapters:
    """v0.23.0 ND3: an empty chapter list at spawn time has two causes — the title
    genuinely shipped none, or it had them and the zero-length cleanup in prepare
    dropped every one. The old message asserted the first, which is a misleading
    thing to show a user whose book DID have chapters."""

    def _run(self):
        processor = BookProcessor(asin="B0OURS", job_id=1)
        processor.download_complete_event = Event()

        con = mock.MagicMock()
        con.__enter__.return_value = con
        con.execute.return_value.fetchone.return_value = BOOK_ROW

        context = {"audio_file": "/tmp/x/master_intermediate.m4b", "chapters": []}
        with (
            mock.patch.object(
                processing_logic, "load_settings", return_value={"naming": {}, "conversion": {"output_format": "m4b"}}
            ),
            mock.patch.object(processing_logic, "get_db_connection", return_value=con),
            mock.patch.object(processing_logic, "prepare_book_assets", return_value=(context, None)),
            mock.patch.object(processing_logic, "build_base_output_path", return_value="/data/A/T/T.m4b"),
            mock.patch("os.path.exists", return_value=False),
            mock.patch("os.makedirs"),
            mock.patch.object(processing_logic, "_yield_progress"),
            mock.patch.object(processing_logic.task_runner, "submit_task") as submit,
            mock.patch.object(processor, "_update_db_on_failure") as fail,
        ):
            processor._prepare_and_spawn_encode_tasks()
        return processor, fail, submit

    def test_error_message_names_both_causes(self):
        processor, fail, submit = self._run()
        message = fail.call_args.args[0]
        assert "no usable chapters" in message
        # Not the old claim that the book simply had no chapter information...
        assert message != "Book has no chapter information."
        # ...and the drop is named as the other possibility, by what the user can
        # observe rather than by the step that did it: "chapter cleanup" is the UI's
        # label for the OPTIONAL transform toggles, and blaming those would send the
        # user off to disable settings that may not have been involved at all.
        assert "zero-length" in message
        assert "dropped" in message
        assert "cleanup" not in message
        # No encode work was queued, and the wait is released.
        submit.assert_not_called()
        assert processor._completion_event.is_set()


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


class TestCompletionTimeout:
    """v0.23.0 M1: `run` waits on one timeout for the whole book, and MP3 output
    must not be judged by the chunked-AAC estimator it never feeds. The watch case
    is an arm64 SBC converting a very long book, where the borrowed model expires
    while the encode is still healthy."""

    # 30 hours of audio: long enough that the two models diverge sharply.
    LONG_RUNTIME_MIN = 1800
    # What the estimator says about a 30h book with no history (10 sec/min).
    LONG_ESTIMATE_SEC = 18_000

    def _timeout(self, *, output_format="m4b", runtime_min=LONG_RUNTIME_MIN, has_row=True, estimate=LONG_ESTIMATE_SEC):
        processor = BookProcessor(asin="B0OURS", job_id=1)
        con = mock.MagicMock()
        con.__enter__.return_value = con
        con.execute.return_value.fetchone.return_value = {"runtime_min": runtime_min} if has_row else None
        with (
            mock.patch.object(processing_logic, "get_db_connection", return_value=con),
            mock.patch.object(processing_logic, "load_settings", return_value={}),
            mock.patch.object(processing_logic, "resolve_output_format", return_value=output_format),
            mock.patch.object(processing_logic, "estimate_conversion_time", return_value=estimate) as estimator,
        ):
            return processor._completion_timeout(), estimator

    def test_mp3_scales_off_the_source_duration(self):
        timeout, estimator = self._timeout(output_format="mp3")
        # 3x runtime (90h) beats 4x the estimate (20h) here, so the runtime model
        # wins the max()...
        assert timeout == self.LONG_RUNTIME_MIN * 60 * 3
        # ...but the estimator IS consulted, because the MP3 budget is the larger
        # of the two models (see test_mp3_never_undercuts_the_old_estimate_model).
        estimator.assert_called_once_with(self.LONG_RUNTIME_MIN)

    def test_mp3_never_undercuts_the_old_estimate_model(self):
        # Minor 5: the runtime model is the TIGHTER one once the machine's recorded
        # AAC rate passes 45 s/min — the slow-hardware case this timeout exists
        # for. Taking the max keeps the change monotonic: 50 s/min here, so the old
        # 4x-estimate budget is the one that must stand.
        slow_estimate = self.LONG_RUNTIME_MIN * 50
        assert 4 * slow_estimate > self.LONG_RUNTIME_MIN * 60 * 3  # what makes this the binding model
        timeout, estimator = self._timeout(output_format="mp3", estimate=slow_estimate)
        assert timeout == 4 * slow_estimate
        estimator.assert_called_once_with(self.LONG_RUNTIME_MIN)

    def test_mp3_budget_exceeds_the_books_own_runtime(self):
        # The regression itself: 4x the AAC estimate is 20h for a 30h book, so a
        # single-pass LAME encode slower than ~0.7x real time was killed mid-run.
        runtime_sec = self.LONG_RUNTIME_MIN * 60
        old_model_timeout = 4 * self.LONG_ESTIMATE_SEC
        assert old_model_timeout < runtime_sec  # what made this a bug
        timeout, _estimator = self._timeout(output_format="mp3")
        assert timeout > runtime_sec

    def test_m4b_still_uses_the_eta_model(self):
        timeout, estimator = self._timeout(output_format="m4b")
        assert timeout == 4 * self.LONG_ESTIMATE_SEC
        estimator.assert_called_once_with(self.LONG_RUNTIME_MIN)

    def test_original_still_uses_the_eta_model(self):
        timeout, estimator = self._timeout(output_format="original")
        assert timeout == 4 * self.LONG_ESTIMATE_SEC
        estimator.assert_called_once_with(self.LONG_RUNTIME_MIN)

    @pytest.mark.parametrize("output_format", ["mp3", "m4b"])
    def test_short_books_get_the_two_hour_floor(self, output_format):
        timeout, _estimator = self._timeout(output_format=output_format, runtime_min=10, estimate=100)
        assert timeout == 7200

    @pytest.mark.parametrize("runtime_min", [None, 0])
    def test_unknown_runtime_falls_back_to_the_floor(self, runtime_min):
        timeout, estimator = self._timeout(output_format="mp3", runtime_min=runtime_min)
        assert timeout == 7200
        estimator.assert_not_called()

    def test_missing_book_row_falls_back_to_the_floor(self):
        timeout, _estimator = self._timeout(has_row=False)
        assert timeout == 7200


class TestTimeoutFailureHandling:
    """v0.23.0 M1 (second half): what an expired completion wait does — exactly one
    failure write, and NO subprocess kill.

    The cases below drive the sequential ordering, where the timeout handler is
    genuinely first and so its message is the one that survives. A step failing in
    the instant before the flag goes up wins the claim instead and keeps its own
    (equally true) message; only the COUNT is guaranteed.

    The kill was built and then pulled before shipping: the process registry is
    keyed by job rather than by book, so terminating the job would also SIGTERM a
    concurrent book's healthy download (max_parallel_downloads defaults to 2),
    failing it with no stated reason. It is deferred to backlog #19 behind per-book
    process tracking, and pinned as absent here so it cannot half-return."""

    def test_timeout_marks_error_without_killing_the_jobs_processes(self, tmp_path):
        processor = BookProcessor(asin="B0OURS", job_id=7)
        with (
            mock.patch.object(processing_logic, "TEMP_DIR", str(tmp_path)),
            mock.patch.object(processor, "_completion_timeout", return_value=0),
            mock.patch.object(processing_logic.task_runner, "submit_task"),
            mock.patch.object(processing_logic.process_registry, "kill_job_processes") as kill,
            mock.patch.object(processor, "_update_db_on_failure") as fail,
        ):
            processor.run()
        kill.assert_not_called()
        assert "timed out" in fail.call_args.args[0]

    def test_late_step_failures_are_not_reported_again(self, tmp_path):
        # W1: the abandoned tasks keep running after the timeout, so they can still
        # report a step failure later — with the stop_event unset, because nobody
        # cancelled anything. Every such report used to write its own ERROR row, and
        # since that write is the only place retry_count is bumped, one timeout
        # consumed the entire automatic-retry budget (once per in-flight chunk on the
        # AAC path) while whichever writer landed last decided the message the user
        # saw. The timeout handler must be the single writer.
        processor = BookProcessor(asin="B0OURS", job_id=7)
        reported = []
        with (
            mock.patch.object(processing_logic, "TEMP_DIR", str(tmp_path)),
            mock.patch.object(processor, "_completion_timeout", return_value=0),
            mock.patch.object(processing_logic.task_runner, "submit_task"),
            mock.patch.object(processor, "_update_db_on_failure", side_effect=reported.append),
        ):
            processor.run()
            # What the still-running tasks do when they eventually give up, from the
            # worker threads.
            processor._fail_or_cancel("A chapter chunk failed to encode.")
            processor._fail_or_cancel("MP3 encode failed.")

        # Exactly one failure write, and with the timeout landing first here, it
        # is the timeout message that survives.
        assert len(reported) == 1
        assert "Processing timed out." in reported[0]

    def test_step_failures_are_still_reported_without_a_timeout(self, tmp_path):
        # The other side of the guard: an ordinary step failure (no timeout, no
        # cancel) still records its own message and retry bump.
        processor = BookProcessor(asin="B0OURS", job_id=7)
        with mock.patch.object(processor, "_update_db_on_failure") as fail:
            processor._fail_or_cancel("MP3 encode failed.")
        fail.assert_called_once_with("MP3 encode failed.")

    def test_normal_completion_reports_nothing(self, tmp_path):
        processor = BookProcessor(asin="B0OURS", job_id=7)
        processor._completion_event.set()
        with (
            mock.patch.object(processing_logic, "TEMP_DIR", str(tmp_path)),
            mock.patch.object(processor, "_completion_timeout", return_value=0),
            mock.patch.object(processing_logic.task_runner, "submit_task"),
            mock.patch.object(processing_logic.process_registry, "kill_job_processes") as kill,
            mock.patch.object(processor, "_update_db_on_failure") as fail,
        ):
            processor.run()
        kill.assert_not_called()
        fail.assert_not_called()


class TestOnceOnlyFailureReport:
    """v0.23.0 B1: one failed run writes exactly ONE ERROR row, whatever fails.

    The timeout guard above only covers timeouts. An ordinary chunk failure has
    the same late-echo shape without one: the first failing chunk reports and
    sets the completion event, `run` wakes and deletes the temp dir, and every
    chunk still in flight or queued then fails too — with no cancel and no
    timeout, so neither of the earlier guards applies. Each echo used to bump
    retry_count again (past the `retry_count <= 1` auto-retry gate, so the one
    promised automatic retry never happened for this whole failure class),
    overwrite error_message, and emit another "Failed!" progress event."""

    def _report(self, *messages):
        """Runs the REAL _update_db_on_failure (the latch lives inside it) with
        the DB and the SSE emitter mocked, and returns both so the caller can
        count writes and progress events."""
        processor = BookProcessor(asin="B0OURS", job_id=7)
        con = mock.MagicMock()
        con.__enter__.return_value = con
        with (
            mock.patch.object(processing_logic, "get_db_connection", return_value=con),
            mock.patch.object(processing_logic, "_yield_progress") as progress,
        ):
            for message in messages:
                processor._fail_or_cancel(message)
        return con, progress

    def test_first_failure_is_recorded(self):
        con, progress = self._report("A chapter chunk failed to encode.")
        assert con.execute.call_count == 1
        sql, params = con.execute.call_args.args
        assert "retry_count = COALESCE(retry_count, 0) + 1" in sql
        assert params == ("A chapter chunk failed to encode.", "B0OURS")
        progress.assert_called_once()
        assert progress.call_args.args[1] == "Failed!"

    def test_later_chunk_failures_are_not_recorded_again(self):
        # The echo: the remaining chunks die against the deleted temp dir and
        # report one after another. Only the first write may survive, and the
        # message must stay the one that named the original cause.
        con, progress = self._report(
            "A chapter chunk failed to encode.",
            "A chapter chunk failed to encode.",
            "Final merge of chapter chunks failed.",
        )
        assert con.execute.call_count == 1
        assert con.execute.call_args.args[1] == ("A chapter chunk failed to encode.", "B0OURS")
        progress.assert_called_once()

    def test_direct_failure_write_after_a_step_failure_is_suppressed(self):
        # `run`'s except block calls _update_db_on_failure directly, so the latch
        # has to live in that method rather than in _fail_or_cancel: a temp-dir
        # teardown error arriving after a chunk already reported must not add a
        # second bump or replace the real cause.
        processor = BookProcessor(asin="B0OURS", job_id=7)
        con = mock.MagicMock()
        con.__enter__.return_value = con
        with (
            mock.patch.object(processing_logic, "get_db_connection", return_value=con),
            mock.patch.object(processing_logic, "_yield_progress") as progress,
        ):
            processor._fail_or_cancel("A chapter chunk failed to encode.")
            processor._update_db_on_failure("A critical error occurred: teardown blew up")
        assert con.execute.call_count == 1
        assert con.execute.call_args.args[1] == ("A chapter chunk failed to encode.", "B0OURS")
        progress.assert_called_once()

    def test_the_latch_is_per_run_not_global(self):
        # A fresh BookProcessor is built per attempt, so the next attempt at the
        # same book still gets to record its own failure (and its own bump).
        con_a, _ = self._report("A chapter chunk failed to encode.")
        con_b, _ = self._report("A chapter chunk failed to encode.")
        assert con_a.execute.call_count == 1
        assert con_b.execute.call_count == 1

    def test_a_raised_write_releases_the_latch(self):
        # The claim is only provisional until the write lands. If the UPDATE
        # raises ("database is locked" against a busy SQLite file), a permanently
        # claimed latch would mean NO ERROR row for the whole run: status stays
        # NEW, retry_count stays 0, and every later reporter — including `run`'s
        # own except handler after the completion timeout — is silently dropped,
        # so every future scheduled run re-attempts the book forever.
        processor = BookProcessor(asin="B0OURS", job_id=7)
        con = mock.MagicMock()
        con.__enter__.return_value = con
        con.execute.side_effect = [sqlite3.OperationalError("database is locked"), None, None]
        with (
            mock.patch.object(processing_logic, "get_db_connection", return_value=con),
            mock.patch.object(processing_logic, "_yield_progress") as progress,
        ):
            with pytest.raises(sqlite3.OperationalError):
                processor._update_db_on_failure("A chapter chunk failed to encode.")
            # The next reporter gets its turn and records the failure.
            processor._update_db_on_failure("A critical error occurred: timed out")
            # ...and having succeeded, it re-claims the latch for good.
            processor._update_db_on_failure("MP3 encode failed.")

        assert con.execute.call_count == 2
        assert con.execute.call_args.args[1] == ("A critical error occurred: timed out", "B0OURS")
        progress.assert_called_once()

    def test_concurrent_reporters_still_produce_exactly_one_write(self):
        # The property the lock exists for, and the one the release-on-error path
        # must not weaken: several chunk workers failing at the same instant claim
        # the report between them exactly once. The barrier lines the threads up
        # on the claim and the write itself is slowed, so the losers are still
        # inside _update_db_on_failure while the winner writes.
        processor = BookProcessor(asin="B0OURS", job_id=7)
        writes = []
        writes_lock = Lock()
        lined_up = Barrier(4)

        def slow_connection():
            con = mock.MagicMock()
            con.__enter__.return_value = con

            def execute(_sql, params):
                time.sleep(0.05)
                with writes_lock:
                    writes.append(params)

            con.execute.side_effect = execute
            return con

        def report():
            lined_up.wait()
            processor._fail_or_cancel("A chapter chunk failed to encode.")

        with (
            mock.patch.object(processing_logic, "get_db_connection", side_effect=slow_connection),
            mock.patch.object(processing_logic, "_yield_progress"),
        ):
            threads = [Thread(target=report) for _ in range(4)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join()

        assert writes == [("A chapter chunk failed to encode.", "B0OURS")]


class TestPostTimeoutMp3Finalize:
    """v0.23.0 W2: an MP3 encode that finishes AFTER `run` timed out must not
    finalize as a success.

    The chunked and remux paths are covered incidentally — their inputs live in
    the temp dir `run` has already deleted, so they simply fail — but the MP3
    encoder promotes its ".part" onto the FINAL path in /data, which outlives the
    temp dir. Without the guard the late success flips the book back to
    DOWNLOADED, resets retry_count, and runs the stale-file cleanup, deleting the
    user's previous copy on behalf of a run that already recorded ERROR."""

    def _encode(self, tmp_path, timed_out):
        processor = BookProcessor(asin="B0OURS", job_id=7)
        processor.final_output_path = str(tmp_path / "Author - Title.mp3")
        with open(processor.final_output_path, "w", encoding="utf-8") as handle:
            handle.write("finished encode")
        if timed_out:
            processor._timed_out.set()
        with (
            mock.patch.object(processing_logic, "encode_book_mp3", return_value=True),
            mock.patch.object(processor, "_finalize_success") as finalize,
            mock.patch.object(processor, "_cleanup_stale_files") as cleanup,
        ):
            processor._encode_mp3_and_finalize()
        return processor, finalize, cleanup

    def test_a_post_timeout_encode_does_not_finalize(self, tmp_path):
        processor, finalize, cleanup = self._encode(tmp_path, timed_out=True)
        finalize.assert_not_called()
        cleanup.assert_not_called()
        # The orphaned output is discarded, so a later deep sync can't adopt it
        # as a DOWNLOADED book the DB says failed.
        assert not os.path.exists(processor.final_output_path)

    def test_a_normal_encode_still_finalizes(self, tmp_path):
        processor, finalize, _cleanup = self._encode(tmp_path, timed_out=False)
        finalize.assert_called_once()
        assert finalize.call_args.kwargs == {"record_eta": False}
        assert os.path.exists(processor.final_output_path)


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

    def test_embedded_quotes_become_two_single_quotes(self):
        # CUE has no backslash escape, so a double quote is REPLACED, not escaped.
        chapters = [{"title": 'The "Big" Chapter', "start_offset_ms": 0}]
        cue = generate_cue_sheet(chapters, "x.m4b", 'A "Quoted" Title', 'Au "thor"', "B0")
        assert "TITLE \"The ''Big'' Chapter\"" in cue
        assert "TITLE \"A ''Quoted'' Title\"" in cue
        assert "PERFORMER \"Au ''thor''\"" in cue
        assert "\\" not in cue

    def test_embedded_newlines_stay_on_one_line(self):
        # A CR/LF inside a field would split the record in this line-oriented
        # format; each becomes a single space (CRLF collapsing to one, not two).
        chapters = [{"title": "Split\r\nChapter", "start_offset_ms": 0}]
        cue = generate_cue_sheet(chapters, "x.m4b", "Ti\ntle", "Au\rthor", "B0")
        assert 'PERFORMER "Au thor"' in cue
        assert 'TITLE "Ti tle"' in cue
        assert '    TITLE "Split Chapter"' in cue
        assert "\r" not in cue
        # Header lines + one TRACK/TITLE/INDEX trio, and nothing extra from a
        # field having broken across lines.
        assert len(cue.rstrip("\n").split("\n")) == 7

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
            "save_annotations": False,
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

    def test_annotations_are_placed_when_present(self, tmp_path):
        # Phase 6: the raw annotations dump prepare fetched is copied to
        # "<base>.annotations.json" — content passed through verbatim.
        dump = tmp_path / "Dracula-annotations.json"
        dump.write_text('{"payload": {"records": []}}', encoding="utf-8")
        processor = self._processor(tmp_path, {"annotations_file": str(dump)})
        with mock.patch.object(processing_logic, "load_settings", return_value=self._settings(save_annotations=True)):
            processor._place_sidecar_files()
        placed = tmp_path / "out" / "Dracula.annotations.json"
        assert placed.exists()
        assert placed.read_text(encoding="utf-8") == '{"payload": {"records": []}}'

    def test_annotations_skipped_when_context_has_none(self, tmp_path):
        # The common case: the setting is on but the title has no annotations, so
        # prepare left the key None. Nothing is written and nothing raises.
        processor = self._processor(tmp_path, {"annotations_file": None})
        with mock.patch.object(processing_logic, "load_settings", return_value=self._settings(save_annotations=True)):
            processor._place_sidecar_files()
        assert list((tmp_path / "out").iterdir()) == []

    def test_annotations_skipped_when_setting_off(self, tmp_path):
        dump = tmp_path / "Dracula-annotations.json"
        dump.write_text("{}", encoding="utf-8")
        processor = self._processor(tmp_path, {"annotations_file": str(dump)})
        with mock.patch.object(processing_logic, "load_settings", return_value=self._settings()):
            processor._place_sidecar_files()
        assert list((tmp_path / "out").iterdir()) == []

    def _title_settings(self, chapters=None):
        """Both title-bearing sidecars on, with an optional chapters block."""
        settings = self._settings(save_metadata_json=True, create_cue_sheet=True)
        if chapters is not None:
            settings["conversion"]["chapters"] = chapters
        return settings

    def _write_titled_sidecars(self, tmp_path, settings, custom_title=None, title="Dracula (Unabridged)"):
        """Run the two title-bearing sidecars and return (json title, cue text)."""
        context = {
            "book_info": {"title": title, "authors": [{"name": "Bram Stoker"}]},
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

    def test_all_marker_title_falls_back_to_the_raw_title(self, tmp_path):
        # Issue #14: a title that is nothing but the marker strips to "", and the
        # two writers then disagreed — the JSON recorded the empty string while
        # the cue sheet substituted "Unknown Title". The strip now falls back to
        # the raw title, so both sidecars say the same thing.
        json_title, cue = self._write_titled_sidecars(
            tmp_path,
            self._title_settings({"strip_unabridged": True}),
            title="(Unabridged)",
        )
        assert json_title == "(Unabridged)"
        assert 'TITLE "(Unabridged)"' in cue

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


class TestSidecarSuffixRegistry:
    """Every sidecar suffix must be in _SIDECAR_SUFFIXES, because that one list is
    what the rename, the timestamp sweep and the stale-file cleanup all walk — a
    sidecar missing from it gets orphaned when its audiobook moves."""

    def test_annotations_suffix_is_registered(self):
        assert ".annotations.json" in processing_logic._SIDECAR_SUFFIXES

    def test_annotations_sidecar_is_found_next_to_a_book(self, tmp_path):
        # End-to-end through the discovery helper the movers use, which is what
        # actually decides whether the file follows its audiobook.
        base = tmp_path / "Dracula"
        (tmp_path / "Dracula.m4b").write_bytes(b"audio")
        (tmp_path / "Dracula.annotations.json").write_text("{}", encoding="utf-8")
        assert processing_logic._existing_sidecar_suffixes(str(base)) == [".annotations.json"]

    def test_metadata_and_annotations_are_distinguished(self, tmp_path):
        # Both end in ".json" and share the same base; both must be reported.
        base = tmp_path / "Dracula"
        (tmp_path / "Dracula.metadata.json").write_text("{}", encoding="utf-8")
        (tmp_path / "Dracula.annotations.json").write_text("{}", encoding="utf-8")
        assert processing_logic._existing_sidecar_suffixes(str(base)) == [".annotations.json", ".metadata.json"]


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

    def test_uppercase_sidecar_extensions_are_stamped_too(self, tmp_path):
        # M12: the cover keeps whatever extension Audible handed us, so a ".JPG"
        # sidecar exists in real libraries — and the lowercase-only match left it
        # carrying the download time while the audiobook carried the release date.
        processor = self._processor(tmp_path, {"release_date": "2019-06-27"})
        out = tmp_path / "out"
        (out / "Dracula.JPG").write_bytes(b"cover")
        (out / "Dracula.pdf").write_bytes(b"pdf")
        self._run(processor, "release_date")
        expected = datetime(2019, 6, 27).timestamp()
        assert (out / "Dracula.JPG").stat().st_mtime == expected
        assert (out / "Dracula.pdf").stat().st_mtime == expected

    def test_unrelated_neighbours_are_not_stamped(self, tmp_path):
        # The guard: only exact sidecar suffixes on this book's base are touched.
        processor = self._processor(tmp_path, {"release_date": "2019-06-27"})
        out = tmp_path / "out"
        neighbour = out / "Dracula 2.jpg"
        neighbour.write_bytes(b"other")
        before = neighbour.stat().st_mtime
        self._run(processor, "release_date")
        assert neighbour.stat().st_mtime == before

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
        other_parts=(),
        split_parts=None,
        split_dir=None,
        split_sidecar_base=None,
        previous_parts=None,
    ):
        """Drive _cleanup_stale_files with the filesystem faked out, and return
        the set of paths it tried to unlink plus the empty-dir cleanup mock. The
        paths are deliberately fake /data ones: the guard is hard-coded to the
        real output root, so nothing here may touch a real file.

        `other_books` are (asin, filepath) pairs for OTHER tracked books, which is
        what the shared-base check reads to decide whether the old base's sidecars
        are jointly owned; `other_parts` are the same for their `book_files` rows.
        `previous_parts` is the part list the PREVIOUS download left behind — the
        set-wise form of `previous_path`."""
        processor = BookProcessor(asin="B0OURS", job_id=1)
        processor.final_output_path = new_path or self.NEW
        processor.cleanup_stale_files = param
        if split_parts:
            processor.split_part_paths = list(split_parts)
            processor.split_output_dir = split_dir or os.path.dirname(split_parts[0])
            processor.split_sidecar_base = split_sidecar_base

        if present is None:
            present = {previous_path} if previous_path else set()
        settings = {"job": {"download": {"cleanup_stale_files": setting}}}

        con = mock.MagicMock()
        con.__enter__.return_value = con
        tracked = [{"asin": "B0OURS", "filepath": previous_path}] if previous_path else []
        tracked += [{"asin": asin, "filepath": path} for asin, path in other_books]
        part_rows = [{"asin": asin, "filepath": path} for asin, path in other_parts]
        part_rows += [{"asin": "B0OURS", "filepath": path} for path in (previous_parts or [])]

        def execute(query, params=None):
            cursor = mock.MagicMock()
            cursor.fetchall.return_value = part_rows if "FROM book_files" in query else tracked
            return cursor

        con.execute.side_effect = execute

        def listdir(directory):
            """The `present` set, seen as a directory listing — the sidecar sweep
            scans the folder so it can match extensions case-insensitively."""
            names = [os.path.basename(p) for p in present if os.path.dirname(p) == directory]
            if not names:
                raise OSError("no such directory")
            return names

        with (
            mock.patch.object(processing_logic, "load_settings", return_value=settings),
            mock.patch.object(processing_logic, "get_db_connection", return_value=con),
            mock.patch("os.path.exists", side_effect=lambda p: p in present),
            mock.patch("os.listdir", side_effect=listdir),
            mock.patch("os.remove", side_effect=remove_error) as remove,
            mock.patch.object(processing_logic, "_cleanup_empty_dirs") as cleanup_dirs,
        ):
            processor._cleanup_stale_files(previous_path, previous_parts)

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

    def test_uppercase_stale_sidecars_are_swept(self):
        # M12: a leftover ".JPG" at the abandoned base is exactly as stale as a
        # ".jpg" one, and the lowercase-only match left it behind forever.
        old_base = "/data/Author/Old Title/Old Title"
        removed, _cleanup_dirs = self._run(
            self.OLD,
            param=True,
            present={self.OLD, old_base + ".JPG", old_base + ".Metadata.JSON", old_base + ".cue"},
        )
        assert removed == {self.OLD, old_base + ".JPG", old_base + ".Metadata.JSON", old_base + ".cue"}

    def test_unrelated_neighbours_at_the_old_base_are_kept(self):
        # The guard on the directory scan: only exact sidecar suffixes on the old
        # base go, so another book living in the same folder is untouched.
        old_base = "/data/Author/Old Title/Old Title"
        removed, _cleanup_dirs = self._run(
            self.OLD,
            param=True,
            present={
                self.OLD,
                old_base + ".cue",
                "/data/Author/Old Title/Old Title 2.jpg",
                "/data/Author/Old Title/Other.m4b",
            },
        )
        assert removed == {self.OLD, old_base + ".cue"}

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

    def test_single_to_split_removes_the_old_whole_book_file(self):
        # W1: the transition every user of this feature makes first. The reserved
        # single-file path is EXACTLY where the previous download sits, so the
        # "we overwrote our own file" early return used to fire and leave the old
        # whole-book .m4b on disk beside the new parts, tracked by nothing.
        old = "/data/Author/Title/Title.m4b"
        base = "/data/Author/Title/Title"
        parts = [f"{base} - 01 - One.m4b", f"{base} - 02 - Two.m4b"]
        removed, _cleanup_dirs = self._run(
            old,
            new_path=old,  # A split book only ever RESERVES this path.
            param=True,
            split_parts=parts,
            split_dir="/data/Author/Title",
            present={old, base + ".jpg", base + ".metadata.json", *parts},
        )
        # The old whole-book file goes...
        assert removed == {old}
        # ...and nothing this run just wrote does: not the parts, and — the
        # critical subtlety — not the sidecars, which sit at the old file's very
        # own base because a split book keeps the single-file-equivalent name.
        assert not removed & set(parts)

    def test_split_flat_guard_keeps_the_sidecars_it_just_wrote(self):
        # The case where the old base and the new SIDECAR base coincide while the
        # reserved path's base does not: an old "{author} - {title}/{author} -
        # {title}" layout re-downloaded with a flat template and splitting on, so
        # D5's guard puts the parts (and the sidecars) in the folder the previous
        # single file was already living in.
        folder = "/data/Bram Stoker - Dracula"
        old = f"{folder}/Bram Stoker - Dracula.m4b"
        base = f"{folder}/Bram Stoker - Dracula"
        parts = [f"{folder}/Dracula - 01 - One.m4b"]
        removed, _cleanup_dirs = self._run(
            old,
            new_path="/data/Bram Stoker - Dracula.m4b",
            param=True,
            split_parts=parts,
            split_dir=folder,
            present={old, base + ".jpg", base + ".pdf", base + ".metadata.json", *parts},
        )
        assert removed == {old}

    def test_a_flat_template_single_to_split_still_sweeps_the_old_sidecars(self):
        # W1, the mirror image of the test above: with a flat "{title}" template
        # D5's guard puts the parts (and this run's sidecars) in a NEW subfolder,
        # so the old sidecars in the output root really are stale — but the
        # reserved single-file path a split book never writes sits at exactly
        # their base, and consulting it skipped the sweep. The four old sidecars
        # were then stranded in /data forever: nothing references them, and the
        # guard means no future run ever lands on that base again.
        old = "/data/Dracula.m4b"
        old_base = "/data/Dracula"
        parts = ["/data/Dracula/Dracula - 01 - One.m4b", "/data/Dracula/Dracula - 02 - Two.m4b"]
        sidecars = {f"{old_base}.jpg", f"{old_base}.pdf", f"{old_base}.cue", f"{old_base}.metadata.json"}
        removed, _cleanup_dirs = self._run(
            old,
            new_path=old,  # a split book only ever RESERVES this path
            param=True,
            split_parts=parts,
            split_dir="/data/Dracula",
            split_sidecar_base="/data/Dracula/Dracula",
            present={old, *sidecars, *parts},
        )
        assert removed == {old, *sidecars}
        # ...and nothing this run just wrote went with them.
        assert not removed & set(parts)

    def test_single_to_split_without_consent_leaves_everything(self):
        old = "/data/Author/Title/Title.m4b"
        base = "/data/Author/Title/Title"
        parts = [f"{base} - 01 - One.m4b"]
        removed, cleanup_dirs = self._run(
            old,
            new_path=old,
            param=None,
            setting=False,
            split_parts=parts,
            split_dir="/data/Author/Title",
            present={old, base + ".jpg", *parts},
        )
        assert removed == set()
        cleanup_dirs.assert_not_called()

    def test_a_split_part_is_still_recognized_as_our_own_file(self):
        # The other half of the produced-set comparison: when the tracked previous
        # path IS one of this run's parts (a split book re-downloaded split, same
        # names), it was overwritten in place and there is nothing stale.
        base = "/data/Author/Title/Title"
        parts = [f"{base} - 01 - One.m4b", f"{base} - 02 - Two.m4b"]
        removed, cleanup_dirs = self._run(
            parts[0],
            new_path="/data/Author/Title/Title.m4b",
            param=True,
            split_parts=parts,
            split_dir="/data/Author/Title",
            present=set(parts),
        )
        assert removed == set()
        cleanup_dirs.assert_not_called()

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

        cleanup.assert_called_once_with(self.OLD, [])


# --- v0.24.0 Phase 2: per-chapter splitting ---------------------------------

SPLIT_CHAPTERS = [
    {"title": "One", "start_offset_ms": 0, "length_ms": 600_000},
    {"title": "Two", "start_offset_ms": 600_000, "length_ms": 600_000},
    {"title": "Three", "start_offset_ms": 1_200_000, "length_ms": 600_000},
]


def _run_split_prepare(
    chapters=None,
    naming=None,
    split_output=True,
    book_row=BOOK_ROW,
    output_format="m4b",
    audio_file="/tmp/x/master_intermediate.m4b",
    split_encode_mode=None,
):
    """
    Drive BookProcessor._prepare_and_spawn_encode_tasks with a context that
    prepare already marked (or didn't mark) as split, so the planning of the
    per-part output paths can be asserted on. Everything external is mocked;
    nothing touches disk.

    `output_format` and `audio_file` exist for the Phase 3 routing matrix: which
    task a book gets depends on its format AND on whether the master is the
    AAC ".m4b" one (a FLAC fallback can't be remuxed or copy-cut).
    """
    chapters = SPLIT_CHAPTERS if chapters is None else chapters
    processor = BookProcessor(asin="B0OURS", job_id=1)
    processor.download_complete_event = Event()

    con = mock.MagicMock()
    con.__enter__.return_value = con
    con.execute.return_value.fetchone.return_value = book_row

    context = {
        "audio_file": audio_file,
        "chapters": chapters,
        "split_output": split_output,
        "split_encode_mode": split_encode_mode,
    }
    submitted = []

    with (
        mock.patch.object(
            processing_logic,
            "load_settings",
            return_value={"naming": naming or {}, "conversion": {"output_format": output_format}},
        ),
        mock.patch.object(processing_logic, "get_db_connection", return_value=con),
        mock.patch.object(processing_logic, "prepare_book_assets", return_value=(context, None)),
        mock.patch("os.path.exists", return_value=False),
        mock.patch("os.makedirs") as makedirs,
        mock.patch.object(processing_logic, "_yield_progress"),
        mock.patch.object(processing_logic.task_runner, "submit_task", side_effect=submitted.append),
    ):
        processor._prepare_and_spawn_encode_tasks()

    return processor, submitted, makedirs


class TestSplitOutputPlanning:
    """D4/D5: the split decision prepare made becomes concrete per-part output
    paths, named from `naming.chapter_file_template` and placed in the folder the
    book naming template already produced."""

    def test_unsplit_context_plans_nothing(self):
        processor, submitted, _ = _run_split_prepare(split_output=False)
        assert processor.split_part_paths == []
        assert processor.split_output_dir is None
        # ...and the chunk fan-out is exactly today's.
        assert processor.total_chunks == 3
        assert all(t.func == processor._encode_and_track_chunk for t in submitted)

    def test_parts_land_beside_the_single_file_path(self):
        processor, _submitted, _ = _run_split_prepare()
        assert processor.split_output_dir == "/data/Bram Stoker/Dracula"
        assert processor.split_part_paths == [
            "/data/Bram Stoker/Dracula/Dracula - 1 - One.m4b",
            "/data/Bram Stoker/Dracula/Dracula - 2 - Two.m4b",
            "/data/Bram Stoker/Dracula/Dracula - 3 - Three.m4b",
        ]

    def test_part_numbers_are_one_based_and_zero_padded_to_the_count(self):
        chapters = [{"title": f"Ch {i}", "start_offset_ms": 0, "length_ms": 60_000} for i in range(1, 13)]
        processor, _submitted, _ = _run_split_prepare(chapters=chapters)
        names = [os.path.basename(p) for p in processor.split_part_paths]
        assert names[0] == "Dracula - 01 - Ch 1.m4b"
        assert names[-1] == "Dracula - 12 - Ch 12.m4b"

    def test_custom_chapter_template_is_honoured(self):
        naming = {"chapter_file_template": "{ch} - {ch_title} ({ch_total})"}
        processor, _submitted, _ = _run_split_prepare(naming=naming)
        assert os.path.basename(processor.split_part_paths[1]) == "2 - Two (3).m4b"

    def test_flat_template_puts_the_parts_in_a_subfolder(self):
        # D5: a template that renders straight into the output root would dump N
        # chapter files into /data; they get a folder named from the book instead.
        naming = {"template": "{author} - {title}"}
        processor, _submitted, makedirs = _run_split_prepare(naming=naming)
        assert processor.final_output_path == "/data/Bram Stoker - Dracula.m4b"
        assert processor.split_output_dir == "/data/Bram Stoker - Dracula"
        assert processor.split_part_paths[0] == "/data/Bram Stoker - Dracula/Dracula - 1 - One.m4b"
        makedirs.assert_any_call("/data/Bram Stoker - Dracula", exist_ok=True)

    def test_nested_template_needs_no_guard(self):
        processor, _submitted, _ = _run_split_prepare()
        # The book already has its own folder, so the parts share it with the
        # sidecars rather than gaining a redundant level.
        assert processor.split_output_dir == os.path.dirname(processor.final_output_path)

    def test_missing_ch_placeholder_warns_exactly_once_per_book(self, caplog):
        # Backlog #37: the guard lives in the renderer, which runs once per part;
        # normalizing the template once per book keeps the warning to one line
        # instead of one per chapter file.
        naming = {"chapter_file_template": "{title} - {ch_title}"}
        with caplog.at_level(logging.WARNING):
            processor, _submitted, _ = _run_split_prepare(naming=naming)
        warnings = [r for r in caplog.records if "no {ch} placeholder" in r.getMessage()]
        assert len(warnings) == 1
        # ...and the appended {ch} still keeps the names unique and sortable.
        assert os.path.basename(processor.split_part_paths[0]) == "Dracula - One - 1.m4b"

    def test_present_ch_placeholder_warns_never(self, caplog):
        with caplog.at_level(logging.WARNING):
            _run_split_prepare()
        assert [r for r in caplog.records if "no {ch} placeholder" in r.getMessage()] == []

    def test_a_planning_failure_fails_the_book_fast_whatever_its_type(self):
        # W1: the guard here used to catch (OSError, ValueError) only, but the
        # realistic escapes are neither — the folder walk reads the ownership map
        # (sqlite3.OperationalError, "database is locked") and the chapter
        # template can be a hand-edited JSON object (AttributeError). Anything
        # that escapes is swallowed by the task runner, leaving the completion
        # event unset and `run` blocked on this book for the full two-hour
        # completion timeout — holding a download worker's slot — before
        # reporting a timeout for work that never started.
        for error in (sqlite3.OperationalError("database is locked"), AttributeError("'dict' object has no split")):
            with mock.patch.object(processing_logic.BookProcessor, "_plan_split_output", side_effect=error):
                processor, submitted, _ = _run_split_prepare()
            assert processor._completion_event.is_set()
            # Failed fast: no chunk was queued, and the failure was recorded once.
            assert submitted == []
            assert processor._failure_reported is True

    def test_the_completion_event_survives_a_failure_report_that_raises_too(self):
        # The same locked database that broke the planning breaks the ERROR write
        # — which re-raises by design — so the event is set in a `finally`. An
        # unreported failure is recoverable; a book that never unblocks `run` is
        # a worker slot held for two hours.
        captured = {}

        def failing_report(self, _message):
            captured["processor"] = self  # patched onto the class, so `self` arrives
            raise sqlite3.OperationalError("database is locked")

        with (
            mock.patch.object(
                processing_logic.BookProcessor,
                "_plan_split_output",
                side_effect=sqlite3.OperationalError("database is locked"),
            ),
            mock.patch.object(processing_logic.BookProcessor, "_update_db_on_failure", failing_report),
            # The report re-raises by design (see _update_db_on_failure), and in
            # production the task runner swallows it; what matters is the state
            # it leaves behind.
            pytest.raises(sqlite3.OperationalError),
        ):
            _run_split_prepare()
        assert captured["processor"]._completion_event.is_set()

    def test_split_book_still_encodes_one_chunk_per_chapter(self):
        processor, submitted, _ = _run_split_prepare()
        assert processor.total_chunks == 3
        assert all(t.func == processor._encode_and_track_chunk for t in submitted)
        assert all(t.priority == processing_logic.TaskPriority.ENCODE_CHAPTER for t in submitted)


class TestSplitFormatRouting:
    """v0.24.0 Phase 3, the routing matrix: three output formats x split on/off.

    With splitting OFF every format keeps the exact task it has always taken —
    one remux task, one single-pass MP3 task, or the chunk fan-out. With it ON
    all three route through the SAME chunk fan-out (there is no single file to
    remux or to encode in one pass), and the last chunk finalizes the parts in
    place instead of merging them.
    """

    def _funcs(self, submitted):
        return {t.func.__name__ for t in submitted}

    # --- Splitting off: today's pipeline, untouched ------------------------

    def test_unsplit_m4b_takes_the_chunk_fan_out(self):
        processor, submitted, _ = _run_split_prepare(split_output=False, output_format="m4b")
        assert self._funcs(submitted) == {"_encode_and_track_chunk"}
        assert len(submitted) == 3
        assert processor.split_part_paths == []

    def test_unsplit_mp3_takes_the_single_pass_encode(self):
        processor, submitted, _ = _run_split_prepare(split_output=False, output_format="mp3")
        assert [t.func for t in submitted] == [processor._encode_mp3_and_finalize]
        assert submitted[0].priority == processing_logic.TaskPriority.ENCODE_CHAPTER
        # Nothing was chunked, and nothing was planned as a part.
        assert processor.total_chunks == 0
        assert processor.split_part_paths == []

    def test_unsplit_original_takes_the_remux(self):
        processor, submitted, _ = _run_split_prepare(split_output=False, output_format="original")
        assert [t.func for t in submitted] == [processor._remux_and_finalize]
        assert submitted[0].priority == processing_logic.TaskPriority.MERGE_BOOK
        assert processor.total_chunks == 0

    # --- Splitting on: one fan-out for all three ---------------------------

    def test_split_m4b_fans_out(self):
        processor, submitted, _ = _run_split_prepare(output_format="m4b", split_encode_mode="aac")
        assert self._funcs(submitted) == {"_encode_and_track_chunk"}
        assert len(submitted) == 3
        assert processor.split_part_paths[0].endswith(".m4b")

    def test_split_mp3_fans_out_instead_of_encoding_in_one_pass(self):
        processor, submitted, _ = _run_split_prepare(output_format="mp3", split_encode_mode="mp3")
        assert self._funcs(submitted) == {"_encode_and_track_chunk"}
        assert len(submitted) == 3
        # The single-pass task is not queued at all...
        assert processor._encode_mp3_and_finalize not in [t.func for t in submitted]
        # ...and the planned parts carry the format's own extension.
        assert [os.path.basename(p) for p in processor.split_part_paths] == [
            "Dracula - 1 - One.mp3",
            "Dracula - 2 - Two.mp3",
            "Dracula - 3 - Three.mp3",
        ]

    def test_split_original_fans_out_instead_of_remuxing(self):
        processor, submitted, _ = _run_split_prepare(output_format="original", split_encode_mode="copy")
        assert self._funcs(submitted) == {"_encode_and_track_chunk"}
        assert processor._remux_and_finalize not in [t.func for t in submitted]
        assert processor.split_part_paths[0].endswith(".m4b")

    def test_split_original_is_not_reported_as_a_flac_fallback(self, caplog):
        # The "fell back to FLAC" line describes a real degradation; a lossless
        # split takes the fan-out on purpose and must not claim it re-encoded.
        with caplog.at_level(logging.INFO):
            _run_split_prepare(output_format="original", split_encode_mode="copy")
        assert [r for r in caplog.records if "fell back to FLAC" in r.getMessage()] == []

    def test_split_original_with_a_flac_master_still_reports_the_fallback(self, caplog):
        # That title genuinely cannot be copy-cut, so it re-encodes to .m4b —
        # the D14 spike's stated scope limit — and says so.
        with caplog.at_level(logging.INFO):
            processor, submitted, _ = _run_split_prepare(
                output_format="original",
                split_encode_mode="aac",
                audio_file="/tmp/x/master_intermediate.flac",
            )
        assert [r for r in caplog.records if "fell back to FLAC" in r.getMessage()]
        assert self._funcs(submitted) == {"_encode_and_track_chunk"}
        assert processor.split_part_paths[0].endswith(".m4b")


class TestSplitPartExtensionFollowsPrepare:
    """A part's filename extension comes from prepare's `split_encode_mode`, not
    from the extension of the reserved single-file path.

    The two are separate reads of the output format, minutes apart: the reserved
    path is named before the download starts, while prepare picks the container
    once the download and decrypt have finished. A user flipping Output Format in
    between must not end up with LAME audio in files named ".m4b" (or the
    reverse) — the extension, the chunk's container and the encode mode agree by
    construction, in both flip directions.
    """

    def test_flip_to_mp3_names_the_parts_mp3_despite_a_reserved_m4b_path(self):
        # Reserved as .m4b (format was M4B at reservation time), but prepare's
        # later read chose the LAME variant.
        processor, _submitted, _ = _run_split_prepare(output_format="m4b", split_encode_mode="mp3")
        assert processor.final_output_path.endswith(".m4b")
        assert [os.path.basename(p) for p in processor.split_part_paths] == [
            "Dracula - 1 - One.mp3",
            "Dracula - 2 - Two.mp3",
            "Dracula - 3 - Three.mp3",
        ]

    def test_flip_away_from_mp3_names_the_parts_m4b_despite_a_reserved_mp3_path(self):
        # The inverse: reserved as .mp3, but prepare chose the AAC re-encode.
        processor, _submitted, _ = _run_split_prepare(output_format="mp3", split_encode_mode="aac")
        assert processor.final_output_path.endswith(".mp3")
        assert [os.path.basename(p) for p in processor.split_part_paths] == [
            "Dracula - 1 - One.m4b",
            "Dracula - 2 - Two.m4b",
            "Dracula - 3 - Three.m4b",
        ]

    def test_flip_from_mp3_to_original_names_the_parts_m4b(self):
        # The third mode: copy-cut parts are mp4, so they follow the same rule.
        processor, _submitted, _ = _run_split_prepare(output_format="mp3", split_encode_mode="copy")
        assert processor.final_output_path.endswith(".mp3")
        assert all(p.endswith(".m4b") for p in processor.split_part_paths)

    def test_a_context_without_a_mode_still_plans_m4b_parts(self):
        # Same degrade-to-AAC default encode_chapter_chunk applies to an older
        # context that predates the key, so the names can't disagree with it.
        processor, _submitted, _ = _run_split_prepare(output_format="m4b", split_encode_mode=None)
        assert all(p.endswith(".m4b") for p in processor.split_part_paths)


class TestSplitFinalTaskSelection:
    """The last chunk to finish submits the finalize-split task in place of the
    merge, at the same MERGE_BOOK priority."""

    def _run(self, split_part_paths):
        processor = BookProcessor(asin="B0OURS", job_id=1)
        processor.total_chunks = 1
        processor.split_part_paths = split_part_paths
        submitted = []
        with (
            mock.patch.object(processing_logic, "encode_chapter_chunk", return_value="/tmp/x/chunk_000.m4b"),
            mock.patch.object(processing_logic, "_yield_progress"),
            mock.patch.object(processing_logic.task_runner, "submit_task", side_effect=submitted.append),
        ):
            processor._encode_and_track_chunk({"index": 0, "total_chunks": 1, "start": 0, "duration": 1})
        return processor, submitted

    def test_unsplit_book_submits_the_merge(self):
        processor, submitted = self._run([])
        assert len(submitted) == 1
        assert submitted[0].func == processor._merge_and_finalize
        assert submitted[0].priority == processing_logic.TaskPriority.MERGE_BOOK

    def test_split_book_submits_the_finalize_instead(self):
        processor, submitted = self._run(["/data/A/T/T - 1 - One.m4b"])
        assert len(submitted) == 1
        assert submitted[0].func == processor._finalize_split
        assert submitted[0].priority == processing_logic.TaskPriority.MERGE_BOOK


class TestPromoteSplitParts:
    """Parts are covered in the temp dir, then moved into the library through a
    ".part" staging name — and a failure part-way leaves nothing behind."""

    def _processor(self, tmp_path, part_count=2, ext=".m4b"):
        processor = BookProcessor(asin="B0OURS", job_id=1)
        temp_dir = tmp_path / "temp"
        out_dir = tmp_path / "out" / "Dracula"
        temp_dir.mkdir()
        chunks = []
        for index in range(part_count):
            chunk = temp_dir / f"chunk_{index:03d}{ext}"
            chunk.write_bytes(f"audio {index}".encode())
            chunks.append(str(chunk))
        processor.temp_dir = str(temp_dir)
        # Deliberately NOT in index order: chunks are appended as they finish.
        processor.encoded_chunk_paths = list(reversed(chunks))
        processor.split_output_dir = str(out_dir)
        processor.split_part_paths = [str(out_dir / f"Dracula - {i + 1}{ext}") for i in range(part_count)]
        processor.context = {"cover_file": None}
        return processor, out_dir

    def test_all_parts_land_in_chapter_order(self, tmp_path):
        processor, out_dir = self._processor(tmp_path)
        with mock.patch.object(processing_logic, "_embed_cover_art") as cover:
            assert processor._promote_split_parts() is True
        assert (out_dir / "Dracula - 1.m4b").read_bytes() == b"audio 0"
        assert (out_dir / "Dracula - 2.m4b").read_bytes() == b"audio 1"
        # No staging files left, and the temp copies are gone.
        assert sorted(p.name for p in out_dir.iterdir()) == ["Dracula - 1.m4b", "Dracula - 2.m4b"]
        assert list((tmp_path / "temp").iterdir()) == []
        # The cover is embedded per part, while each is still in the temp dir.
        assert cover.call_count == 2

    def test_mp3_parts_skip_the_atomicparsley_pass(self, tmp_path):
        # Phase 3: AtomicParsley only understands mp4. An .mp3 part already
        # carries its cover as an id3v2 APIC frame, muxed during the encode.
        processor, out_dir = self._processor(tmp_path, ext=".mp3")
        processor.context = {"cover_file": str(tmp_path / "cover.jpg")}
        with mock.patch.object(processing_logic, "_embed_cover_art") as cover:
            assert processor._promote_split_parts() is True
        cover.assert_not_called()
        # ...and the parts still land exactly as the .m4b ones do.
        assert (out_dir / "Dracula - 1.mp3").read_bytes() == b"audio 0"
        assert (out_dir / "Dracula - 2.mp3").read_bytes() == b"audio 1"
        assert list((tmp_path / "temp").iterdir()) == []

    def test_m4b_parts_still_get_the_atomicparsley_pass(self, tmp_path):
        processor, _out_dir = self._processor(tmp_path, ext=".m4b")
        processor.context = {"cover_file": str(tmp_path / "cover.jpg")}
        with mock.patch.object(processing_logic, "_embed_cover_art") as cover:
            assert processor._promote_split_parts() is True
        assert cover.call_count == 2

    def test_a_failed_move_removes_what_already_landed(self, tmp_path):
        processor, out_dir = self._processor(tmp_path)
        real_move = processing_logic.shutil.move
        calls = []

        def failing_move(src, dst):
            calls.append(src)
            if len(calls) == 2:
                raise OSError("No space left on device")
            return real_move(src, dst)

        with (
            mock.patch.object(processing_logic, "_embed_cover_art"),
            mock.patch.object(processing_logic.shutil, "move", side_effect=failing_move),
        ):
            assert processor._promote_split_parts() is False
        # Half a book in the library is worse than none of it.
        assert not (out_dir / "Dracula - 1.m4b").exists()
        assert not (out_dir / "Dracula - 2.m4b").exists()

    def test_a_cancel_mid_promotion_removes_what_already_landed(self, tmp_path):
        # M4: a cancel cannot SIGTERM a shutil.move, so without a per-part recheck
        # the loop promotes the whole remaining set and _finalize_success's cancel
        # branch then leaves every one of them in the library, untracked.
        processor, out_dir = self._processor(tmp_path)
        processor.stop_event = Event()
        real_move = processing_logic.shutil.move

        def move_then_cancel(src, dst):
            result = real_move(src, dst)
            processor.stop_event.set()  # The cancel lands after the first part.
            return result

        with (
            mock.patch.object(processing_logic, "_embed_cover_art"),
            mock.patch.object(processing_logic.shutil, "move", side_effect=move_then_cancel),
        ):
            assert processor._promote_split_parts() is False

        assert not (out_dir / "Dracula - 1.m4b").exists()
        assert not (out_dir / "Dracula - 2.m4b").exists()

    def test_a_mismatched_chunk_count_refuses_to_place_anything(self, tmp_path):
        processor, out_dir = self._processor(tmp_path)
        processor.encoded_chunk_paths = processor.encoded_chunk_paths[:1]
        with mock.patch.object(processing_logic, "_embed_cover_art") as cover:
            assert processor._promote_split_parts() is False
        cover.assert_not_called()
        assert not out_dir.exists()


class TestPromotionSparesThePreviousDownload:
    """B1/W8: a split -> split re-download deliberately overwrites the previous
    download's own chapter files IN PLACE — the folder walk subtracts this ASIN,
    so a book's own parts never read as a collision, and unchanged metadata
    re-renders identical part names. Every teardown around promotion therefore
    has to tell a file this run created from the file the user already had.

    Deleting the second is the one thing in this release that destroys data on an
    ordinary action: cancel is one click in the job panel, and promotion is a
    full cross-device copy of the whole book, so the window is minutes wide. The
    tests below all start from a REAL previous split set on disk, tracked by
    `book_files` rows — the state every earlier failure test was missing."""

    def _processor(self, tmp_path, part_count=3, previous=(0, 1)):
        """A re-download in flight: `part_count` planned targets, of which the
        `previous` ones are already on disk AND already in `book_files`."""
        processor = BookProcessor(asin="B0OURS", job_id=1)
        temp_dir = tmp_path / "temp"
        out_dir = tmp_path / "out" / "Dracula"
        temp_dir.mkdir()
        out_dir.mkdir(parents=True)
        chunks = []
        for index in range(part_count):
            chunk = temp_dir / f"chunk_{index:03d}.m4b"
            chunk.write_text(f"new audio {index}", encoding="utf-8")
            chunks.append(str(chunk))
        processor.temp_dir = str(temp_dir)
        processor.encoded_chunk_paths = chunks
        processor.split_output_dir = str(out_dir)
        processor.split_part_paths = [str(out_dir / f"Dracula - {i + 1}.m4b") for i in range(part_count)]
        processor.context = {"cover_file": None}

        tracked = []
        for index in previous:
            path = processor.split_part_paths[index]
            with open(path, "w", encoding="utf-8") as handle:
                handle.write(f"previous audio {index}")
            tracked.append(path)
        return processor, out_dir, tracked

    def _patches(self, tracked, move=None):
        """The DB seam (`book_files`) plus the cover pass, and optionally a
        substitute for shutil.move that injects the failure."""
        patches = [
            mock.patch.object(processing_logic, "_embed_cover_art"),
            mock.patch.object(processing_logic, "_tracked_part_paths", return_value=list(tracked)),
        ]
        if move is not None:
            patches.append(mock.patch.object(processing_logic.shutil, "move", side_effect=move))
        return patches

    def test_a_cancel_mid_promotion_keeps_the_previous_downloads_parts(self, tmp_path):
        processor, out_dir, tracked = self._processor(tmp_path)
        processor.stop_event = Event()
        real_move = processing_logic.shutil.move

        def move_then_cancel(src, dst):
            result = real_move(src, dst)
            processor.stop_event.set()  # The cancel lands after the first part.
            return result

        with contextlib.ExitStack() as stack:
            for patch in self._patches(tracked, move=move_then_cancel):
                stack.enter_context(patch)
            assert processor._promote_split_parts() is False

        # Part 1 was already replaced when the cancel arrived: it holds this
        # run's audio, which is a complete chapter file the rows already name.
        # Deleting it — what the rollback used to do — would leave the user with
        # a hole in a book they had before pressing Download.
        assert (out_dir / "Dracula - 1.m4b").read_text() == "new audio 0"
        # Part 2 was never reached, so it is still the previous download's.
        assert (out_dir / "Dracula - 2.m4b").read_text() == "previous audio 1"
        # The third target belonged to nothing but this run, so it goes...
        assert not (out_dir / "Dracula - 3.m4b").exists()
        # ...and the folder stays, because it is not empty.
        assert out_dir.exists()

    def test_a_failed_move_keeps_the_previous_downloads_parts(self, tmp_path):
        processor, out_dir, tracked = self._processor(tmp_path)
        real_move = processing_logic.shutil.move
        calls = []

        def failing_move(src, dst):
            calls.append(src)
            if len(calls) == 3:
                raise OSError("No space left on device")
            return real_move(src, dst)

        with contextlib.ExitStack() as stack:
            for patch in self._patches(tracked, move=failing_move):
                stack.enter_context(patch)
            assert processor._promote_split_parts() is False

        assert (out_dir / "Dracula - 1.m4b").read_text() == "new audio 0"
        assert (out_dir / "Dracula - 2.m4b").read_text() == "new audio 1"
        assert not (out_dir / "Dracula - 3.m4b").exists()

    def test_a_failed_first_download_still_takes_everything_back(self, tmp_path):
        # The control, and the behavior the rollback was written for: with no
        # previous set to protect, half a book in the library is still worse than
        # none of it — every placed part goes and the folder with it.
        processor, out_dir, _tracked = self._processor(tmp_path, previous=())
        real_move = processing_logic.shutil.move
        calls = []

        def failing_move(src, dst):
            calls.append(src)
            if len(calls) == 3:
                raise OSError("No space left on device")
            return real_move(src, dst)

        with contextlib.ExitStack() as stack:
            for patch in self._patches([], move=failing_move):
                stack.enter_context(patch)
            assert processor._promote_split_parts() is False

        assert not out_dir.exists()

    def test_a_target_no_row_names_is_not_mistaken_for_a_tracked_one(self, tmp_path):
        # Both halves of the predicate are load-bearing. Here the row read plainly
        # SUCCEEDED — it names this book's parts under their previous chapter
        # naming — and none of them is where this run is writing, so what is
        # sitting at the targets is this run's own orphaned output rather than a
        # previous download to protect.
        processor, out_dir, _tracked = self._processor(tmp_path, previous=(0, 1))
        processor.stop_event = Event()
        real_move = processing_logic.shutil.move
        elsewhere = [str(out_dir / "Dracula - 01 - One.m4b")]

        def move_then_cancel(src, dst):
            result = real_move(src, dst)
            processor.stop_event.set()
            return result

        with contextlib.ExitStack() as stack:
            for patch in self._patches(elsewhere, move=move_then_cancel):
                stack.enter_context(patch)
            assert processor._promote_split_parts() is False

        assert not (out_dir / "Dracula - 1.m4b").exists()

    def test_a_row_read_that_answers_nothing_spares_everything_on_disk(self, tmp_path):
        # F1: `_tracked_part_paths` swallows sqlite3.Error and answers [] — the
        # same answer a book with no part rows gives — so an empty read with
        # files already at our targets cannot be read as "these are disposable".
        # A lock on that one SELECT would otherwise hand the cancel path straight
        # back to deleting the user's previous chapter files.
        processor, out_dir, _tracked = self._processor(tmp_path, previous=(0, 1))
        processor.stop_event = Event()
        real_move = processing_logic.shutil.move

        def move_then_cancel(src, dst):
            result = real_move(src, dst)
            processor.stop_event.set()
            return result

        with contextlib.ExitStack() as stack:
            for patch in self._patches([], move=move_then_cancel):
                stack.enter_context(patch)
            assert processor._promote_split_parts() is False

        assert (out_dir / "Dracula - 1.m4b").read_text() == "new audio 0"
        assert (out_dir / "Dracula - 2.m4b").read_text() == "previous audio 1"
        # ...and the target nothing was ever at is still not there, so a failed
        # FIRST download (nothing on disk, nothing to spare) still rolls back.
        assert not (out_dir / "Dracula - 3.m4b").exists()

    def test_a_mixed_set_spares_the_old_parts_and_removes_the_new_ones(self, tmp_path):
        # F2: with every promoted part also pre-existing, a rollback that skipped
        # its delete loop entirely would look identical. Only one target belongs
        # to the previous download here, so the two halves have to be told apart:
        # part 1 survives, part 2 — placed by this run, tracked by nothing — goes.
        processor, out_dir, tracked = self._processor(tmp_path, previous=(0,))
        real_move = processing_logic.shutil.move
        calls = []

        def failing_move(src, dst):
            calls.append(src)
            if len(calls) == 3:
                raise OSError("No space left on device")
            return real_move(src, dst)

        with contextlib.ExitStack() as stack:
            for patch in self._patches(tracked, move=failing_move):
                stack.enter_context(patch)
            assert processor._promote_split_parts() is False

        assert (out_dir / "Dracula - 1.m4b").read_text() == "new audio 0"
        assert not (out_dir / "Dracula - 2.m4b").exists()
        assert not (out_dir / "Dracula - 3.m4b").exists()
        # The folder still holds the spared part, so the prune leaves it alone.
        assert out_dir.exists()

    def test_a_raising_finalize_discards_only_the_parts_this_run_created(self, tmp_path):
        # W8: the part-row write is allowed to raise (a locked database), and that
        # transaction rolls back whole — so the N parts promotion has already moved
        # into /data are referenced by nothing and a later deep sync would adopt
        # them. They are discarded, in the same currency B1 uses: the previous
        # download's own files stay, because the surviving rows still name them.
        processor, out_dir, tracked = self._processor(tmp_path)
        with contextlib.ExitStack() as stack:
            for patch in self._patches(tracked):
                stack.enter_context(patch)
            stack.enter_context(mock.patch.object(processing_logic, "_yield_progress"))
            stack.enter_context(
                mock.patch.object(
                    processor, "_finalize_success", side_effect=sqlite3.OperationalError("database is locked")
                )
            )
            fail = stack.enter_context(mock.patch.object(processor, "_update_db_on_failure"))
            processor._finalize_split()

        assert not (out_dir / "Dracula - 3.m4b").exists()
        assert (out_dir / "Dracula - 1.m4b").read_text() == "new audio 0"
        assert (out_dir / "Dracula - 2.m4b").read_text() == "new audio 1"
        fail.assert_called_once()
        assert processor._completion_event.is_set()

    def test_a_raising_finalize_on_a_first_download_leaves_nothing_behind(self, tmp_path):
        # The same failure with no previous set: every promoted part is untracked
        # orphan output, so the folder comes back empty and goes.
        processor, out_dir, _tracked = self._processor(tmp_path, previous=())
        with contextlib.ExitStack() as stack:
            for patch in self._patches([]):
                stack.enter_context(patch)
            stack.enter_context(mock.patch.object(processing_logic, "_yield_progress"))
            stack.enter_context(
                mock.patch.object(
                    processor, "_finalize_success", side_effect=sqlite3.OperationalError("database is locked")
                )
            )
            stack.enter_context(mock.patch.object(processor, "_update_db_on_failure"))
            processor._finalize_split()

        assert not out_dir.exists()

    def test_a_non_oserror_escape_still_rolls_the_promotion_back(self, tmp_path):
        # N2: the rollback is the only thing between a failure and half a book in
        # the library, so it cannot be reachable only by OSError.
        processor, out_dir, _tracked = self._processor(tmp_path, previous=())
        real_move = processing_logic.shutil.move
        calls = []

        def failing_move(src, dst):
            calls.append(src)
            if len(calls) == 3:
                raise RuntimeError("something nobody predicted")
            return real_move(src, dst)

        with contextlib.ExitStack() as stack:
            for patch in self._patches([], move=failing_move):
                stack.enter_context(patch)
            assert processor._promote_split_parts() is False

        assert not out_dir.exists()


class TestSplitOutputVerification:
    """D10: every part is checked before any row is written, and the durations
    are summed against the book's runtime under the existing tolerance."""

    def _processor(self, tmp_path, sizes=(200_000, 200_000), chapters=None):
        processor = BookProcessor(asin="B0OURS", job_id=1)
        out_dir = tmp_path / "out"
        out_dir.mkdir()
        processor.final_output_path = str(out_dir / "Dracula.m4b")
        processor.split_output_dir = str(out_dir)
        processor.split_part_paths = []
        for index, size in enumerate(sizes):
            part = out_dir / f"Dracula - {index + 1}.m4b"
            if size is not None:
                part.write_bytes(b"\0" * size)
            processor.split_part_paths.append(str(part))
        processor.context = {
            "chapters": chapters
            or [{"title": "x", "start_offset_ms": 0, "length_ms": 1_800_000} for _ in range(len(sizes))]
        }
        return processor

    def _verify(self, processor, runtime_min=60, durations=(1800.0, 1800.0)):
        con = mock.MagicMock()
        con.__enter__.return_value = con
        con.execute.return_value.fetchone.return_value = {"runtime_min": runtime_min}
        probes = list(durations)
        with (
            mock.patch.object(processing_logic, "get_db_connection", return_value=con),
            mock.patch.object(processing_logic, "_probe_duration_seconds", side_effect=lambda *_a, **_k: probes.pop(0)),
        ):
            return processor._verify_output_file()

    def test_all_parts_present_and_long_enough_passes(self, tmp_path):
        assert self._verify(self._processor(tmp_path)) == (True, None)

    def test_a_missing_part_fails_the_book(self, tmp_path):
        processor = self._processor(tmp_path, sizes=(200_000, None))
        ok, reason = self._verify(processor)
        assert ok is False
        assert "Chapter file 2 of 2 is missing" in reason

    def test_a_stub_part_fails_the_book(self, tmp_path):
        processor = self._processor(tmp_path, sizes=(200_000, 500))
        ok, reason = self._verify(processor)
        assert ok is False
        assert "implausibly small" in reason

    def test_a_short_chapter_is_not_held_to_the_whole_book_floor(self, tmp_path):
        # The minimum-duration merge lets a 3-second chapter become its own file,
        # and three seconds of AAC is well under the 64 KiB whole-book floor.
        processor = self._processor(
            tmp_path,
            sizes=(200_000, 40_000),
            chapters=[
                {"title": "long", "start_offset_ms": 0, "length_ms": 3_596_000},
                {"title": "short", "start_offset_ms": 3_596_000, "length_ms": 4_000},
            ],
        )
        assert self._verify(processor, durations=(3596.0, 4.0)) == (True, None)

    def test_an_empty_short_part_still_fails(self, tmp_path):
        processor = self._processor(
            tmp_path,
            sizes=(200_000, 300),
            chapters=[
                {"title": "long", "start_offset_ms": 0, "length_ms": 3_596_000},
                {"title": "short", "start_offset_ms": 3_596_000, "length_ms": 4_000},
            ],
        )
        ok, reason = self._verify(processor, durations=(3596.0, 4.0))
        assert ok is False
        assert "implausibly small" in reason

    def test_an_unreadable_part_fails_the_book(self, tmp_path):
        ok, reason = self._verify(self._processor(tmp_path), durations=(1800.0, None))
        assert ok is False
        assert "could not be read back" in reason

    def test_summed_durations_are_compared_not_individual_ones(self, tmp_path):
        # Each part is a fraction of the runtime; only the sum is meaningful.
        assert self._verify(self._processor(tmp_path), durations=(1790.0, 1795.0)) == (True, None)

    def test_a_truncated_set_fails_the_book(self, tmp_path):
        ok, reason = self._verify(self._processor(tmp_path), durations=(600.0, 600.0))
        assert ok is False
        assert "truncated" in reason

    def test_unknown_runtime_skips_the_duration_check(self, tmp_path):
        assert self._verify(self._processor(tmp_path), runtime_min=None, durations=()) == (True, None)

    def test_one_truncated_part_fails_the_book_even_when_the_sum_survives(self, tmp_path):
        # M3: a 30-minute chapter that came out 2 seconds long clears the size
        # floor and barely dents the summed total, so only the per-part check
        # sees it. This is what makes D10's "one truncated part fails the book"
        # literally true.
        processor = self._processor(
            tmp_path,
            sizes=(200_000, 200_000, 200_000),
            chapters=[
                {"title": "one", "start_offset_ms": 0, "length_ms": 1_800_000},
                {"title": "two", "start_offset_ms": 1_800_000, "length_ms": 1_800_000},
                {"title": "three", "start_offset_ms": 3_600_000, "length_ms": 60_000},
            ],
        )
        ok, reason = self._verify(processor, runtime_min=61, durations=(1800.0, 1800.0, 2.0))
        assert ok is False
        assert "Chapter file 3 of 3 is truncated" in reason

    def test_normal_encoder_slack_still_passes(self, tmp_path):
        # The tolerance is deliberately lenient: frame/priming slack shifts a
        # part's real duration off its chapter length by a second or two, and a
        # short part loses proportionally more of it.
        processor = self._processor(
            tmp_path,
            sizes=(200_000, 40_000),
            chapters=[
                {"title": "long", "start_offset_ms": 0, "length_ms": 1_800_000},
                {"title": "short", "start_offset_ms": 1_800_000, "length_ms": 8_000},
            ],
        )
        assert self._verify(processor, runtime_min=30, durations=(1798.4, 6.1)) == (True, None)

    def test_a_chapter_without_a_length_skips_the_per_part_check(self, tmp_path):
        # Nothing to compare against, so the part is judged by the size floor and
        # the summed total alone rather than by a guessed length. The first part
        # is deliberately two seconds long: a duration that would fail the
        # per-part check outright, so the pass below can only be the check being
        # skipped rather than a duration that would have cleared it anyway.
        processor = self._processor(
            tmp_path,
            chapters=[
                {"title": "one", "start_offset_ms": 0},
                {"title": "two", "start_offset_ms": 1_800_000, "length_ms": 1_800_000},
            ],
        )
        assert self._verify(processor, runtime_min=30, durations=(2.0, 1800.0)) == (True, None)

        # ...and the paired case that proves it: the same 2-second part fails the
        # moment its chapter carries a length.
        second = tmp_path / "second"
        second.mkdir()
        with_length = self._processor(
            second,
            chapters=[
                {"title": "one", "start_offset_ms": 0, "length_ms": 1_800_000},
                {"title": "two", "start_offset_ms": 1_800_000, "length_ms": 1_800_000},
            ],
        )
        ok, reason = self._verify(with_length, runtime_min=30, durations=(2.0, 1800.0))
        assert ok is False
        assert "Chapter file 1 of 2 is truncated" in reason


class TestSplitFinalizeSuccess:
    """The finalize-split task shares the success path with every other format:
    verify first, then ONE transaction carrying the parent row and the part rows
    (D3/D10), and no ETA pollution (D15)."""

    PARTS = ["/data/A/Dracula/Dracula - 1 - One.m4b", "/data/A/Dracula/Dracula - 2 - Two.m4b"]

    def _run(self, promoted=True, verify=(True, None), replace_error=None, fail_error=None):
        processor = BookProcessor(asin="B0OURS", job_id=1)
        processor.final_output_path = "/data/A/Dracula/A - Dracula.m4b"
        processor.split_output_dir = "/data/A/Dracula"
        processor.split_part_paths = list(self.PARTS)
        con = mock.MagicMock()
        con.__enter__.return_value = con
        con.execute.return_value.fetchone.return_value = {"runtime_min": 60, "filepath": None}
        with (
            mock.patch.object(processor, "_promote_split_parts", return_value=promoted),
            mock.patch.object(processing_logic, "get_db_connection", return_value=con) as connect,
            mock.patch.object(processing_logic, "replace_book_files", side_effect=replace_error) as replace,
            mock.patch.object(processing_logic, "record_conversion_time") as record_eta,
            mock.patch.object(processing_logic, "_yield_progress") as progress,
            mock.patch("os.rmdir") as prune,
            mock.patch.object(processor, "_verify_output_file", return_value=verify),
            mock.patch.object(processor, "_place_supplementary_pdf") as pdf,
            mock.patch.object(processor, "_place_sidecar_files") as sidecars,
            mock.patch.object(processor, "_apply_file_timestamps") as timestamps,
            mock.patch.object(processor, "_cleanup_stale_files") as cleanup,
            mock.patch.object(processor, "_update_db_on_failure", side_effect=fail_error) as fail,
            mock.patch("os.path.exists", return_value=False),
        ):
            processor._finalize_split()
        downloaded = [c for c in con.execute.call_args_list if "status = 'DOWNLOADED'" in c.args[0]]
        return {
            "processor": processor,
            "con": con,
            "connect": connect,
            "downloaded": downloaded,
            "replace": replace,
            "record_eta": record_eta,
            "progress": progress,
            "prune": prune,
            "pdf": pdf,
            "sidecars": sidecars,
            "timestamps": timestamps,
            "cleanup": cleanup,
            "fail": fail,
        }

    def test_success_tracks_the_folder_and_writes_the_part_rows(self):
        r = self._run()
        r["fail"].assert_not_called()
        assert len(r["downloaded"]) == 1
        # audiobooks.filepath holds the FOLDER for a split book (D3).
        assert r["downloaded"][0].args[1][0] == "/data/A/Dracula"
        # ...and the authoritative per-file list goes in the SAME transaction.
        # The connection COUNT is what proves that: with one shared fake
        # connection, passing `con=` would look identical if the parent row and
        # the part rows sat in two separate `with` blocks committing
        # independently. Exactly one is opened for the whole finalize.
        assert r["connect"].call_count == 1
        r["replace"].assert_called_once_with("B0OURS", self.PARTS, con=r["con"])

    def test_part_rows_are_written_in_playback_order(self):
        r = self._run()
        assert r["replace"].call_args.args[1] == self.PARTS

    def test_success_does_not_pollute_eta_history(self):
        # D15: a split conversion's wall clock isn't comparable to the
        # single-file re-encode the estimator models.
        self._run()["record_eta"].assert_not_called()

    def test_success_places_sidecars_and_stamps_timestamps(self):
        r = self._run()
        r["pdf"].assert_called_once()
        r["sidecars"].assert_called_once()
        r["timestamps"].assert_called_once()
        r["cleanup"].assert_called_once()

    def test_finalize_reports_progress_at_ninety_five(self):
        r = self._run()
        assert 95 in [c.args[2] for c in r["progress"].call_args_list]

    def test_failed_verification_writes_nothing(self):
        # The whole point of D10: one bad part fails the BOOK before any row is
        # touched, so no half-tracked split book can exist.
        r = self._run(verify=(False, "Chapter file 2 of 2 is missing; ..."))
        assert r["downloaded"] == []
        r["replace"].assert_not_called()
        r["fail"].assert_called_once_with("Chapter file 2 of 2 is missing; ...")

    def test_failed_promotion_reports_and_writes_nothing(self):
        r = self._run(promoted=False)
        assert r["downloaded"] == []
        r["replace"].assert_not_called()
        r["fail"].assert_called_once_with("Placing the per-chapter files failed.")

    def test_the_completion_event_is_always_set(self):
        assert self._run(promoted=False)["processor"]._completion_event.is_set()
        assert self._run()["processor"]._completion_event.is_set()

    def test_failed_verification_removes_every_part(self):
        processor = BookProcessor(asin="B0OURS", job_id=1)
        processor.final_output_path = "/data/A/Dracula/A - Dracula.m4b"
        processor.split_output_dir = "/data/A/Dracula"
        processor.split_part_paths = list(self.PARTS)
        con = mock.MagicMock()
        con.__enter__.return_value = con
        con.execute.return_value.fetchone.return_value = {"runtime_min": 60, "filepath": None}
        with (
            mock.patch.object(processing_logic, "get_db_connection", return_value=con),
            mock.patch.object(processing_logic, "_yield_progress"),
            mock.patch("os.rmdir") as prune,
            mock.patch.object(processor, "_verify_output_file", return_value=(False, "truncated")),
            mock.patch.object(processor, "_update_db_on_failure"),
            mock.patch("os.path.exists", return_value=True),
            mock.patch("os.remove") as remove,
        ):
            processor._finalize_success(conversion_start_time=0, record_eta=False)
        assert [c.args[0] for c in remove.call_args_list] == self.PARTS
        # M5: removing every part empties the folder _plan_split_output created
        # before the first chunk was queued — most visibly D5's flat-guard
        # subfolder, a level that did not exist before this run. That folder, and
        # nothing above it (W3).
        prune.assert_called_once_with("/data/A/Dracula")

    def test_an_unsplit_failed_verification_prunes_no_directory(self):
        # The unsplit path never had a folder of its own and must stay exactly as
        # it was: the single failed file goes, nothing else is touched.
        processor = BookProcessor(asin="B0OURS", job_id=1)
        processor.final_output_path = "/data/A/Dracula/A - Dracula.m4b"
        con = mock.MagicMock()
        con.__enter__.return_value = con
        con.execute.return_value.fetchone.return_value = {"runtime_min": 60, "filepath": None}
        with (
            mock.patch.object(processing_logic, "get_db_connection", return_value=con),
            mock.patch.object(processing_logic, "_yield_progress"),
            mock.patch("os.rmdir") as prune,
            mock.patch.object(processor, "_verify_output_file", return_value=(False, "truncated")),
            mock.patch.object(processor, "_update_db_on_failure"),
            mock.patch("os.path.exists", return_value=True),
            mock.patch("os.remove") as remove,
        ):
            processor._finalize_success(conversion_start_time=0, record_eta=False)
        assert [c.args[0] for c in remove.call_args_list] == ["/data/A/Dracula/A - Dracula.m4b"]
        prune.assert_not_called()

    def test_a_raising_part_row_write_fails_the_book_instead_of_hanging_it(self):
        # W2: the part-row write is deliberately allowed to propagate, and the
        # realistic cause is "database is locked". Before, that exception unwound
        # past the completion event and `run` sat on it for the full completion
        # timeout — two hours minimum — before reporting a bogus timeout.
        r = self._run(replace_error=sqlite3.OperationalError("database is locked"))
        assert r["processor"]._completion_event.is_set()
        r["fail"].assert_called_once()
        assert "database is locked" in r["fail"].call_args.args[0]
        # The transaction rolled back, so nothing follows a failed write.
        r["cleanup"].assert_not_called()

    def test_a_raising_failure_report_still_sets_the_completion_event(self):
        # ...and the failure write can hit the very same locked database. There is
        # nothing left to do about it, but the book must still stop blocking `run`.
        r = self._run(
            replace_error=sqlite3.OperationalError("database is locked"),
            fail_error=sqlite3.OperationalError("database is locked"),
        )
        assert r["processor"]._completion_event.is_set()


class TestPostTimeoutSplitFinalize:
    """B1: the split finalize needs the same post-timeout guard the MP3 encode
    carries, and for the same reason.

    The chunked and remux paths are covered incidentally — their inputs live in
    the temp dir `run` has already deleted, so they simply fail — but promotion
    moves a split book's parts OUT of the temp dir into /data, where they outlive
    it. Without the guard a run that already recorded ERROR flips the book back
    to DOWNLOADED, resets retry_count, and runs the stale-file cleanup, deleting
    the user's previous copy on behalf of a run the system gave up on."""

    def _finalize(self, tmp_path, timed_out, on_disk=(0, 1), staged=()):
        """Drive _finalize_split with promotion already done and the parts named
        by `on_disk` sitting in the book's folder — the timeout can land before,
        during or after promotion, so the set on disk may be empty, partial or
        complete. `staged` names any ".part" a half-finished move left behind."""
        processor = BookProcessor(asin="B0OURS", job_id=7)
        out_dir = tmp_path / "Dracula"
        out_dir.mkdir()
        processor.final_output_path = str(tmp_path / "Bram Stoker - Dracula.m4b")
        processor.split_output_dir = str(out_dir)
        processor.split_part_paths = [
            str(out_dir / "Dracula - 01 - One.m4b"),
            str(out_dir / "Dracula - 02 - Two.m4b"),
        ]
        for index in on_disk:
            with open(processor.split_part_paths[index], "w", encoding="utf-8") as handle:
                handle.write("finished part")
        for index in staged:
            with open(f"{processor.split_part_paths[index]}.part", "w", encoding="utf-8") as handle:
                handle.write("half-moved part")
        if timed_out:
            processor._timed_out.set()

        with (
            mock.patch.object(processor, "_promote_split_parts", return_value=True),
            mock.patch.object(processor, "_finalize_success") as finalize,
            mock.patch.object(processor, "_cleanup_stale_files") as cleanup,
            mock.patch.object(processing_logic, "_yield_progress"),
        ):
            processor._finalize_split()
        return processor, finalize, cleanup

    def test_a_post_timeout_finalize_does_not_finalize(self, tmp_path):
        processor, finalize, cleanup = self._finalize(tmp_path, timed_out=True)
        finalize.assert_not_called()
        # The destructive step the guard exists for: the previous download stays.
        cleanup.assert_not_called()
        # Every orphaned part is discarded, so a later deep sync can't adopt them
        # as a DOWNLOADED book the DB says failed.
        assert [p for p in processor.split_part_paths if os.path.exists(p)] == []
        assert processor._completion_event.is_set()

    def test_a_partly_promoted_set_is_discarded_too(self, tmp_path):
        # The timeout landed between two parts: one file placed, one staged.
        processor, finalize, _cleanup = self._finalize(tmp_path, timed_out=True, on_disk=(0,), staged=(1,))
        finalize.assert_not_called()
        assert [p for p in processor.split_part_paths if os.path.exists(p)] == []
        assert not os.path.exists(f"{processor.split_part_paths[1]}.part")

    def test_nothing_promoted_yet_is_a_clean_no_op(self, tmp_path):
        processor, finalize, _cleanup = self._finalize(tmp_path, timed_out=True, on_disk=())
        finalize.assert_not_called()
        assert processor._completion_event.is_set()

    def test_a_normal_finalize_still_succeeds(self, tmp_path):
        processor, finalize, _cleanup = self._finalize(tmp_path, timed_out=False)
        finalize.assert_called_once()
        assert finalize.call_args.kwargs == {"record_eta": False}
        assert all(os.path.exists(p) for p in processor.split_part_paths)


class TestFailedSplitRunLeavesNoEmptyFolder:
    """Review M1: _plan_split_output creates the book's output folder before the
    first chunk is queued, so a failed or cancelled encode used to leave an empty
    directory behind in the library — most visibly under a flat naming template,
    where that folder is a level that did not exist before the run. Every failure
    and cancel report goes through _fail_or_cancel, so that is where the folder is
    swept.

    Driven against REAL directories rather than a mocked sweep, because the
    property that matters is how far the removal reaches (W3): this runs on every
    cancelled split download, and the levels above the book — the author folder,
    a series folder — belong to the user, not to this run."""

    def _report(self, tmp_path, message, *, split=True, stop_event=None, timed_out=False, leftover=None):
        processor = BookProcessor(asin="B0OURS", job_id=7, stop_event=stop_event)
        # An author level the user already had, holding nothing but this book.
        author_dir = tmp_path / "Bram Stoker"
        book_dir = author_dir / "Dracula"
        book_dir.mkdir(parents=True)
        processor.final_output_path = str(tmp_path / "Bram Stoker - Dracula.m4b")
        if split:
            processor.split_output_dir = str(book_dir)
            processor.split_part_paths = [
                str(book_dir / "Dracula - 01 - One.m4b"),
                str(book_dir / "Dracula - 02 - Two.m4b"),
            ]
        if leftover:
            (book_dir / leftover).write_bytes(b"x")
        if timed_out:
            processor._timed_out.set()
        with mock.patch.object(processor, "_update_db_on_failure") as fail:
            processor._fail_or_cancel(message)
        return book_dir, author_dir, fail

    def test_a_failed_chunk_encode_removes_the_planned_folder(self, tmp_path):
        book_dir, author_dir, fail = self._report(tmp_path, "A chapter chunk failed to encode.")
        assert not book_dir.exists()
        # ...and stops there. The author level was not this run's to remove, even
        # though the run's own folder was the only thing in it.
        assert author_dir.exists()
        fail.assert_called_once()

    def test_a_cancel_removes_it_too_without_touching_the_book(self, tmp_path):
        # The cancel path returns before any DB write, so the sweep runs first —
        # a cancelled split download must not leave a folder either.
        stop_event = Event()
        stop_event.set()
        book_dir, author_dir, fail = self._report(tmp_path, "A chapter chunk failed to encode.", stop_event=stop_event)
        assert not book_dir.exists()
        assert author_dir.exists()
        fail.assert_not_called()

    def test_a_post_timeout_echo_removes_it_too_without_reporting_again(self, tmp_path):
        book_dir, author_dir, fail = self._report(tmp_path, "A chapter chunk failed to encode.", timed_out=True)
        assert not book_dir.exists()
        assert author_dir.exists()
        fail.assert_not_called()

    def test_a_folder_still_holding_something_is_left_exactly_as_it_is(self, tmp_path):
        # The prune only ever rmdirs, so the previous download's chapter files —
        # which the promotion rollback deliberately spares — keep their folder.
        book_dir, _author_dir, _fail = self._report(
            tmp_path, "A chapter chunk failed to encode.", leftover="Dracula - 01 - One.m4b"
        )
        assert (book_dir / "Dracula - 01 - One.m4b").exists()

    def test_an_unsplit_run_has_no_folder_of_its_own_to_remove(self, tmp_path):
        book_dir, author_dir, fail = self._report(tmp_path, "MP3 encode failed.", split=False)
        assert book_dir.exists()
        assert author_dir.exists()
        fail.assert_called_once()


class TestUnsplitFinalizeStillClearsPartRows:
    """The mirror of the split write: a book re-downloaded as a single file must
    lose the part rows a previous split download left, or it stays 'split'
    forever — but that clear can never fail a finished download."""

    def _run(self, replace_side_effect=None):
        processor = BookProcessor(asin="B0OURS", job_id=1)
        processor.final_output_path = "/data/A/Dracula/A - Dracula.m4b"
        con = mock.MagicMock()
        con.__enter__.return_value = con
        con.execute.return_value.fetchone.return_value = {"runtime_min": 60, "filepath": None}
        # Kept on the test instance so a test can assert how the transaction ended.
        self.con = con
        with (
            mock.patch.object(processing_logic, "get_db_connection", return_value=con),
            mock.patch.object(processing_logic, "replace_book_files", side_effect=replace_side_effect) as replace,
            mock.patch.object(processing_logic, "_yield_progress"),
            mock.patch.object(processor, "_verify_output_file", return_value=(True, None)),
            mock.patch.object(processor, "_place_supplementary_pdf"),
            mock.patch.object(processor, "_place_sidecar_files"),
            mock.patch.object(processor, "_apply_file_timestamps"),
            mock.patch.object(processor, "_cleanup_stale_files"),
        ):
            processor._finalize_success(conversion_start_time=0, record_eta=False)
        downloaded = [c for c in con.execute.call_args_list if "status = 'DOWNLOADED'" in c.args[0]]
        return downloaded, replace

    def test_single_file_finalize_clears_the_rows(self):
        downloaded, replace = self._run()
        assert downloaded[0].args[1][0] == "/data/A/Dracula/A - Dracula.m4b"
        replace.assert_called_once_with("B0OURS", [], con=mock.ANY)

    def test_a_failing_clear_does_not_fail_the_download(self):
        downloaded, _replace = self._run(replace_side_effect=sqlite3.OperationalError("no such table: book_files"))
        assert len(downloaded) == 1

    def test_a_locked_database_fails_the_download_instead_of_committing_half_of_it(self):
        # W9: "no such table" and "database is locked" are both sqlite3 errors,
        # and swallowing the second committed the parent row — this `with` block
        # IS the transaction — while the book's OLD part rows survived. The
        # result is a single-file book that every reader counts as an N-part
        # split, whose parts the stale cleanup is about to delete. Only the
        # missing-table case is tolerated; a lock propagates.
        with pytest.raises(sqlite3.OperationalError):
            self._run(replace_side_effect=sqlite3.OperationalError("database is locked"))
        # The connection's context manager saw the exception, which is what makes
        # sqlite3 roll the parent UPDATE back with it.
        assert self.con.__exit__.call_args.args[0] is sqlite3.OperationalError


class TestSplitSidecarPlacement:
    """D9: sidecars keep the single-file-equivalent base inside the book's
    folder, and the cue sheet — which describes ONE file's internal layout — is
    skipped for a split book."""

    def _processor(self, tmp_path, split, folder="Dracula"):
        processor = BookProcessor(asin="B0OURS", job_id=1)
        out = tmp_path / "out" / folder
        out.mkdir(parents=True)
        processor.final_output_path = str(out / "Bram Stoker - Dracula.m4b")
        if split:
            processor.split_output_dir = str(out)
            processor.split_part_paths = [str(out / "Dracula - 1 - One.m4b")]
        processor.context = {
            "book_info": {"title": "Dracula", "authors": [{"name": "Bram Stoker"}]},
            "chapters": [{"title": "One", "start_offset_ms": 0}],
        }
        return processor, out

    def _settings(self):
        return {"conversion": {"create_cue_sheet": True, "save_metadata_json": True}}

    def test_unsplit_book_still_gets_a_cue_sheet(self, tmp_path):
        processor, out = self._processor(tmp_path, split=False)
        with mock.patch.object(processing_logic, "load_settings", return_value=self._settings()):
            processor._place_sidecar_files()
        assert (out / "Bram Stoker - Dracula.cue").exists()

    def test_split_book_skips_the_cue_sheet(self, tmp_path):
        processor, out = self._processor(tmp_path, split=True)
        with mock.patch.object(processing_logic, "load_settings", return_value=self._settings()):
            processor._place_sidecar_files()
        assert not (out / "Bram Stoker - Dracula.cue").exists()
        # The other sidecars still land, at the single-file-equivalent base.
        assert (out / "Bram Stoker - Dracula.metadata.json").exists()

    def test_flat_guard_moves_the_sidecar_base_into_the_book_folder(self, tmp_path):
        # With D5's guard the parts live one level below the single-file path, so
        # the sidecars follow them in rather than being stranded in /data.
        processor = BookProcessor(asin="B0OURS", job_id=1)
        root = tmp_path / "data"
        book_dir = root / "Bram Stoker - Dracula"
        book_dir.mkdir(parents=True)
        processor.final_output_path = str(root / "Bram Stoker - Dracula.m4b")
        processor.split_output_dir = str(book_dir)
        processor.split_part_paths = [str(book_dir / "Dracula - 1 - One.m4b")]
        assert processor._sidecar_base() == str(book_dir / "Bram Stoker - Dracula")

    def test_plain_split_keeps_todays_sidecar_base(self, tmp_path):
        processor, out = self._processor(tmp_path, split=True)
        assert processor._sidecar_base() == str(out / "Bram Stoker - Dracula")


class TestSplitPartsAreNotSidecars:
    """Watch-item: _existing_sidecar_suffixes prefix-matches on the base, and a
    part filename can legitimately start with that base. The exact-suffix
    membership test is the only thing keeping a chapter file from being swept,
    moved or stamped as if it were a sidecar."""

    def test_a_part_sharing_the_sidecar_prefix_is_not_a_sidecar(self, tmp_path):
        base = tmp_path / "Bram Stoker - Dracula"
        (tmp_path / "Bram Stoker - Dracula.jpg").write_bytes(b"img")
        # Worst case: a chapter template that begins with the very base name.
        (tmp_path / "Bram Stoker - Dracula - 01 - One.m4b").write_bytes(b"audio")
        (tmp_path / "Bram Stoker - Dracula - 02 - Two.m4b").write_bytes(b"audio")
        assert processing_logic._existing_sidecar_suffixes(str(base)) == [".jpg"]

    def test_timestamps_stamp_the_parts_once_and_the_sidecar_once(self, tmp_path):
        processor = BookProcessor(asin="B0OURS", job_id=1)
        base = tmp_path / "Bram Stoker - Dracula"
        parts = [tmp_path / "Bram Stoker - Dracula - 01 - One.m4b", tmp_path / "Bram Stoker - Dracula - 02 - Two.m4b"]
        for part in parts:
            part.write_bytes(b"audio")
        (tmp_path / "Bram Stoker - Dracula.jpg").write_bytes(b"img")
        processor.final_output_path = f"{base}.m4b"
        processor.split_output_dir = str(tmp_path)
        processor.split_part_paths = [str(p) for p in parts]
        processor.context = {"book_info": {"release_date": "2020-01-02"}}
        with (
            mock.patch.object(
                processing_logic,
                "load_settings",
                return_value={"conversion": {"file_timestamp_source": "release_date"}},
            ),
            mock.patch("os.utime") as utime,
        ):
            processor._apply_file_timestamps()
        stamped = sorted(c.args[0] for c in utime.call_args_list)
        assert stamped == sorted([str(p) for p in parts] + [str(tmp_path / "Bram Stoker - Dracula.jpg")])


# --- v0.24.0 Phase 4: lifecycle machinery -----------------------------------


class TestSplitRename:
    """#20 / D12: a metadata edit renames a SPLIT book by moving its whole set —
    folder, chapter files and sidecars — and rewrites `filepath` and the part
    rows together. Part filenames are deliberately kept: they were rendered from
    per-chapter titles, which nothing persists."""

    PARTS = ("Dracula - 1 - One.m4b", "Dracula - 2 - Two.m4b")
    SIDECARS = ("Bram Stoker - Dracula.jpg", "Bram Stoker - Dracula.metadata.json")

    def _library(self, tmp_path, *, parts=PARTS, sidecars=SIDECARS):
        """A split book on disk: a folder of chapter files plus its sidecars at
        the single-file-equivalent base."""
        folder = tmp_path / "old" / "Dracula"
        folder.mkdir(parents=True)
        for name in parts:
            (folder / name).write_bytes(b"audio")
        for name in sidecars:
            (folder / name).write_bytes(b"sidecar")
        return folder

    def _row(self, folder, **over):
        row = {
            "author": "Bram Stoker",
            "title": "Dracula",
            "narrator": "N",
            "publisher": "P",
            "custom_title": None,
            "custom_author": None,
            "filepath": str(folder),
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
        folder,
        target,
        *,
        part_names=PARTS,
        tracked=(),
        tracked_parts=(),
        move_error=None,
        row_over=None,
        update_error=None,
        shared_error=None,
    ):
        """Drive the rename against the real files under `folder`, with only the
        database and the naming engine faked. Returns (result, fake connection)."""
        part_paths = [str(folder / name) for name in part_names]
        row = self._row(folder, **(row_over or {}))
        con = _FakeDb(
            book_row=row,
            part_rows=part_paths,
            tracked=[("B0OURS", str(folder)), *tracked],
            tracked_parts=[("B0OURS", path) for path in part_paths] + list(tracked_parts),
            update_error=update_error,
        )

        patches = [
            mock.patch.object(
                processing_logic, "load_settings", return_value={"naming": {"apply_custom_to_filenames": True}}
            ),
            mock.patch.object(processing_logic, "get_db_connection", return_value=con),
            mock.patch.object(processing_logic, "build_base_output_path", return_value=target),
        ]
        if move_error is not None:
            patches.append(mock.patch.object(processing_logic.shutil, "move", side_effect=move_error))
        if shared_error is not None:
            # The #20 shared-base check hitting a briefly locked library.db.
            patches.append(mock.patch.object(processing_logic, "_output_base_is_shared", side_effect=shared_error))
        with contextlib.ExitStack() as stack:
            for patch in patches:
                stack.enter_context(patch)
            result = processing_logic.rename_book_to_match_metadata("B0OURS")
        return result, con

    def test_parts_and_sidecars_move_into_the_new_folder(self, tmp_path):
        folder = self._library(tmp_path)
        new_folder = tmp_path / "new" / "Nosferatu"
        result, con = self._run(folder, str(new_folder / "Bram Stoker - Nosferatu.m4b"))

        assert result == str(new_folder)
        # Every chapter file moved, keeping its own name (see the class docstring).
        assert sorted(p.name for p in new_folder.iterdir()) == [
            "Bram Stoker - Nosferatu.jpg",
            "Bram Stoker - Nosferatu.metadata.json",
            *sorted(self.PARTS),
        ]
        assert list(folder.iterdir()) == []

    def test_filepath_and_part_rows_are_written_in_one_transaction(self, tmp_path):
        folder = self._library(tmp_path)
        new_folder = tmp_path / "new" / "Nosferatu"
        _result, con = self._run(folder, str(new_folder / "Bram Stoker - Nosferatu.m4b"))

        # The folder goes in the parent row (D3)...
        assert con.updates() == [(str(new_folder), 0, "B0OURS")]
        # ...the parts in the child rows, in playback order, on the SAME
        # connection, and the whole thing commits exactly once.
        assert con.inserted_parts() == [
            ("B0OURS", 0, str(new_folder / self.PARTS[0])),
            ("B0OURS", 1, str(new_folder / self.PARTS[1])),
        ]
        assert con.commits == 1

    def test_unchanged_name_is_a_noop(self, tmp_path):
        folder = self._library(tmp_path)
        result, con = self._run(folder, str(folder / "Bram Stoker - Dracula.m4b"))
        assert result is None
        assert con.updates() == []
        assert sorted(p.name for p in folder.iterdir()) == sorted([*self.PARTS, *self.SIDECARS])

    def test_stem_only_change_moves_the_sidecars_and_leaves_the_parts(self, tmp_path):
        # A naming template whose FOLDER level doesn't depend on the title: the
        # book stays where it is and only its sidecar base moves.
        folder = self._library(tmp_path)
        result, con = self._run(folder, str(folder / "Bram Stoker - Nosferatu.m4b"))
        assert result == str(folder)
        assert (folder / "Bram Stoker - Nosferatu.jpg").exists()
        assert not (folder / "Bram Stoker - Dracula.jpg").exists()
        # The chapter files never moved, so their rows still name the same paths.
        assert [row[2] for row in con.inserted_parts()] == [str(folder / name) for name in self.PARTS]

    def test_missing_folder_is_a_noop(self, tmp_path):
        folder = tmp_path / "gone"
        result, con = self._run(folder, str(tmp_path / "new" / "New.m4b"))
        assert result is None
        assert con.updates() == []

    def test_no_parts_on_disk_is_a_noop(self, tmp_path):
        folder = self._library(tmp_path, parts=())
        result, con = self._run(folder, str(tmp_path / "new" / "New.m4b"))
        assert result is None
        assert con.updates() == []

    def test_a_failed_move_rolls_back_and_writes_nothing(self, tmp_path):
        folder = self._library(tmp_path)
        new_folder = tmp_path / "new" / "Nosferatu"
        moves = []

        def move(src, dst):
            if len(moves) == 1:
                raise OSError("no space left on device")
            moves.append((src, dst))
            shutil.move(src, dst)

        result, con = self._run(folder, str(new_folder / "Bram Stoker - Nosferatu.m4b"), move_error=move)
        assert result is None
        assert con.updates() == []
        # The part that had already moved is back where it started.
        assert sorted(p.name for p in folder.iterdir()) == sorted([*self.PARTS, *self.SIDECARS])

    def test_ambiguous_sidecar_base_leaves_the_sidecars_alone(self, tmp_path):
        # Two books' sidecars in one folder: which base is ours is unknowable, so
        # nothing is moved rather than moving the other book's cover.
        folder = self._library(tmp_path, sidecars=("Bram Stoker - Dracula.jpg", "Someone Else - Other.jpg"))
        new_folder = tmp_path / "new" / "Nosferatu"
        result, _con = self._run(folder, str(new_folder / "Bram Stoker - Nosferatu.m4b"))
        assert result == str(new_folder)
        assert sorted(p.name for p in folder.iterdir()) == ["Bram Stoker - Dracula.jpg", "Someone Else - Other.jpg"]

    def test_an_external_library_managers_cover_is_not_taken_along(self, tmp_path):
        # The same defect the stale sweep had, in the move direction: the folder's
        # only sidecar-shaped file is an external library manager's "cover.jpg"
        # (its bare "metadata.json" matches no suffix at all), which reads back as
        # the unambiguous base "cover". The chapter files still move; the files
        # this app never wrote stay exactly where their owner put them.
        folder = self._library(tmp_path, sidecars=("cover.jpg", "metadata.json"))
        new_folder = tmp_path / "new" / "Nosferatu"
        result, _con = self._run(folder, str(new_folder / "Bram Stoker - Nosferatu.m4b"))

        assert result == str(new_folder)
        assert sorted(p.name for p in folder.iterdir()) == ["cover.jpg", "metadata.json"]
        assert sorted(p.name for p in new_folder.iterdir()) == sorted(self.PARTS)

    def test_a_cover_only_base_still_moves_when_the_stem_is_unchanged(self, tmp_path):
        # The name branch of the corroboration: nothing but a cover image at the
        # base, but it is spelled exactly like the stem this rename renders, so
        # it is this book's and follows the parts into the new folder.
        folder = self._library(tmp_path, sidecars=("Bram Stoker - Dracula.jpg",))
        new_folder = tmp_path / "new" / "Dracula"
        result, _con = self._run(folder, str(new_folder / "Bram Stoker - Dracula.m4b"))

        assert result == str(new_folder)
        assert sorted(p.name for p in new_folder.iterdir()) == sorted(["Bram Stoker - Dracula.jpg", *self.PARTS])
        assert list(folder.iterdir()) == []

    def test_shared_sidecar_base_is_left_where_it_is(self, tmp_path):
        # #20 for the split shape: another book's row still points at the old
        # base, so its cover/metadata are not ours to walk off with.
        folder = self._library(tmp_path)
        new_folder = tmp_path / "new" / "Nosferatu"
        result, _con = self._run(
            folder,
            str(new_folder / "Bram Stoker - Nosferatu.m4b"),
            tracked=[("B0OTHER", str(folder / "Bram Stoker - Dracula.mp3"))],
        )
        assert result == str(new_folder)
        assert (folder / "Bram Stoker - Dracula.jpg").exists()
        assert not (new_folder / "Bram Stoker - Nosferatu.jpg").exists()

    def test_a_folder_another_book_already_owns_gets_the_asin_suffix(self, tmp_path):
        # D12 at rename time: the target folder's part paths are claimed by
        # another split book, so this book's folder is suffixed instead of its
        # files being written over that book's.
        folder = self._library(tmp_path)
        new_folder = tmp_path / "new" / "Nosferatu"
        result, con = self._run(
            folder,
            str(new_folder / "Bram Stoker - Nosferatu.m4b"),
            tracked_parts=[("B0OTHER", str(new_folder / self.PARTS[0]))],
        )
        assert result == f"{new_folder}_B0OURS"
        assert con.updates() == [(f"{new_folder}_B0OURS", 1, "B0OURS")]
        assert (tmp_path / "new" / "Nosferatu_B0OURS" / self.PARTS[0]).exists()

    def test_a_failed_database_write_puts_the_whole_set_back(self, tmp_path):
        # W2: the parts and sidecars move first and the two rows are rewritten
        # afterwards. A library.db locked for that instant used to leave every
        # file in the new folder while `filepath` and `book_files` still named
        # the old one — the book reads as MISSING on the next Verify with N
        # intact chapter files one directory away, and nothing ever reconciles
        # it. The move is undone instead.
        folder = self._library(tmp_path)
        new_folder = tmp_path / "new" / "Nosferatu"
        result, con = self._run(
            folder,
            str(new_folder / "Bram Stoker - Nosferatu.m4b"),
            update_error=sqlite3.OperationalError("database is locked"),
        )
        assert result is None
        # Every part and every sidecar is back under its original name...
        assert sorted(p.name for p in folder.iterdir()) == sorted([*self.PARTS, *self.SIDECARS])
        assert list(new_folder.iterdir()) == []
        # ...and nothing was written, so the rows still point at the old folder.
        assert con.updates() == []
        assert con.commits == 0

    def test_a_raising_shared_base_check_moves_nothing_at_all(self, tmp_path):
        # W2, split shape: the #20 check sat BETWEEN the file moves and the row
        # write, and its exception is caught by the function's outer handler —
        # which logs and returns without undoing the moves, stranding the whole
        # set in the new folder while `filepath` and `book_files` still name the
        # old one. Asked before the first move, nothing has moved to strand.
        folder = self._library(tmp_path)
        new_folder = tmp_path / "new" / "Nosferatu"
        result, con = self._run(
            folder,
            str(new_folder / "Bram Stoker - Nosferatu.m4b"),
            shared_error=sqlite3.OperationalError("database is locked"),
        )
        assert result is None
        assert sorted(p.name for p in folder.iterdir()) == sorted([*self.PARTS, *self.SIDECARS])
        assert not new_folder.exists()
        assert con.updates() == []
        assert con.commits == 0

    def test_a_folder_an_in_flight_download_claimed_gets_the_asin_suffix(self, tmp_path):
        # W3, rename side: a DOWNLOAD reserves its split folder at PREPARE time,
        # long before any part file or `book_files` row exists, so neither the
        # on-disk check nor the ownership map can see it. The two allocators
        # only meet if they spell the claim the same way — the FOLDER path.
        folder = self._library(tmp_path)
        new_folder = tmp_path / "new" / "Nosferatu"
        processing_logic._reserved_output_paths.add(str(new_folder))
        result, con = self._run(folder, str(new_folder / "Bram Stoker - Nosferatu.m4b"))
        assert result == f"{new_folder}_B0OURS"
        assert con.updates() == [(f"{new_folder}_B0OURS", 1, "B0OURS")]
        assert (tmp_path / "new" / "Nosferatu_B0OURS" / self.PARTS[0]).exists()
        # The in-flight book's own claim is untouched — we release only ours.
        assert processing_logic._reserved_output_paths == {str(new_folder)}

    def test_the_chosen_folder_is_claimed_while_the_files_move(self, tmp_path):
        # The other direction of the same currency: the folder claim has to be
        # visible to a DOWNLOAD's folder walk (`_first_free_split_folder`) for
        # the whole time the files are in motion, so the assertion happens
        # INSIDE the move — checking afterwards cannot tell "claimed then
        # released" from "never claimed".
        folder = self._library(tmp_path)
        new_folder = tmp_path / "new" / "Nosferatu"
        held = []
        real_move = shutil.move  # bound before _run patches the module attribute

        def move(src, dst):
            held.append(str(new_folder) in processing_logic._reserved_output_paths)
            real_move(src, dst)

        result, _con = self._run(folder, str(new_folder / "Bram Stoker - Nosferatu.m4b"), move_error=move)
        assert result == str(new_folder)
        assert held and all(held)
        # ...and released once the move and the database write are done.
        assert processing_logic._reserved_output_paths == set()

    def test_a_part_sharing_the_sidecar_base_is_moved_once_as_a_part(self, tmp_path):
        # Watch-item: _existing_sidecar_suffixes prefix-matches, and this chapter
        # template produces names that begin with the very sidecar base. A part
        # must move as a PART (keeping its name) and never be swept as a sidecar.
        parts = ("Bram Stoker - Dracula - 01 - One.m4b", "Bram Stoker - Dracula - 02 - Two.m4b")
        folder = self._library(tmp_path, parts=parts)
        new_folder = tmp_path / "new" / "Nosferatu"
        result, _con = self._run(folder, str(new_folder / "Bram Stoker - Nosferatu.m4b"), part_names=parts)
        assert result == str(new_folder)
        assert sorted(p.name for p in new_folder.iterdir()) == sorted(
            ["Bram Stoker - Nosferatu.jpg", "Bram Stoker - Nosferatu.metadata.json", *parts]
        )


class TestSplitCollisionFolders:
    """The same-title x2 data-loss window (Phase 2's deferred W3): two different
    books with the same author+title, both split, must not write their part sets
    into one folder. D12 disambiguates on the FOLDER, because part filenames
    carry none of the book base and would collide file-for-file."""

    def _plan(self, asin, *, reserved=None, tracked=(), tracked_parts=(), naming=None, book_row=BOOK_ROW):
        """Run PREPARE's path reservation and split planning for one book."""
        processor, submitted, _makedirs = _run_split_prepare_with_db(
            asin=asin, tracked=tracked, tracked_parts=tracked_parts, naming=naming, book_row=book_row
        )
        return processor

    def test_two_in_flight_books_get_separate_folders(self):
        first = self._plan("B0AAAA")
        second = self._plan("B0BBBB")
        assert first.split_output_dir == "/data/Bram Stoker/Dracula"
        assert second.split_output_dir == "/data/Bram Stoker/Dracula_B0BBBB"
        # ...which is the whole point: no part path is shared.
        assert not set(first.split_part_paths) & set(second.split_part_paths)
        assert second.is_duplicate is True

    def test_the_second_books_sidecars_follow_its_own_folder(self):
        self._plan("B0AAAA")
        second = self._plan("B0BBBB")
        # The ASIN moved from the filename onto the folder, so the sidecar base
        # inside that folder is the plain single-file-equivalent name again.
        assert second._sidecar_base() == "/data/Bram Stoker/Dracula_B0BBBB/Bram Stoker - Dracula"

    def test_a_previously_downloaded_split_book_is_seen_too(self):
        # The sequential case, which no reservation can see: book A finished long
        # ago and left no file at the single-file base — only its part rows.
        second = self._plan(
            "B0BBBB",
            tracked=[("B0AAAA", "/data/Bram Stoker/Dracula")],
            tracked_parts=[("B0AAAA", "/data/Bram Stoker/Dracula/Dracula - 1 - One.m4b")],
        )
        assert second.split_output_dir == "/data/Bram Stoker/Dracula_B0BBBB"
        assert second.is_duplicate is True

    def test_our_own_previous_part_set_is_not_a_collision(self):
        # A re-download of the same book overwrites its own parts in place.
        processor = self._plan(
            "B0OURS",
            tracked=[("B0OURS", "/data/Bram Stoker/Dracula")],
            tracked_parts=[("B0OURS", "/data/Bram Stoker/Dracula/Dracula - 1 - One.m4b")],
        )
        assert processor.split_output_dir == "/data/Bram Stoker/Dracula"
        assert processor.is_duplicate is False

    def test_another_books_parts_elsewhere_in_a_shared_folder_are_not_a_collision(self):
        # A naming template that gives every book of an author one folder is
        # normal; two DIFFERENT titles splitting into it collide with nothing, so
        # no spurious suffix appears.
        processor = self._plan(
            "B0OURS",
            tracked_parts=[("B0OTHER", "/data/Bram Stoker/Dracula/Other Book - 1 - One.m4b")],
        )
        assert processor.split_output_dir == "/data/Bram Stoker/Dracula"
        assert processor.is_duplicate is False

    """W3: the folder itself is the reservation currency, because two books can
    render DIFFERENT single-file bases into the SAME folder — and because the
    rename allocator spells a collision on the folder while the download
    allocator spells it on the filename, so neither could ever see the other's
    filename-shaped claim."""

    def test_two_titles_sharing_a_folder_but_not_a_base_still_separate(self):
        # The failure scenario: "{author}/{title}/{title} - {narrator}" renders a
        # different filename base per edition (so neither book's filename
        # reservation sees the other) and the same folder for both — while the
        # default chapter template renders identical part names. Both books used
        # to pick that folder and the second one's promotion overwrote the first
        # book's chapter files.
        naming = {"template": "{author}/{title}/{title} - {narrator}"}
        first = self._plan("B0AAAA", naming=naming, book_row={**BOOK_ROW, "narrator": "Simon Vance"})
        second = self._plan("B0BBBB", naming=naming, book_row={**BOOK_ROW, "narrator": "Tim Curry"})
        # Neither reservation collided on the filename base...
        assert first.final_output_path == "/data/Bram Stoker/Dracula/Dracula - Simon Vance.m4b"
        assert second.final_output_path == "/data/Bram Stoker/Dracula/Dracula - Tim Curry.m4b"
        # ...and the folder claim is what keeps the part sets apart.
        assert first.split_output_dir == "/data/Bram Stoker/Dracula"
        assert second.split_output_dir == "/data/Bram Stoker/Dracula_B0BBBB"
        assert not set(first.split_part_paths) & set(second.split_part_paths)
        assert second.is_duplicate is True

    def test_the_chosen_folder_is_registered_as_a_reservation(self):
        # The claim a concurrent rename (or download) has to be able to see.
        processor = self._plan("B0AAAA")
        assert processor.split_output_dir in processing_logic._reserved_output_paths
        assert processor.split_folder_reservation == processor.split_output_dir

    def test_a_folder_claimed_by_a_rename_is_not_taken(self):
        # The mirror of TestSplitRename's in-flight-download case: a metadata
        # edit holds its target folder across the move, and the download walks
        # past it instead of writing its parts into the middle of that move.
        processing_logic._reserved_output_paths.add("/data/Bram Stoker/Dracula")
        processor = self._plan("B0AAAA")
        assert processor.split_output_dir == "/data/Bram Stoker/Dracula_B0AAAA"
        assert processor.is_duplicate is True

    def test_a_flat_template_book_does_not_collide_with_its_own_reservation(self):
        # The control for the two checks above: D5's guard names the subfolder
        # from the rendered single-file base, so the folder IS this book's own
        # filename reservation. Its own claim must not push it to a suffix.
        processor = self._plan("B0AAAA", naming={"template": "{author} - {title}"})
        assert processor.final_output_path == "/data/Bram Stoker - Dracula.m4b"
        assert processor.split_output_dir == "/data/Bram Stoker - Dracula"
        assert processor.is_duplicate is False

    def test_the_folder_claim_is_released_when_the_run_finishes(self, tmp_path):
        # Reservations are process-global, so a claim that is never released
        # would send every later re-download of this book to a suffixed folder
        # for the life of the container. `run` discards exactly what was added.
        processor = self._plan("B0AAAA")
        assert processing_logic._reserved_output_paths
        with (
            mock.patch.object(processing_logic, "TEMP_DIR", str(tmp_path)),
            mock.patch.object(processor, "_completion_timeout", return_value=0),
            mock.patch.object(processing_logic.task_runner, "submit_task"),
            mock.patch.object(processor, "_update_db_on_failure"),
        ):
            processor.run()
        assert processing_logic._reserved_output_paths == set()


def _run_split_prepare_with_db(asin="B0OURS", tracked=(), tracked_parts=(), naming=None, book_row=BOOK_ROW):
    """
    _run_split_prepare with a database that can answer the ownership questions
    the split-folder allocator asks (which books track which paths), and a
    filesystem where nothing exists.
    """
    processor = BookProcessor(asin=asin, job_id=1)
    processor.download_complete_event = Event()
    con = _FakeDb(book_row=book_row, tracked=list(tracked), tracked_parts=list(tracked_parts))
    context = {
        "audio_file": "/tmp/x/master_intermediate.m4b",
        "chapters": SPLIT_CHAPTERS,
        "split_output": True,
        "split_encode_mode": "aac",
    }
    submitted = []
    with (
        mock.patch.object(
            processing_logic,
            "load_settings",
            return_value={"naming": naming or {}, "conversion": {"output_format": "m4b"}},
        ),
        mock.patch.object(processing_logic, "get_db_connection", return_value=con),
        mock.patch.object(processing_logic, "prepare_book_assets", return_value=(context, None)),
        mock.patch("os.path.exists", return_value=False),
        mock.patch("os.makedirs") as makedirs,
        mock.patch.object(processing_logic, "_yield_progress"),
        mock.patch.object(processing_logic.task_runner, "submit_task", side_effect=submitted.append),
    ):
        processor._prepare_and_spawn_encode_tasks()
    return processor, submitted, makedirs


class TestCleanupStaleSets(TestCleanupStaleFiles):
    """#30 / Phase 4: the previous download is a SET — the old `book_files` rows
    when it was split, the single filepath otherwise — so every shape transition
    cleans up after itself, and nothing is deleted that another book claims.

    Inherits the parent's `_run` harness (and, deliberately, its whole suite of
    single-file cases, which must keep passing unchanged)."""

    OLD_FOLDER = "/data/Author/Old Title"
    OLD_PARTS = [f"{OLD_FOLDER}/Old Title - 1 - One.m4b", f"{OLD_FOLDER}/Old Title - 2 - Two.m4b"]

    def test_split_to_single_removes_every_old_part(self):
        # The mirror of the single -> split transition: the book is now one file
        # somewhere else, and the whole old part set is stale. The metadata JSON
        # is what identifies the old base as this app's own work — the book was
        # renamed, so the stem no longer matches the one this run rendered.
        old_base = f"{self.OLD_FOLDER}/Old Title"
        removed, cleanup_dirs = self._run(
            self.OLD_FOLDER,
            previous_parts=self.OLD_PARTS,
            param=True,
            present={*self.OLD_PARTS, old_base + ".jpg", old_base + ".metadata.json"},
        )
        assert removed == {*self.OLD_PARTS, old_base + ".jpg", old_base + ".metadata.json"}
        cleanup_dirs.assert_any_call(self.OLD_FOLDER)

    def test_the_tracked_folder_itself_is_never_unlinked(self):
        # `previous_path` is a DIRECTORY for a split book; only its parts are files.
        removed, _cleanup_dirs = self._run(
            self.OLD_FOLDER,
            previous_parts=self.OLD_PARTS,
            param=True,
            present={*self.OLD_PARTS, self.OLD_FOLDER},
        )
        assert self.OLD_FOLDER not in removed

    def test_split_to_split_with_new_names_removes_the_old_parts(self):
        # The gap Phase 3's smoke test found: a re-download whose chapter naming
        # (or format) changed writes new parts beside the old ones, and nothing
        # tracked the old set any more.
        new_parts = [f"{self.OLD_FOLDER}/Old Title - 01 - One.mp3", f"{self.OLD_FOLDER}/Old Title - 02 - Two.mp3"]
        removed, _cleanup_dirs = self._run(
            self.OLD_FOLDER,
            previous_parts=self.OLD_PARTS,
            new_path=f"{self.OLD_FOLDER}/Old Title.mp3",
            param=True,
            split_parts=new_parts,
            split_dir=self.OLD_FOLDER,
            present={*self.OLD_PARTS, *new_parts},
        )
        assert removed == set(self.OLD_PARTS)

    def test_split_to_split_in_place_removes_nothing(self):
        # Same names, same folder: every old part IS a new part, overwritten in
        # place, so there is nothing stale — and the sidecars are this run's own.
        removed, cleanup_dirs = self._run(
            self.OLD_FOLDER,
            previous_parts=self.OLD_PARTS,
            new_path=f"{self.OLD_FOLDER}/Old Title.m4b",
            param=True,
            split_parts=self.OLD_PARTS,
            split_dir=self.OLD_FOLDER,
            present={*self.OLD_PARTS, f"{self.OLD_FOLDER}/Old Title.jpg"},
        )
        assert removed == set()
        cleanup_dirs.assert_not_called()

    def test_single_to_split_in_place_removes_the_old_cue_sheet(self):
        # W10: converting a book to chapter files under unchanged naming keeps
        # the same base, so `old_base` IS this run's own sidecar base and the
        # sweep is correctly skipped — the cover and metadata there are the ones
        # just written. The cue sheet is the exception: a split book never gets
        # one (D9), so the one on disk describes the single-file timeline that
        # has just been deleted, and nothing else would ever remove it.
        folder = "/data/Author/Dracula"
        old_single = f"{folder}/Dracula.m4b"
        base = f"{folder}/Dracula"
        new_parts = [f"{folder}/Dracula - 1 - One.m4b", f"{folder}/Dracula - 2 - Two.m4b"]
        removed, _cleanup_dirs = self._run(
            old_single,
            new_path=old_single,  # the reserved single-file-equivalent path
            param=True,
            split_parts=new_parts,
            split_dir=folder,
            split_sidecar_base=base,
            present={old_single, *new_parts, base + ".cue", base + ".jpg", base + ".metadata.json"},
        )
        assert removed == {old_single, base + ".cue"}
        # This run's own cover and metadata are untouched — the reason the sweep
        # is skipped at all, and why the cue is named rather than inferred.
        assert base + ".jpg" not in removed
        assert base + ".metadata.json" not in removed

    def test_an_unsplit_re_download_in_place_keeps_its_cue_sheet(self):
        # The control: an ordinary format change writes a new cue at the same
        # base, so removing "the old one" would delete this run's own output.
        folder = "/data/Author/Dracula"
        old_single = f"{folder}/Dracula.mp3"
        base = f"{folder}/Dracula"
        removed, _cleanup_dirs = self._run(
            old_single,
            new_path=f"{base}.m4b",
            param=True,
            present={old_single, base + ".cue", base + ".jpg"},
        )
        assert removed == {old_single}

    def test_the_old_folders_sidecars_are_swept_with_its_parts(self):
        # The old sidecar base is read back off the folder (nothing records it),
        # and only when the new base is somewhere else.
        old_base = f"{self.OLD_FOLDER}/Old Title"
        removed, _cleanup_dirs = self._run(
            self.OLD_FOLDER,
            previous_parts=self.OLD_PARTS,
            param=True,
            present={*self.OLD_PARTS, old_base + ".jpg", old_base + ".metadata.json"},
        )
        assert removed == {*self.OLD_PARTS, old_base + ".jpg", old_base + ".metadata.json"}

    def test_an_external_library_managers_files_are_never_swept(self):
        # The live Phase 8 defect: Audiobookshelf writes "cover.jpg" and a bare
        # "metadata.json" into every book folder it manages. The bare JSON matches
        # no sidecar suffix, so the folder reads back as the single unambiguous
        # base "cover" — and the sweep deleted another program's cover image on
        # every re-download. Nothing in that folder is ours to delete but the
        # parts we wrote.
        removed, _cleanup_dirs = self._run(
            self.OLD_FOLDER,
            previous_parts=self.OLD_PARTS,
            param=True,
            present={*self.OLD_PARTS, f"{self.OLD_FOLDER}/cover.jpg", f"{self.OLD_FOLDER}/metadata.json"},
        )
        assert removed == set(self.OLD_PARTS)

    def test_the_rendered_stem_corroborates_a_cover_only_base(self):
        # The other corroboration branch, and the common case: only the FOLDER
        # level of the naming template changed, so the old base is spelled
        # exactly like the stem this run just rendered for itself. A lone cover
        # image at that base is ours and goes with the parts.
        old_folder = "/data/Old Author/Title"
        old_parts = [f"{old_folder}/Title - 1 - One.m4b"]
        removed, _cleanup_dirs = self._run(
            old_folder,
            previous_parts=old_parts,
            param=True,
            present={*old_parts, f"{old_folder}/Title.jpg"},
        )
        assert removed == {*old_parts, f"{old_folder}/Title.jpg"}

    def test_a_renamed_books_uncorroborated_cover_is_left_behind(self):
        # The accepted cost of the guard above: the book was renamed (so the old
        # stem matches nothing this run rendered) and the only sidecar left is a
        # cover image, which any library manager could equally have written. The
        # cover is stranded rather than risking someone else's file.
        removed, _cleanup_dirs = self._run(
            self.OLD_FOLDER,
            previous_parts=self.OLD_PARTS,
            param=True,
            present={*self.OLD_PARTS, f"{self.OLD_FOLDER}/Old Title.jpg"},
        )
        assert removed == set(self.OLD_PARTS)

    def test_an_ambiguous_old_folder_keeps_its_sidecars(self):
        # Two books' sidecars in the old folder: the stem is unknowable, so the
        # parts go and the sidecars stay rather than deleting the wrong book's.
        removed, _cleanup_dirs = self._run(
            self.OLD_FOLDER,
            previous_parts=self.OLD_PARTS,
            param=True,
            present={*self.OLD_PARTS, f"{self.OLD_FOLDER}/Old Title.jpg", f"{self.OLD_FOLDER}/Other Book.jpg"},
        )
        assert removed == set(self.OLD_PARTS)

    def test_a_part_of_another_book_is_never_deleted(self):
        # #30: co-ownership is checked BEFORE the unlink. Here the "stale" path is
        # one of another split book's chapter files.
        shared = self.OLD_PARTS[0]
        removed, _cleanup_dirs = self._run(
            self.OLD_FOLDER,
            previous_parts=self.OLD_PARTS,
            param=True,
            present=set(self.OLD_PARTS),
            other_parts=(("B0OTHER", shared),),
        )
        assert shared not in removed
        assert self.OLD_PARTS[1] in removed

    def test_a_file_another_row_still_tracks_is_never_deleted(self):
        # The single-file half of #30: two audiobooks rows at the identical path
        # (reachable in pre-collision-guard libraries, and via manual upload).
        removed, cleanup_dirs = self._run(
            self.OLD,
            param=True,
            present={self.OLD},
            other_books=(("B0OTHER", self.OLD),),
        )
        assert removed == set()
        cleanup_dirs.assert_not_called()

    def test_a_file_only_this_book_tracks_is_still_deleted(self):
        # The control for the veto above.
        removed, _cleanup_dirs = self._run(self.OLD, param=True, present={self.OLD})
        assert removed == {self.OLD}

    def test_a_tracked_folder_is_never_unlinked(self, tmp_path):
        # A split book's tracked path is a DIRECTORY. With its part rows gone (a
        # hand-restored library.db) that folder is all the cleanup is handed, and
        # it must not be passed to os.remove nor let its neighbours be swept as
        # if it were an audio file. A real directory here (not the fake /data
        # paths the other cases use), so os.path.isdir has something true to say.
        folder = tmp_path / "Dracula"
        folder.mkdir()
        (folder / "Dracula - 1 - One.m4b").write_bytes(b"audio")

        processor = BookProcessor(asin="B0OURS", job_id=1)
        processor.final_output_path = str(tmp_path / "new" / "Dracula.m4b")
        processor.cleanup_stale_files = True

        with (
            mock.patch.object(processing_logic, "load_settings", return_value={}),
            mock.patch("os.remove") as remove,
            mock.patch.object(processing_logic, "_cleanup_empty_dirs") as cleanup_dirs,
        ):
            processor._cleanup_stale_files(str(folder))

        remove.assert_not_called()
        cleanup_dirs.assert_not_called()

    def test_a_split_part_at_the_old_base_is_not_swept_as_a_sidecar(self):
        # Watch-item: the sweep matches on the old base by prefix, and a chapter
        # file can legitimately start with it. Only exact sidecar suffixes go.
        old_base = f"{self.OLD_FOLDER}/Old Title"
        parts = [f"{old_base} - 01 - One.m4b", f"{old_base} - 02 - Two.m4b"]
        removed, _cleanup_dirs = self._run(
            self.OLD,
            param=True,
            present={self.OLD, old_base + ".cue", *parts},
        )
        assert removed == {self.OLD, old_base + ".cue"}
        assert not removed & set(parts)


class TestPostSuccessIsNonFatal:
    """#29: everything after the verified DOWNLOADED write is housekeeping. A
    raise there used to leave the completion event unset, `run` waiting out its
    two-hour timeout, and the book finally recorded ERROR with its files sitting
    perfectly well on disk — and the previous download already deleted."""

    def _finalize(self, failing_step, error=RuntimeError("boom"), split=False):
        processor = BookProcessor(asin="B0OURS", job_id=1)
        processor.final_output_path = "/data/A/Title/Title.m4b"
        if split:
            processor.split_output_dir = "/data/A/Title"
            processor.split_part_paths = ["/data/A/Title/Title - 1 - One.m4b"]

        con = mock.MagicMock()
        con.__enter__.return_value = con
        con.execute.return_value.fetchone.return_value = {"filepath": "/data/A/Old/Old.m4b", "runtime_min": 60}

        steps = {
            "_place_supplementary_pdf": None,
            "_place_sidecar_files": None,
            "_apply_file_timestamps": None,
            "_cleanup_stale_files": None,
        }
        with (
            mock.patch.object(processing_logic, "get_db_connection", return_value=con),
            mock.patch.object(processing_logic, "replace_book_files"),
            mock.patch.object(processing_logic, "_yield_progress") as progress,
            mock.patch.object(processor, "_verify_output_file", return_value=(True, None)),
            mock.patch.object(processor, "_update_db_on_failure") as fail,
            mock.patch.object(processor, "_promote_split_parts", return_value=True),
            mock.patch("os.path.exists", return_value=False),
        ):
            for name in steps:
                patcher = mock.patch.object(processor, name, side_effect=error if name == failing_step else None)
                steps[name] = patcher.start()
            try:
                if split:
                    processor._finalize_split()
                else:
                    processor._finalize_success(conversion_start_time=0, record_eta=False)
            finally:
                mock.patch.stopall()
        downloaded = [c for c in con.execute.call_args_list if "status = 'DOWNLOADED'" in c.args[0]]
        return {"steps": steps, "fail": fail, "progress": progress, "downloaded": downloaded, "processor": processor}

    @pytest.mark.parametrize(
        "failing_step",
        ["_place_supplementary_pdf", "_place_sidecar_files", "_apply_file_timestamps", "_cleanup_stale_files"],
    )
    def test_a_raising_step_never_fails_the_book(self, failing_step):
        r = self._finalize(failing_step)
        assert len(r["downloaded"]) == 1  # the book is recorded DOWNLOADED...
        r["fail"].assert_not_called()  # ...and never flipped to ERROR
        assert 100 in [c.args[2] for c in r["progress"].call_args_list]

    def test_the_later_steps_still_run(self):
        # Non-fatal means "log and continue", not "abandon the rest": a failed PDF
        # must not cost the book its sidecars, timestamps or stale-file cleanup.
        r = self._finalize("_place_supplementary_pdf")
        for name in ("_place_sidecar_files", "_apply_file_timestamps", "_cleanup_stale_files"):
            r["steps"][name].assert_called_once()

    def test_a_sqlite_error_in_the_cleanup_is_survived(self):
        # #29's actual trigger: an unguarded con.execute inside the cleanup's
        # ownership checks, running after the DOWNLOADED write.
        r = self._finalize("_cleanup_stale_files", error=sqlite3.OperationalError("database is locked"))
        assert len(r["downloaded"]) == 1
        r["fail"].assert_not_called()

    def test_the_split_shape_is_covered_too(self):
        # _finalize_split wraps the same body in a try that reports failures — the
        # housekeeping must be non-fatal before it ever gets there.
        r = self._finalize("_place_sidecar_files", split=True)
        assert len(r["downloaded"]) == 1
        r["fail"].assert_not_called()
        assert r["processor"]._completion_event.is_set()


class TestUniqueSidecarBase:
    """Reading a folder BACKWARDS to the base its sidecars hang off — the only
    way to find a split book's single-file-equivalent name, which nothing
    records. The same watch-item applies in reverse here: a chapter PART must
    never be read as a sidecar."""

    def test_a_lone_sidecar_names_the_base(self, tmp_path):
        (tmp_path / "Bram Stoker - Dracula.jpg").write_bytes(b"cover")
        assert processing_logic._unique_sidecar_base(str(tmp_path)) == str(tmp_path / "Bram Stoker - Dracula")

    def test_part_files_are_not_sidecars(self, tmp_path):
        (tmp_path / "Bram Stoker - Dracula.jpg").write_bytes(b"cover")
        (tmp_path / "Dracula - 01 - One.m4b").write_bytes(b"audio")
        (tmp_path / "Dracula - 02 - Two.mp3").write_bytes(b"audio")
        assert processing_logic._unique_sidecar_base(str(tmp_path)) == str(tmp_path / "Bram Stoker - Dracula")

    def test_the_longest_suffix_wins(self, tmp_path):
        # ".metadata.json" must not be read as a base of "Title.metadata".
        (tmp_path / "Title.metadata.json").write_bytes(b"{}")
        assert processing_logic._unique_sidecar_base(str(tmp_path)) == str(tmp_path / "Title")

    def test_agreeing_sidecars_still_name_one_base(self, tmp_path):
        for name in ("Title.jpg", "Title.pdf", "Title.metadata.json", "Title.voucher"):
            (tmp_path / name).write_bytes(b"x")
        assert processing_logic._unique_sidecar_base(str(tmp_path)) == str(tmp_path / "Title")

    def test_two_bases_are_ambiguous(self, tmp_path):
        (tmp_path / "Title.jpg").write_bytes(b"x")
        (tmp_path / "Other.pdf").write_bytes(b"x")
        assert processing_logic._unique_sidecar_base(str(tmp_path)) is None

    def test_no_sidecars_at_all(self, tmp_path):
        (tmp_path / "Dracula - 01 - One.m4b").write_bytes(b"audio")
        assert processing_logic._unique_sidecar_base(str(tmp_path)) is None

    def test_a_bare_suffix_claims_nothing(self, tmp_path):
        (tmp_path / ".pdf").write_bytes(b"x")
        assert processing_logic._unique_sidecar_base(str(tmp_path)) is None

    def test_a_missing_folder_is_answered_not_raised(self, tmp_path):
        assert processing_logic._unique_sidecar_base(str(tmp_path / "gone")) is None

    def test_uppercase_spellings_count(self, tmp_path):
        (tmp_path / "Title.JPG").write_bytes(b"x")
        assert processing_logic._unique_sidecar_base(str(tmp_path)) == str(tmp_path / "Title")


class TestOwnedSidecarBase:
    """Inferring a base is a guess; acting on it needs proof. A book folder on a
    real library is shared with whatever else manages that library — an
    Audiobookshelf "cover.jpg" reads back as a perfectly unambiguous base — so a
    base counts as ours only when its NAME is the stem we render today or its
    FILES are ones only this app writes."""

    def test_the_rendered_stem_names_the_base(self, tmp_path):
        (tmp_path / "Bram Stoker - Dracula.jpg").write_bytes(b"cover")
        base = processing_logic._owned_sidecar_base(str(tmp_path), "Bram Stoker - Dracula")
        assert base == str(tmp_path / "Bram Stoker - Dracula")

    def test_an_app_written_sidecar_names_the_base(self, tmp_path):
        # The renamed-book case: the stem no longer matches anything we would
        # render, but a curated metadata.json at that base is ours whatever it is
        # called, so the sweep and the rename can still follow it.
        (tmp_path / "Old Title.jpg").write_bytes(b"cover")
        (tmp_path / "Old Title.metadata.json").write_bytes(b"{}")
        base = processing_logic._owned_sidecar_base(str(tmp_path), "New Title")
        assert base == str(tmp_path / "Old Title")

    def test_an_uppercase_app_written_sidecar_still_counts(self, tmp_path):
        (tmp_path / "Old Title.Metadata.JSON").write_bytes(b"{}")
        assert processing_logic._owned_sidecar_base(str(tmp_path), "New Title") == str(tmp_path / "Old Title")

    def test_a_library_managers_cover_is_not_ours(self, tmp_path):
        # The v0.24.0 defect: "cover.jpg" nominates the base "cover" and the bare
        # "metadata.json" matches no suffix, so the folder looks unambiguous.
        (tmp_path / "cover.jpg").write_bytes(b"cover")
        (tmp_path / "metadata.json").write_bytes(b"{}")
        (tmp_path / "Dracula - 01 - One.m4b").write_bytes(b"audio")
        assert processing_logic._owned_sidecar_base(str(tmp_path), "Bram Stoker - Dracula") is None

    def test_a_foreign_pdf_or_cover_at_any_other_name_is_not_ours(self, tmp_path):
        # Neither of the two corroborating suffix-less signals: not our stem, and
        # a PDF/cover pair is exactly what other tools also leave lying about.
        (tmp_path / "Booklet.pdf").write_bytes(b"pdf")
        (tmp_path / "Booklet.jpg").write_bytes(b"cover")
        assert processing_logic._owned_sidecar_base(str(tmp_path), "Bram Stoker - Dracula") is None

    def test_an_unknown_stem_leaves_the_files_alone(self, tmp_path):
        # No expected stem to compare against (the naming metadata could not be
        # read): the app-written files are then the only proof on offer.
        (tmp_path / "Bram Stoker - Dracula.jpg").write_bytes(b"cover")
        assert processing_logic._owned_sidecar_base(str(tmp_path), None) is None
        (tmp_path / "Bram Stoker - Dracula.cue").write_bytes(b"cue")
        assert processing_logic._owned_sidecar_base(str(tmp_path), None) == str(tmp_path / "Bram Stoker - Dracula")

    def test_an_ambiguous_folder_is_still_ambiguous(self, tmp_path):
        # The inherited guard: two candidate bases answer None before ownership
        # is even asked, even when one of them is ours.
        (tmp_path / "Bram Stoker - Dracula.metadata.json").write_bytes(b"{}")
        (tmp_path / "Someone Else - Other.jpg").write_bytes(b"cover")
        assert processing_logic._owned_sidecar_base(str(tmp_path), "Bram Stoker - Dracula") is None

    def test_a_retained_master_and_voucher_corroborate_a_renamed_split_book(self, tmp_path):
        # W1: the realistic renamed-split-book folder. No cue sheet (D9 never
        # writes one for a split book) and both JSON dumps off, so the retained
        # AAXC master and its voucher are the only proof the folder is ours —
        # and they are hundreds of MB, so a skipped sweep strands them.
        (tmp_path / "Old Title.aaxc").write_bytes(b"master")
        (tmp_path / "Old Title.voucher").write_bytes(b"{}")
        (tmp_path / "Old Title.jpg").write_bytes(b"cover")
        assert processing_logic._owned_sidecar_base(str(tmp_path), "New Title") == str(tmp_path / "Old Title")

    def test_a_retained_aax_master_alone_corroborates(self, tmp_path):
        # The AAX fallback leaves a ".aax" and no voucher; it has to count too.
        (tmp_path / "Old Title.aax").write_bytes(b"master")
        assert processing_logic._owned_sidecar_base(str(tmp_path), "New Title") == str(tmp_path / "Old Title")

    def test_a_foreign_cue_does_not_corroborate_a_split_books_folder(self, tmp_path):
        # W4: split output never writes a cue sheet (D9 refuses one), so a ".cue"
        # at a stem this book does not render is affirmative evidence the base
        # belongs to something ELSE — cue sheets beside audiobooks are ordinary
        # output from CD rippers and taggers. It corroborated ownership anyway,
        # which handed a foreign base to the sweep that deletes and the rename
        # that moves.
        (tmp_path / "Old Title.cue").write_bytes(b"cue")
        (tmp_path / "Old Title.jpg").write_bytes(b"cover")
        assert processing_logic._owned_sidecar_base(str(tmp_path), "New Title", split=True) is None
        # ...and it still corroborates for a single-file book, which does write one.
        assert processing_logic._owned_sidecar_base(str(tmp_path), "New Title") == str(tmp_path / "Old Title")

    def test_a_split_books_own_stem_still_names_its_base(self, tmp_path):
        # The name arm is untouched: only the file-based backstop shrinks.
        (tmp_path / "Bram Stoker - Dracula.cue").write_bytes(b"cue")
        base = processing_logic._owned_sidecar_base(str(tmp_path), "Bram Stoker - Dracula", split=True)
        assert base == str(tmp_path / "Bram Stoker - Dracula")

    def test_a_retained_master_still_corroborates_a_renamed_split_book(self, tmp_path):
        # And the suffixes that matter most for a split book stay in the set: a
        # retained master and its voucher are usually the ONLY corroborator on
        # offer there, and they are hundreds of MB to strand.
        (tmp_path / "Old Title.aaxc").write_bytes(b"master")
        (tmp_path / "Old Title.voucher").write_bytes(b"{}")
        assert processing_logic._owned_sidecar_base(str(tmp_path), "New Title", split=True) == str(
            tmp_path / "Old Title"
        )

    def test_an_empty_folder_is_answered_not_raised(self, tmp_path):
        assert processing_logic._owned_sidecar_base(str(tmp_path / "gone"), "Title") is None

    def test_a_destructive_caller_is_told_why_the_sweep_was_skipped(self, tmp_path, caplog):
        # M1: the sweep and the rename stay loud — the refusal is the explanation
        # for files they left behind.
        (tmp_path / "cover.jpg").write_bytes(b"cover")
        with caplog.at_level(logging.INFO):
            assert processing_logic._owned_sidecar_base(str(tmp_path), "Bram Stoker - Dracula") is None
        assert "leaving them alone" in caplog.text

    def test_a_quiet_caller_says_nothing(self, tmp_path, caplog):
        (tmp_path / "cover.jpg").write_bytes(b"cover")
        with caplog.at_level(logging.INFO):
            assert processing_logic._owned_sidecar_base(str(tmp_path), "Bram Stoker - Dracula", quiet=True) is None
        assert "leaving them alone" not in caplog.text


class TestSidecarBaseForTrackedBook:
    """Where a finished book's sidecars live, asked of the database by whatever
    meets the book later (the Download Annotations button). For a split book that
    is a recovered answer — the stem is stored nowhere — so it is re-rendered
    from the naming template, and the folder's own sidecars win over that only
    when they are provably this book's."""

    ROW = {
        "filepath": None,  # set per test
        "author": "Bram Stoker",
        "title": "Dracula",
        "narrator": "N",
        "publisher": "P",
        "custom_title": None,
        "custom_author": None,
        "series": "N/A",
        "series_sequence": "N/A",
        "release_date": "N/A",
        "language": "N/A",
    }

    def _base(self, folder, parts, rendered="/data/Bram Stoker/Dracula/Bram Stoker - Dracula.m4b"):
        row = dict(self.ROW, filepath=str(folder))
        con = _FakeDb(book_row=row, part_rows=[str(p) for p in parts])
        with (
            mock.patch.object(processing_logic, "load_settings", return_value={}),
            mock.patch.object(processing_logic, "get_db_connection", return_value=con),
            mock.patch.object(processing_logic, "build_base_output_path", return_value=rendered),
        ):
            return processing_logic.sidecar_base_for_tracked_book("B0OURS")

    def test_a_library_managers_cover_never_becomes_the_base(self, tmp_path):
        # Writing the annotations dump at the inferred "cover" base would both
        # misfile it and make that foreign base look like ours ever after — the
        # re-rendered name is used instead.
        folder = tmp_path / "Dracula"
        folder.mkdir()
        part = folder / "Dracula - 01 - One.m4b"
        part.write_bytes(b"audio")
        (folder / "cover.jpg").write_bytes(b"cover")
        (folder / "metadata.json").write_bytes(b"{}")

        assert self._base(folder, [part]) == str(folder / "Bram Stoker - Dracula")

    def test_the_books_own_sidecars_still_win_over_the_rendered_name(self, tmp_path):
        # The reason the folder is consulted at all: this book was downloaded
        # under a naming template that has since changed, so its sidecars sit at
        # a stem nothing would render today. The app-written metadata JSON is
        # what proves they are its own.
        folder = tmp_path / "Dracula"
        folder.mkdir()
        part = folder / "Dracula - 01 - One.m4b"
        part.write_bytes(b"audio")
        (folder / "Old Name.metadata.json").write_bytes(b"{}")

        assert self._base(folder, [part]) == str(folder / "Old Name")

    def test_a_single_file_book_is_answered_from_its_own_path(self, tmp_path):
        book = tmp_path / "Dracula.m4b"
        book.write_bytes(b"audio")
        assert self._base(book, []) == str(tmp_path / "Dracula")

    def test_the_annotations_lookup_does_not_log_its_refusal(self, tmp_path, caplog):
        # M1: this runs on every Download Annotations press, and app.log is
        # user-downloadable — a foreign folder must not narrate itself each time.
        folder = tmp_path / "Dracula"
        folder.mkdir()
        part = folder / "Dracula - 01 - One.m4b"
        part.write_bytes(b"audio")
        (folder / "cover.jpg").write_bytes(b"cover")

        with caplog.at_level(logging.INFO):
            base = self._base(folder, [part])
        assert base == str(folder / "Bram Stoker - Dracula")
        assert "leaving them alone" not in caplog.text


class TestSplitFolderGuardLogging:
    """M2: the D5 flat-guard line announces a PLACEMENT, so only the planner may
    write it. The same helper answers the read-only "where do this book's
    sidecars live" question behind the Download Annotations button, which throws
    the folder away — and used to log a placement that wasn't happening into the
    user-downloadable app.log on every press."""

    def test_the_planner_announces_the_invented_subfolder(self, caplog):
        with caplog.at_level(logging.INFO):
            folder, stem = processing_logic._split_folder_and_stem("/data/Dracula", "", "B0OURS")
        assert (folder, stem) == ("/data/Dracula", "Dracula")
        assert "subfolder instead" in caplog.text

    def test_a_quiet_caller_says_nothing(self, caplog):
        with caplog.at_level(logging.INFO):
            folder, stem = processing_logic._split_folder_and_stem("/data/Dracula", "", "B0OURS", quiet=True)
        assert (folder, stem) == ("/data/Dracula", "Dracula")
        assert "subfolder instead" not in caplog.text

    def test_the_sidecar_base_lookup_is_quiet(self, caplog):
        # The real repro: a flat template plus a split book whose folder holds no
        # unambiguous sidecar, which is what sends the annotations route here.
        con = _FakeDb(book_row=BOOK_ROW)
        with (
            mock.patch.object(
                processing_logic, "load_settings", return_value={"naming": {"template": "{author} - {title}"}}
            ),
            mock.patch.object(processing_logic, "get_db_connection", return_value=con),
            caplog.at_level(logging.INFO),
        ):
            base = processing_logic._rendered_split_sidecar_base(
                "B0OURS",
                "/data/Bram Stoker - Dracula",
                ["/data/Bram Stoker - Dracula/Dracula - 1 - One.m4b"],
            )
        assert base == "/data/Bram Stoker - Dracula/Bram Stoker - Dracula"
        assert "subfolder instead" not in caplog.text

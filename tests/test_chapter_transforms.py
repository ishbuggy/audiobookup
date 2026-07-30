# tests/test_chapter_transforms.py

# Unit tests for the pure chapter transforms (PLAN v0.22.0 Phase 4). Every
# function here is I/O-free, so these run on the host with no container.

import pytest

from audible_downloader.chapter_transforms import (
    apply_branding_trim,
    drop_zero_length_chapters,
    flatten_chapter_tree,
    merge_credit_chapters,
    merge_short_chapters,
    render_chapter_title,
    strip_unabridged,
)


def _ch(title, start, length, children=None):
    """Build a chapter dict in the shape prepare_book_assets works with."""
    d = {"title": title, "start_offset_ms": start, "length_ms": length}
    if children is not None:
        d["chapters"] = children
    return d


class TestFlattenChapterTree:
    def test_flat_list_without_children_is_unchanged_content(self):
        chapters = [_ch("One", 0, 1000), _ch("Two", 1000, 1000)]
        out = flatten_chapter_tree(chapters, join_titles=True)
        assert [c["title"] for c in out] == ["One", "Two"]
        assert [c["start_offset_ms"] for c in out] == [0, 1000]

    def test_two_level_depth_first_order_no_join(self):
        chapters = [
            _ch("Part 1", 0, 500, children=[_ch("Ch A", 500, 500), _ch("Ch B", 1000, 500)]),
            _ch("Part 2", 1500, 500, children=[_ch("Ch C", 2000, 500)]),
        ]
        out = flatten_chapter_tree(chapters, join_titles=False)
        assert [c["title"] for c in out] == ["Part 1", "Ch A", "Ch B", "Part 2", "Ch C"]
        # Parents keep their own offsets; children keep theirs.
        assert [c["start_offset_ms"] for c in out] == [0, 500, 1000, 1500, 2000]

    def test_two_level_with_title_join(self):
        chapters = [
            _ch("Part 1", 0, 500, children=[_ch("Ch A", 500, 500), _ch("Ch B", 1000, 500)]),
            _ch("Part 2", 1500, 500, children=[_ch("Ch C", 2000, 500)]),
        ]
        out = flatten_chapter_tree(chapters, join_titles=True)
        assert [c["title"] for c in out] == [
            "Part 1",
            "Part 1: Ch A",
            "Part 1: Ch B",
            "Part 2",
            "Part 2: Ch C",
        ]

    def test_three_level_join_accumulates_through_depth(self):
        chapters = [
            _ch(
                "Part 1",
                0,
                100,
                children=[_ch("Ch A", 100, 100, children=[_ch("Section 1", 200, 100)])],
            ),
        ]
        out = flatten_chapter_tree(chapters, join_titles=True)
        assert [c["title"] for c in out] == ["Part 1", "Part 1: Ch A", "Part 1: Ch A: Section 1"]

    def test_custom_separator(self):
        chapters = [_ch("P", 0, 100, children=[_ch("C", 100, 100)])]
        out = flatten_chapter_tree(chapters, join_titles=True, separator=" - ")
        assert [c["title"] for c in out] == ["P", "P - C"]

    def test_children_key_is_dropped_from_output(self):
        chapters = [_ch("P", 0, 100, children=[_ch("C", 100, 100)])]
        out = flatten_chapter_tree(chapters, join_titles=True)
        assert all("chapters" not in c for c in out)

    def test_does_not_mutate_input(self):
        child = _ch("C", 100, 100)
        chapters = [_ch("P", 0, 100, children=[child])]
        flatten_chapter_tree(chapters, join_titles=True)
        # Original dicts untouched (title not joined, children key intact).
        assert child["title"] == "C"
        assert chapters[0]["chapters"] is not None

    def test_preserves_extra_keys_on_emitted_nodes(self):
        chapters = [_ch("P", 0, 100, children=[{"title": "C", "start_offset_ms": 100, "extra": 7}])]
        out = flatten_chapter_tree(chapters, join_titles=False)
        assert out[1]["extra"] == 7


class TestMergeCreditChapters:
    def test_leading_opening_credits_folds_into_following(self):
        chapters = [_ch("Opening Credits", 0, 500), _ch("Chapter 1", 500, 1000)]
        out = merge_credit_chapters(chapters)
        assert [c["title"] for c in out] == ["Chapter 1"]
        # Following chapter now begins where the credits began, absorbing the span.
        assert out[0]["start_offset_ms"] == 0
        assert out[0]["length_ms"] == 1500

    def test_trailing_end_credits_folds_into_preceding(self):
        chapters = [_ch("Chapter 1", 0, 1000), _ch("End Credits", 1000, 500)]
        out = merge_credit_chapters(chapters)
        assert [c["title"] for c in out] == ["Chapter 1"]
        assert out[0]["start_offset_ms"] == 0
        assert out[0]["length_ms"] == 1500

    def test_both_credits(self):
        chapters = [
            _ch("Opening Credits", 0, 300),
            _ch("Chapter 1", 300, 1000),
            _ch("End Credits", 1300, 400),
        ]
        out = merge_credit_chapters(chapters)
        assert [c["title"] for c in out] == ["Chapter 1"]
        assert out[0]["start_offset_ms"] == 0
        assert out[0]["length_ms"] == 1700

    def test_absent_credits_is_noop(self):
        chapters = [_ch("Chapter 1", 0, 1000), _ch("Chapter 2", 1000, 1000)]
        out = merge_credit_chapters(chapters)
        assert [c["title"] for c in out] == ["Chapter 1", "Chapter 2"]
        assert out == chapters

    def test_credit_only_book_leaves_lonely_opening_in_place(self):
        chapters = [_ch("Opening Credits", 0, 500)]
        out = merge_credit_chapters(chapters)
        assert [c["title"] for c in out] == ["Opening Credits"]

    def test_credit_only_book_leaves_lonely_end_in_place(self):
        chapters = [_ch("End Credits", 0, 500)]
        out = merge_credit_chapters(chapters)
        assert [c["title"] for c in out] == ["End Credits"]

    def test_case_insensitive_and_whitespace_tolerant(self):
        chapters = [_ch("  opening CREDITS ", 0, 500), _ch("Chapter 1", 500, 1000)]
        out = merge_credit_chapters(chapters)
        assert [c["title"] for c in out] == ["Chapter 1"]

    def test_partial_title_match_is_not_treated_as_credits(self):
        # A real chapter that merely contains the words must survive untouched.
        chapters = [_ch("The Opening Credits Heist", 0, 500), _ch("Chapter 1", 500, 1000)]
        out = merge_credit_chapters(chapters)
        assert [c["title"] for c in out] == ["The Opening Credits Heist", "Chapter 1"]

    def test_does_not_mutate_input(self):
        chapters = [_ch("Opening Credits", 0, 500), _ch("Chapter 1", 500, 1000)]
        merge_credit_chapters(chapters)
        assert [c["title"] for c in chapters] == ["Opening Credits", "Chapter 1"]
        assert chapters[1]["start_offset_ms"] == 500


class TestMergeShortChapters:
    """v0.24.0 groundwork: per-chapter splitting turns every chapter into its own
    file, so sub-minimum chapters (announcement markers, stingers) must fold
    FORWARD into the following chapter or the output is a pile of fragments.
    Pure transform — nothing wires it into the pipeline yet."""

    MIN_MS = 3_000

    def test_short_first_chapter_folds_into_the_following_one(self):
        chapters = [_ch("A", 0, 1_000), _ch("B", 1_000, 600_000)]
        out = merge_short_chapters(chapters, self.MIN_MS)
        assert [c["title"] for c in out] == ["A B"]
        # The survivor starts where the short one began and carries both spans.
        assert out[0]["start_offset_ms"] == 0
        assert out[0]["length_ms"] == 601_000

    def test_short_middle_chapter_folds_forward_not_backward(self):
        chapters = [_ch("A", 0, 600_000), _ch("B", 600_000, 1_000), _ch("C", 601_000, 600_000)]
        out = merge_short_chapters(chapters, self.MIN_MS)
        assert [c["title"] for c in out] == ["A", "B C"]
        # A is untouched; the merged chapter absorbed B's span from the front.
        assert out[0]["length_ms"] == 600_000
        assert out[1]["start_offset_ms"] == 600_000
        assert out[1]["length_ms"] == 601_000

    def test_consecutive_shorts_collapse_into_one_following_chapter(self):
        chapters = [
            _ch("A", 0, 500),
            _ch("B", 500, 500),
            _ch("C", 1_000, 500),
            _ch("D", 1_500, 600_000),
        ]
        out = merge_short_chapters(chapters, self.MIN_MS)
        assert [c["title"] for c in out] == ["A B C D"]
        assert out[0]["start_offset_ms"] == 0
        assert out[0]["length_ms"] == 601_500

    def test_merged_run_stops_once_it_reaches_the_minimum(self):
        # A+B is already 4s, over the 3s floor, so the run stops there and C
        # survives as its own chapter.
        chapters = [_ch("A", 0, 2_000), _ch("B", 2_000, 2_000), _ch("C", 4_000, 600_000)]
        out = merge_short_chapters(chapters, self.MIN_MS)
        assert [c["title"] for c in out] == ["A B", "C"]
        assert out[0]["length_ms"] == 4_000
        assert out[1]["start_offset_ms"] == 4_000

    def test_titles_concatenate_in_reading_order(self):
        chapters = [_ch("One", 0, 500), _ch("Two", 500, 500), _ch("Three", 1_000, 600_000)]
        out = merge_short_chapters(chapters, self.MIN_MS)
        assert out[0]["title"] == "One Two Three"

    def test_empty_titles_do_not_leave_stray_spaces(self):
        chapters = [_ch("", 0, 500), _ch("Real", 500, 600_000)]
        out = merge_short_chapters(chapters, self.MIN_MS)
        assert out[0]["title"] == "Real"

    def test_short_last_chapter_is_exempt(self):
        # Nothing follows it, so it has nowhere to fold and stays as-is.
        chapters = [_ch("A", 0, 600_000), _ch("B", 600_000, 1_000)]
        out = merge_short_chapters(chapters, self.MIN_MS)
        assert [c["title"] for c in out] == ["A", "B"]
        assert out[1]["length_ms"] == 1_000

    def test_single_short_chapter_book_is_untouched(self):
        chapters = [_ch("Only", 0, 1_000)]
        assert merge_short_chapters(chapters, self.MIN_MS) == chapters

    def test_all_long_chapters_is_a_noop(self):
        chapters = [_ch("A", 0, 600_000), _ch("B", 600_000, 600_000), _ch("C", 1_200_000, 600_000)]
        assert merge_short_chapters(chapters, self.MIN_MS) == chapters

    def test_exactly_the_minimum_is_long_enough(self):
        chapters = [_ch("A", 0, 3_000), _ch("B", 3_000, 600_000)]
        assert [c["title"] for c in merge_short_chapters(chapters, self.MIN_MS)] == ["A", "B"]

    @pytest.mark.parametrize("min_ms", [0, -1, -5_000])
    def test_zero_or_negative_minimum_disables_the_merge(self, min_ms):
        chapters = [_ch("A", 0, 1), _ch("B", 1, 600_000)]
        assert merge_short_chapters(chapters, min_ms) == chapters

    def test_empty_list(self):
        assert merge_short_chapters([], self.MIN_MS) == []

    def test_preserves_other_keys_on_the_surviving_chapter(self):
        chapters = [_ch("A", 0, 500), {"title": "B", "start_offset_ms": 500, "length_ms": 600_000, "extra": 7}]
        out = merge_short_chapters(chapters, self.MIN_MS)
        assert out[0]["extra"] == 7

    def test_does_not_mutate_input(self):
        chapters = [_ch("A", 0, 500), _ch("B", 500, 600_000)]
        merge_short_chapters(chapters, self.MIN_MS)
        assert [c["title"] for c in chapters] == ["A", "B"]
        assert chapters[1]["start_offset_ms"] == 500
        assert chapters[1]["length_ms"] == 600_000

    def test_disabled_path_still_returns_copies(self):
        # Same convention as every other transform here: the caller's dicts are
        # never handed back, even on the pass-through path.
        chapters = [_ch("A", 0, 500)]
        out = merge_short_chapters(chapters, 0)
        assert out == chapters
        assert out[0] is not chapters[0]


class TestApplyBrandingTrim:
    """Phase 6: shift chapters into the branding-trimmed output timeline and
    report that timeline's length. Lengths are deliberately left alone here —
    the caller's sanitize step recomputes them from the starts and the effective
    total, which is what shortens the last chapter by the outro."""

    CHAPTERS = [_ch("One", 0, 600_000), _ch("Two", 600_000, 600_000), _ch("Three", 1_200_000, 600_000)]
    TOTAL = 1_800_000

    def test_intro_only_shifts_starts_and_shortens_total(self):
        out, effective = apply_branding_trim(self.CHAPTERS, 2_000, 0, self.TOTAL)
        assert [c["start_offset_ms"] for c in out] == [0, 598_000, 1_198_000]
        assert effective == 1_798_000

    def test_outro_only_leaves_starts_and_shortens_total(self):
        out, effective = apply_branding_trim(self.CHAPTERS, 0, 5_000, self.TOTAL)
        assert [c["start_offset_ms"] for c in out] == [0, 600_000, 1_200_000]
        assert effective == 1_795_000

    def test_both_spans(self):
        out, effective = apply_branding_trim(self.CHAPTERS, 2_043, 5_061, self.TOTAL)
        assert [c["start_offset_ms"] for c in out] == [0, 597_957, 1_197_957]
        assert effective == self.TOTAL - 2_043 - 5_061

    def test_zero_spans_are_identity(self):
        out, effective = apply_branding_trim(self.CHAPTERS, 0, 0, self.TOTAL)
        assert out == self.CHAPTERS
        assert effective == self.TOTAL

    def test_chapter_starting_inside_intro_clamps_to_zero(self):
        # Audible sometimes places a chapter boundary partway through the brand
        # intro; the shifted start must never go negative.
        chapters = [_ch("Opening", 0, 1_000), _ch("One", 1_000, 600_000)]
        out, _ = apply_branding_trim(chapters, 2_043, 0, self.TOTAL)
        assert [c["start_offset_ms"] for c in out] == [0, 0]

    def test_missing_start_offset_treated_as_zero(self):
        out, _ = apply_branding_trim([{"title": "One"}], 2_000, 0, self.TOTAL)
        assert out[0]["start_offset_ms"] == 0

    def test_preserves_other_keys(self):
        out, _ = apply_branding_trim([{"title": "One", "start_offset_ms": 5_000, "extra": 7}], 2_000, 0, self.TOTAL)
        assert out[0] == {"title": "One", "start_offset_ms": 3_000, "extra": 7}

    def test_empty_chapter_list_still_reports_effective_total(self):
        out, effective = apply_branding_trim([], 2_000, 5_000, self.TOTAL)
        assert out == []
        assert effective == 1_793_000

    def test_does_not_mutate_input(self):
        chapters = [_ch("One", 10_000, 600_000)]
        apply_branding_trim(chapters, 2_000, 0, self.TOTAL)
        assert chapters[0]["start_offset_ms"] == 10_000

    def test_marker_inside_the_outro_is_dropped(self):
        # v0.23.0 regression: a marker whose span lies wholly inside the brand
        # outro. Keeping it made the preceding chapter run past the trim boundary
        # (its chunk encode read outro audio) and emitted a chapter start beyond
        # the end of the output.
        chapters = [
            _ch("One", 0, 600_000),
            _ch("Two", 600_000, 1_195_000),
            _ch("Outro Marker", 1_795_000, 5_000),
        ]
        out, effective = apply_branding_trim(chapters, 0, 10_000, self.TOTAL)
        assert [c["title"] for c in out] == ["One", "Two"]
        assert effective == 1_790_000

    def test_marker_before_the_boundary_survives(self):
        # One millisecond on the retained side of the same boundary is real audio.
        chapters = [_ch("One", 0, 600_000), _ch("Two", 1_789_999, 10_001)]
        out, effective = apply_branding_trim(chapters, 0, 10_000, self.TOTAL)
        assert [c["title"] for c in out] == ["One", "Two"]
        assert out[-1]["start_offset_ms"] == 1_789_999
        assert effective == 1_790_000

    def test_marker_the_intro_shift_pulls_back_into_the_retained_audio_is_kept(self):
        # The drop compares the SHIFTED start against the effective total, not the
        # raw one. Here the raw start (1_792_000) is already past that total
        # (1_790_000), but the intro shift moves it down to 1_787_000 — inside the
        # retained audio, so the marker stays.
        chapters = [_ch("One", 0, 600_000), _ch("Two", 1_792_000, 8_000)]
        out, _ = apply_branding_trim(chapters, 5_000, 5_000, self.TOTAL)
        assert [c["start_offset_ms"] for c in out] == [0, 1_787_000]

    def test_drop_does_not_mutate_input(self):
        chapters = [_ch("One", 0, 600_000), _ch("Outro Marker", 1_795_000, 5_000)]
        apply_branding_trim(chapters, 0, 10_000, self.TOTAL)
        assert [c["title"] for c in chapters] == ["One", "Outro Marker"]
        assert chapters[1]["start_offset_ms"] == 1_795_000


def _sanitize_lengths(chapters, effective_total_ms):
    """Mirror of prepare_book_assets' sanitize loop: recompute every length from
    the next chapter's start (the last one from the effective total), capped at
    the effective total and clamped at zero. Kept here so the drop tests exercise
    the same inputs the pipeline actually feeds drop_zero_length_chapters."""
    out = [dict(ch) for ch in chapters]
    out.sort(key=lambda x: x.get("start_offset_ms", 0))
    for i, ch in enumerate(out):
        current_start = ch.get("start_offset_ms", 0)
        if i < len(out) - 1:
            new_length = min(out[i + 1].get("start_offset_ms", 0), effective_total_ms) - current_start
        else:
            new_length = effective_total_ms - current_start
        ch["length_ms"] = max(0, new_length)
    return out


class TestDropZeroLengthChapters:
    """B1 regression: a chapter that spans no audio becomes a "-t 0" chunk encode
    (a header-only file with no audio stream), and in first position that fails
    the concat merge outright. Both new v0.22.0 chapter features can produce one."""

    def test_normal_list_passes_through_unchanged(self):
        chapters = [_ch("One", 0, 600_000), _ch("Two", 600_000, 600_000)]
        assert drop_zero_length_chapters(chapters) == chapters

    def test_drops_zero_and_negative_lengths(self):
        chapters = [_ch("Zero", 0, 0), _ch("One", 0, 600_000), _ch("Negative", 600_000, -5)]
        assert [c["title"] for c in drop_zero_length_chapters(chapters)] == ["One"]

    def test_missing_length_key_is_dropped(self):
        assert drop_zero_length_chapters([{"title": "One", "start_offset_ms": 0}]) == []

    def test_does_not_mutate_input(self):
        chapters = [_ch("One", 0, 0)]
        drop_zero_length_chapters(chapters)
        assert chapters == [_ch("One", 0, 0)]

    def test_kept_chapters_are_shallow_copies(self):
        # ND3/ND4: every other transform here hands back copies, so this one did
        # too little — it returned the caller's own dicts. A later step editing a
        # kept chapter would then reach back into the input list.
        chapters = [_ch("One", 0, 600_000)]
        kept = drop_zero_length_chapters(chapters)
        assert kept == chapters  # same content...
        assert kept[0] is not chapters[0]  # ...different objects
        kept[0]["title"] = "Edited"
        assert chapters[0]["title"] == "One"

    def test_flattened_parent_sharing_first_childs_start_leaves_no_zero(self):
        # The primary vector: a part whose first child begins at the part's own
        # offset. Flatten emits both, sanitize gives the parent length 0.
        nested = [
            _ch("Part 1", 0, 1_200_000, children=[_ch("Ch 1", 0, 600_000), _ch("Ch 2", 600_000, 600_000)]),
        ]
        flat = flatten_chapter_tree(nested, join_titles=True)
        sanitized = _sanitize_lengths(flat, 1_200_000)
        assert 0 in [c["length_ms"] for c in sanitized]  # the defect this guards

        kept = drop_zero_length_chapters(sanitized)
        assert all(c["length_ms"] > 0 for c in kept)
        assert [c["title"] for c in kept] == ["Part 1: Ch 1", "Part 1: Ch 2"]

    def test_trim_clamped_duplicate_starts_leave_no_zero(self):
        # The Phase 6 vector: chapter 2 starts inside the brand intro, so both it
        # and chapter 1 clamp to start 0 and the first ends up zero-length.
        chapters = [_ch("Opening", 0, 1_000), _ch("One", 1_000, 600_000), _ch("Two", 601_000, 600_000)]
        shifted, effective = apply_branding_trim(chapters, 2_043, 0, 1_201_000)
        sanitized = _sanitize_lengths(shifted, effective)
        assert sanitized[0]["length_ms"] == 0

        kept = drop_zero_length_chapters(sanitized)
        assert all(c["length_ms"] > 0 for c in kept)
        assert [c["title"] for c in kept] == ["One", "Two"]


class TestStripUnabridged:
    @pytest.mark.parametrize(
        ("text", "expected"),
        [
            ("The Book (Unabridged)", "The Book"),
            ("The Book (unabridged)", "The Book"),
            ("The Book (UNABRIDGED)", "The Book"),
            ("The Book (Unabridged): Subtitle", "The Book: Subtitle"),
            ("Book (Unabridged) Two", "Book Two"),
            ("No tag here", "No tag here"),
            ("", ""),
            (None, None),
        ],
    )
    def test_variants(self, text, expected):
        assert strip_unabridged(text) == expected

    def test_collapses_doubled_spaces_left_behind(self):
        # A mid-string removal that would otherwise leave two spaces.
        assert strip_unabridged("Book  (Unabridged)  Two") == "Book Two"


class TestRenderChapterTitle:
    def test_default_template_reproduces_chapter_title(self):
        assert render_chapter_title("{ch_title}", 1, 10, "Chapter One", "My Book") == "Chapter One"

    def test_number_and_total(self):
        assert render_chapter_title("{ch} of {ch_total}", 3, 10, "X", "Y") == "3 of 10"

    def test_ch_not_corrupting_ch_total_or_ch_title(self):
        # {ch} is a prefix of the longer tags; ordering must not mangle them.
        assert render_chapter_title("{ch} {ch_total} {ch_title}", 2, 5, "Intro", "Book") == "2 5 Intro"

    def test_book_title_tag(self):
        assert render_chapter_title("{title} - {ch}", 4, 12, "Ch", "Dune") == "Dune - 4"

    def test_unknown_braces_pass_through(self):
        assert render_chapter_title("{ch} {unknown}", 1, 2, "t", "b") == "1 {unknown}"

    def test_none_values_render_empty(self):
        assert render_chapter_title("[{ch_title}][{title}]", 1, 1, None, None) == "[][]"

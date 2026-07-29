# audible_downloader/chapter_transforms.py

# Pure, side-effect-free transforms over the plain chapter dicts that
# `prepare_book_assets` builds from Audible's chapter JSON. Each chapter dict
# carries `title`, `start_offset_ms`, `length_ms`, and (in the nested/Tree
# shape) an optional `chapters` list of children.
#
# Everything here is I/O-free and deterministic so it can be unit-tested on the
# host without a container or a real download. The wire-up (which of these run,
# and in what order) lives in `chunked_conversion_logic.prepare_book_assets`;
# see PLAN §6 Phase 4 for the exact ordering. All of these are gated OFF by
# default so the shipped chapter output is byte-for-byte unchanged.

import re

# Credit-chapter titles are matched whole-string (case-insensitive), tolerating
# surrounding whitespace, so a real chapter merely containing the words
# ("The Opening Credits Heist") is never touched.
_OPENING_CREDITS_RE = re.compile(r"^\s*Opening Credits\s*$", re.IGNORECASE)
_END_CREDITS_RE = re.compile(r"^\s*End Credits\s*$", re.IGNORECASE)

# "(Unabridged)" with any leading whitespace, case-insensitive. The leading
# `\s*` absorbs the space before a trailing tag so "Book (Unabridged)" collapses
# cleanly to "Book" without a dangling space.
_UNABRIDGED_RE = re.compile(r"\s*\((?:Unabridged)\)", re.IGNORECASE)


def flatten_chapter_tree(chapters, join_titles, separator=": "):
    """Depth-first flatten a nested chapter tree into a flat list.

    Emits each node (as a shallow copy WITHOUT its `chapters` key) and then
    recurses into that node's children, so the output is in reading order.

    When `join_titles` is true, a child's title is prefixed with its ancestors'
    titles joined by `separator`, accumulating through depth:
    "Part 1" -> "Part 1: Chapter 1" -> "Part 1: Chapter 1: Section A". A node
    with no ancestors keeps its own title unchanged.

    Parents keep their own `start_offset_ms`; the caller's sanitize step
    recomputes every `length_ms` from the next start, which correctly shrinks a
    parent entry to just its own intro span before its first child.
    """
    flat = []

    def _walk(nodes, prefix):
        for node in nodes:
            own_title = node.get("title", "")
            combined_title = f"{prefix}{separator}{own_title}" if prefix else own_title

            # Shallow copy without the nested-children key; the flat list has no
            # place for it and leaving it in would confuse downstream consumers.
            emitted = {k: v for k, v in node.items() if k != "chapters"}
            emitted["title"] = combined_title
            flat.append(emitted)

            children = node.get("chapters")
            if children:
                # Descendants prefix with the *combined* title so joins
                # accumulate; when joining is off, prefix stays empty throughout.
                _walk(children, combined_title if join_titles else "")

    _walk(chapters, "")
    return flat


def merge_credit_chapters(chapters):
    """Fold "Opening Credits"/"End Credits" chapters into their neighbors.

    Operates on a FLAT list and returns a new list of shallow copies (the
    caller's dicts are never mutated):

    - An "Opening Credits" chapter is removed and its span folded into the
      FOLLOWING chapter (that chapter now starts where the credits began).
    - An "End Credits" chapter is removed and its span folded into the PRECEDING
      chapter.

    Both fold the removed span geometrically (start/length adjusted) so the
    result is correct standalone. In the pipeline the later sanitize step
    recomputes lengths from starts anyway, so the length adjustments here are
    belt-and-suspenders. A credit chapter with no neighbor to fold into (e.g. a
    single-chapter, credits-only book) is left in place.
    """
    result = [dict(ch) for ch in chapters]

    # Opening Credits -> fold forward into the following chapter.
    i = 0
    while i < len(result):
        if _OPENING_CREDITS_RE.match(result[i].get("title", "")):
            if i + 1 < len(result):
                removed = result.pop(i)
                following = result[i]
                following["start_offset_ms"] = removed.get("start_offset_ms", 0)
                following["length_ms"] = following.get("length_ms", 0) + removed.get("length_ms", 0)
                # Do not advance: re-examine whatever now sits at position i.
                continue
            # No following chapter to fold into — leave the credits in place.
        i += 1

    # End Credits -> fold backward into the preceding chapter.
    i = 0
    while i < len(result):
        if _END_CREDITS_RE.match(result[i].get("title", "")):
            if i > 0:
                removed = result.pop(i)
                preceding = result[i - 1]
                preceding["length_ms"] = preceding.get("length_ms", 0) + removed.get("length_ms", 0)
                # `i` now indexes the chapter that followed the removed one.
                continue
            # No preceding chapter to fold into — leave the credits in place.
        i += 1

    return result


def apply_branding_trim(chapters, intro_ms, outro_ms, total_duration_ms):
    """Shift a FLAT chapter list into the branding-trimmed OUTPUT timeline.

    Audible's masters open with a "This is Audible" brand intro and close with a
    matching outro; their lengths come from the chapter JSON (`brandIntroDurationMs`
    / `brandOutroDurationMs`). Cutting them means the output is the master minus a
    head span and a tail span, so:

    - every `start_offset_ms` moves down by `intro_ms`, clamped at 0 — a chapter
      that begins *inside* the intro (Audible sometimes starts chapter 1 at 0)
      lands at the top of the trimmed file rather than going negative;
    - `effective_total_ms = total_duration_ms - intro_ms - outro_ms` is the length
      of the trimmed output.

    Returns `(chapters, effective_total_ms)`. The returned chapters are shallow
    copies (inputs are never mutated) and `length_ms` is left as-is: the caller's
    sanitize step recomputes every length from the starts and the effective total,
    which is what shortens the FINAL chapter by the outro.

    Callers that seek into the untrimmed master afterwards (the per-chapter AAC
    encode, the single-pass MP3 encode) must add `intro_ms` back to the seek —
    these offsets are output-timeline, not source-timeline.
    """
    shifted = []
    for ch in chapters:
        new_ch = dict(ch)
        new_ch["start_offset_ms"] = max(0, new_ch.get("start_offset_ms", 0) - intro_ms)
        shifted.append(new_ch)

    return shifted, total_duration_ms - intro_ms - outro_ms


def strip_unabridged(text):
    """Remove every "(Unabridged)" occurrence (with leading whitespace) and
    collapse any doubled spaces the removal leaves behind. Non-string / falsy
    input is returned unchanged."""
    if not text:
        return text
    stripped = _UNABRIDGED_RE.sub("", text)
    # Collapse runs of two-or-more spaces down to one (a mid-string tag removal
    # can leave "Book  Two"); leaves single spaces and other whitespace alone.
    return re.sub(r" {2,}", " ", stripped)


def render_chapter_title(template, ch_num, ch_total, ch_title, book_title):
    """Render a per-chapter title from a `{tag}` template.

    Supported tags: `{ch}` (1-based chapter number), `{ch_total}` (chapter
    count), `{ch_title}` (the chapter's own title), `{title}` (the book title).
    Unknown braces pass through untouched. The default template `{ch_title}`
    reproduces the chapter's title verbatim.
    """
    # Replace the longer tags first: `{ch}` is a prefix of `{ch_total}`/
    # `{ch_title}`, so substituting it first would corrupt them.
    return (
        template.replace("{ch_total}", str(ch_total))
        .replace("{ch_title}", ch_title if ch_title is not None else "")
        .replace("{title}", book_title if book_title is not None else "")
        .replace("{ch}", str(ch_num))
    )

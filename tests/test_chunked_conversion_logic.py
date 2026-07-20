# tests/test_chunked_conversion_logic.py

import subprocess

from audible_downloader.chunked_conversion_logic import _summarize_subprocess_error


class TestSummarizeSubprocessError:
    """Bug 7: turn a failed subprocess into a concise, human-readable reason
    instead of the bare 'returned non-zero exit status N'."""

    def test_uses_last_stderr_lines(self):
        exc = subprocess.CalledProcessError(
            1, ["audible", "download"], stderr="starting\nBook not available in your region\n"
        )
        assert _summarize_subprocess_error(exc, "fallback") == "starting | Book not available in your region"

    def test_keeps_only_the_last_three_lines(self):
        exc = subprocess.CalledProcessError(1, ["x"], stderr="a\nb\nc\nd\ne\n")
        assert _summarize_subprocess_error(exc, "fallback") == "c | d | e"

    def test_decodes_bytes_stderr(self):
        exc = subprocess.CalledProcessError(1, ["ffmpeg"], stderr=b"boom\n")
        assert _summarize_subprocess_error(exc, "fallback") == "boom"

    def test_falls_back_when_no_stderr_attribute(self):
        assert _summarize_subprocess_error(ValueError("nope"), "fallback") == "fallback"

    def test_falls_back_when_stderr_blank(self):
        exc = subprocess.CalledProcessError(1, ["x"], stderr="   \n\n")
        assert _summarize_subprocess_error(exc, "fallback") == "fallback"

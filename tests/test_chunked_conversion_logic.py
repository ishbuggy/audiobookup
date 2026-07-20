# tests/test_chunked_conversion_logic.py

import subprocess

from audible_downloader.chunked_conversion_logic import _summarize_subprocess_error


class TestSummarizeSubprocessError:
    """Bug 7: turn a failed subprocess into a concise, human-readable reason
    instead of the bare 'returned non-zero exit status N'."""

    def test_prefers_audible_error_on_stdout_over_stderr_noise(self):
        # The real audible-cli failure this fix targets: the useful line is on
        # stdout, while stderr only has a tqdm bar and a generic "Aborted!".
        exc = subprocess.CalledProcessError(
            1,
            ["audible", "download"],
            output="error: Asin B007MFQ15O not found in library.\n",
            stderr="foo.aaxc: 100%|##########| 21.7M/21.7M\nAborted!\n",
        )
        assert _summarize_subprocess_error(exc, "fallback") == "error: Asin B007MFQ15O not found in library."

    def test_prefers_explicit_error_line_among_others(self):
        exc = subprocess.CalledProcessError(1, ["x"], stderr="line one\nnot available in your region\nline three")
        assert _summarize_subprocess_error(exc, "fallback") == "not available in your region"

    def test_falls_back_to_last_lines_without_error_keyword(self):
        exc = subprocess.CalledProcessError(1, ["ffmpeg"], stderr="a\nb\nc\nd\ne\n")
        assert _summarize_subprocess_error(exc, "fallback") == "c | d | e"

    def test_skips_progress_bars_and_aborted(self):
        exc = subprocess.CalledProcessError(1, ["x"], stderr="thing: 50%|#####| 1/2\nAborted!\n")
        assert _summarize_subprocess_error(exc, "fallback") == "fallback"

    def test_decodes_bytes_streams(self):
        exc = subprocess.CalledProcessError(1, ["ffmpeg"], stderr=b"boom\n")
        assert _summarize_subprocess_error(exc, "fallback") == "boom"

    def test_falls_back_when_no_output_attributes(self):
        assert _summarize_subprocess_error(ValueError("nope"), "fallback") == "fallback"

    def test_falls_back_when_streams_blank(self):
        exc = subprocess.CalledProcessError(1, ["x"], output="   \n", stderr="   \n\n")
        assert _summarize_subprocess_error(exc, "fallback") == "fallback"

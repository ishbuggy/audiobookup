# tests/test_chunked_conversion_logic.py

import subprocess
from unittest import mock

from audible_downloader import chunked_conversion_logic as ccl
from audible_downloader.chunked_conversion_logic import _embed_cover_art, _summarize_subprocess_error


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


class TestEmbedCoverArt:
    """Bug 5: cover art is embedded via AtomicParsley (ffmpeg can't write the
    cover and use_metadata_tags together), best-effort."""

    def test_skips_when_no_cover_file(self):
        with mock.patch.object(ccl.subprocess, "Popen") as popen:
            _embed_cover_art("B0X", 1, "/data/out.m4b", None)
        popen.assert_not_called()

    def test_skips_when_cover_missing_on_disk(self):
        with mock.patch("os.path.exists", return_value=False), mock.patch.object(ccl.subprocess, "Popen") as popen:
            _embed_cover_art("B0X", 1, "/data/out.m4b", "/tmp/cover.jpg")
        popen.assert_not_called()

    def _run_with_returncode(self, returncode):
        proc = mock.MagicMock()
        proc.returncode = returncode
        proc.communicate.return_value = ("", "atomicparsley says no")
        with (
            mock.patch("os.path.exists", return_value=True),
            mock.patch.object(ccl.subprocess, "Popen", return_value=proc) as popen,
            mock.patch.object(ccl.process_registry, "register"),
            mock.patch.object(ccl.process_registry, "unregister"),
        ):
            _embed_cover_art("B0X", 7, "/data/out.m4b", "/tmp/cover.jpg")
        return popen

    def test_runs_atomicparsley_with_artwork(self):
        popen = self._run_with_returncode(0)
        cmd = popen.call_args.args[0]
        assert cmd[0] == "AtomicParsley"
        assert cmd[1] == "/data/out.m4b"
        assert "--artwork" in cmd and "/tmp/cover.jpg" in cmd and "--overWrite" in cmd

    def test_nonzero_exit_is_non_fatal(self):
        # Must not raise — a failed cover embed leaves the (usable) book intact.
        self._run_with_returncode(1)

    def test_missing_binary_is_non_fatal(self):
        with (
            mock.patch("os.path.exists", return_value=True),
            mock.patch.object(ccl.subprocess, "Popen", side_effect=OSError("no AtomicParsley")),
        ):
            _embed_cover_art("B0X", 1, "/data/out.m4b", "/tmp/cover.jpg")  # should not raise

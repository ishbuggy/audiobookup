# tests/test_chunked_conversion_logic.py

import subprocess
from unittest import mock

import pytest

from audible_downloader import chunked_conversion_logic as ccl
from audible_downloader.chunked_conversion_logic import (
    _embed_cover_art,
    _should_auto_chunk,
    _summarize_subprocess_error,
    remux_book_lossless,
)


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


class TestDownloadRegistration:
    """M6 regression: the download Popen must be unregistered from the process
    registry on every exit, including the SIGTERM (-15) cancel path — which
    previously returned (None, None) without unregistering, leaking a dead
    process reference into the registry."""

    def test_cancelled_download_unregisters_process(self):
        proc = mock.MagicMock()
        proc.stderr.readline.return_value = ""  # no progress lines; the loop ends at once
        proc.wait.return_value = -15  # SIGTERM: the job was cancelled mid-download
        proc.stdout.read.return_value = ""

        with (
            mock.patch.object(ccl, "load_settings", return_value={}),
            mock.patch.object(ccl.subprocess, "Popen", return_value=proc),
            mock.patch.object(ccl.process_registry, "register") as register,
            mock.patch.object(ccl.process_registry, "unregister") as unregister,
            mock.patch.object(ccl, "_yield_progress"),
        ):
            result = ccl.prepare_book_assets("B0X", 1, "/tmp/x")

        assert result == (None, None)  # cancellation signal, not a failure
        register.assert_called_once_with(1, proc)
        unregister.assert_called_once_with(1, proc)


class TestRunRegistered:
    """L3: short probe/metadata subprocesses run registered so a job cancel can
    SIGTERM them, while preserving subprocess.run's check=True semantics so the
    existing fallback/error-summary handling is unchanged."""

    def test_registers_and_unregisters(self):
        proc = mock.MagicMock()
        proc.returncode = 0
        proc.communicate.return_value = ("out", "err")
        with (
            mock.patch.object(ccl.subprocess, "Popen", return_value=proc),
            mock.patch.object(ccl.process_registry, "register") as register,
            mock.patch.object(ccl.process_registry, "unregister") as unregister,
        ):
            result = ccl._run_registered(["ffprobe"], 3)
        assert result.returncode == 0
        assert result.stdout == "out"
        register.assert_called_once_with(3, proc)
        unregister.assert_called_once_with(3, proc)

    def test_check_raises_and_still_unregisters(self):
        proc = mock.MagicMock()
        proc.returncode = 1
        proc.communicate.return_value = ("bad stdout", "boom")
        with (
            mock.patch.object(ccl.subprocess, "Popen", return_value=proc),
            mock.patch.object(ccl.process_registry, "register"),
            mock.patch.object(ccl.process_registry, "unregister") as unregister,
        ):
            with pytest.raises(subprocess.CalledProcessError) as excinfo:
                ccl._run_registered(["audible", "api"], 3, check=True)
        # The raised error carries the captured streams (used by the summarizer).
        assert excinfo.value.output == "bad stdout"
        assert excinfo.value.stderr == "boom"
        # Unregistered even though check raised (finally-paired).
        unregister.assert_called_once_with(3, proc)


class TestRemuxLossless:
    """Phase 8 (FR12): no-re-encode finalize copies the audio straight through
    (-c copy) while muxing chapters/metadata, then embeds the cover — same
    cancellation contract as the merge path."""

    CONTEXT = {
        "audio_file": "/tmp/x/master_intermediate.m4b",
        "chapter_file": "/tmp/x/chapters.txt",
        "cover_file": "/tmp/x/cover.jpg",
    }

    def _run_with_returncode(self, returncode):
        proc = mock.MagicMock()
        proc.returncode = returncode
        proc.communicate.return_value = ("", "ffmpeg says no")
        with (
            mock.patch.object(ccl.subprocess, "Popen", return_value=proc) as popen,
            mock.patch.object(ccl.process_registry, "register"),
            mock.patch.object(ccl.process_registry, "unregister"),
            mock.patch.object(ccl, "_embed_cover_art") as embed,
            mock.patch.object(ccl, "_yield_progress"),
        ):
            result = remux_book_lossless("B0X", 1, "/tmp/x", "/data/out.m4b", self.CONTEXT)
        return result, popen, embed

    def test_success_copies_audio_and_embeds_cover(self):
        result, popen, embed = self._run_with_returncode(0)
        assert result is True
        cmd = popen.call_args.args[0]
        assert cmd[0] == "ffmpeg"
        # Single master input (not a concat demuxer) copied straight through.
        assert cmd.count("-i") == 2
        assert self.CONTEXT["audio_file"] in cmd
        assert "-c" in cmd and "copy" in cmd
        assert "concat" not in cmd
        embed.assert_called_once_with("B0X", 1, "/data/out.m4b", self.CONTEXT["cover_file"])

    def test_cancellation_returns_false_without_cover(self):
        result, _, embed = self._run_with_returncode(-15)
        assert result is False
        embed.assert_not_called()

    def test_failure_returns_false_without_cover(self):
        result, _, embed = self._run_with_returncode(1)
        assert result is False
        embed.assert_not_called()


class TestShouldAutoChunk:
    """Phase 8 (FR12): the single-chapter auto-chunking gate. It is suppressed
    only when the title will truly be remuxed losslessly (no-re-encode on AND an
    AAC ".m4b" master). A FLAC fallback still auto-chunks, because that title
    takes the re-encode path and needs the parallel chunks / navigation markers."""

    LONG = ccl.AUTO_CHUNK_TRIGGER_SEC + 1  # long enough to trigger chunking
    SHORT = ccl.AUTO_CHUNK_TRIGGER_SEC - 1

    def test_reencode_single_chapter_long_book_chunks(self):
        assert _should_auto_chunk(False, "/t/master_intermediate.m4b", [{}], self.LONG) is True

    def test_reencode_chapterless_long_book_chunks(self):
        # 0 native chapters (the case that would otherwise fail "no chapter information").
        assert _should_auto_chunk(False, "/t/master_intermediate.m4b", [], self.LONG) is True

    def test_lossless_aac_master_never_chunks(self):
        # True lossless path: keep the native (possibly empty) chapters as-is.
        assert _should_auto_chunk(True, "/t/master_intermediate.m4b", [], self.LONG) is False
        assert _should_auto_chunk(True, "/t/master_intermediate.M4B", [{}], self.LONG) is False

    def test_lossless_requested_but_flac_master_still_chunks(self):
        # M1 regression: a FLAC fallback takes the re-encode path, so the
        # requested-but-unavailable lossless flag must NOT suppress chunking —
        # otherwise a chapterless FLAC book would reach the encode path with 0
        # chapters and fail. It must still chunk.
        assert _should_auto_chunk(True, "/t/master_intermediate.flac", [], self.LONG) is True
        assert _should_auto_chunk(True, "/t/master_intermediate.flac", [{}], self.LONG) is True

    @pytest.mark.parametrize("lossless", [True, False])
    def test_multi_chapter_book_is_never_chunked(self, lossless):
        assert _should_auto_chunk(lossless, "/t/master_intermediate.m4b", [{}, {}], self.LONG) is False

    def test_short_single_chapter_book_is_not_chunked(self):
        # Below the duration trigger, even the re-encode path leaves it alone.
        assert _should_auto_chunk(False, "/t/master_intermediate.m4b", [{}], self.SHORT) is False

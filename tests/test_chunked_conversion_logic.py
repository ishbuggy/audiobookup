# tests/test_chunked_conversion_logic.py

import subprocess
from unittest import mock

import pytest

from audible_downloader import chunked_conversion_logic as ccl
from audible_downloader.chunked_conversion_logic import (
    _embed_cover_art,
    _should_auto_chunk,
    _summarize_subprocess_error,
    build_mp3_flags,
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


class _FakeDirEntry:
    """Minimal os.scandir entry stand-in for the download's file-detection step."""

    def __init__(self, path):
        self.path = path
        self.name = path.rsplit("/", 1)[-1]

    def is_file(self):
        return True


class TestCancelDuringPrepProbe:
    """WF#2 (adversarial review): a cancel (-15) arriving during the check=False
    prep probes (the post-decrypt duration probe) must be treated as cancellation
    — return (None, None) — NOT let the empty-output ValueError fall through to the
    inner decryption `except`, which would `continue` into a full (and equally
    cancelled) FLAC decode that kill_job_processes can no longer reach."""

    def _drive_to_probe(self, probe_returncode):
        # Download Popen succeeds; the stderr loop ends immediately and wait()==0.
        download_proc = mock.MagicMock()
        download_proc.stderr.readline.return_value = ""
        download_proc.stdout.read.return_value = ""
        download_proc.wait.return_value = 0
        # The AAC-copy decrypt Popen succeeds (-c copy master produced).
        decrypt_proc = mock.MagicMock()
        decrypt_proc.returncode = 0
        decrypt_proc.communicate.return_value = (b"", b"")

        temp_dir = "/tmp/x"
        entries = [
            _FakeDirEntry(f"{temp_dir}/book.aaxc"),
            _FakeDirEntry(f"{temp_dir}/book.voucher"),
            _FakeDirEntry(f"{temp_dir}/cover.jpg"),
            _FakeDirEntry(f"{temp_dir}/chapters.json"),
        ]

        # _run_registered is called for the metadata fetch, then the duration
        # probe. The AAXC+voucher path skips the AAX activation-bytes call.
        metadata_result = subprocess.CompletedProcess(["audible", "api"], 0, '{"item": {"runtime_length_min": 60}}', "")
        probe_result = subprocess.CompletedProcess(["ffprobe"], probe_returncode, "", "")

        voucher_json = '{"content_license": {"license_response": {"key": "K", "iv": "IV"}}}'

        with (
            mock.patch.object(ccl, "load_settings", return_value={}),
            mock.patch.object(ccl.subprocess, "Popen", side_effect=[download_proc, decrypt_proc]) as popen,
            mock.patch.object(ccl, "_run_registered", side_effect=[metadata_result, probe_result]) as run_registered,
            mock.patch.object(ccl.process_registry, "register"),
            mock.patch.object(ccl.process_registry, "unregister"),
            mock.patch.object(ccl.os, "scandir", side_effect=lambda _d: iter(entries)),
            mock.patch("builtins.open", mock.mock_open(read_data=voucher_json)),
            mock.patch.object(ccl, "_yield_progress"),
        ):
            result = ccl.prepare_book_assets("B0X", 1, temp_dir)
        return result, popen, run_registered

    def test_cancel_at_duration_probe_returns_cancel_without_flac_fallback(self):
        result, popen, run_registered = self._drive_to_probe(probe_returncode=-15)
        # Cancellation signal, not a failure.
        assert result == (None, None)
        # Exactly two Popens (download + the single AAC-copy decrypt): the FLAC
        # fallback strategy's decrypt was NOT started after the cancel.
        assert popen.call_count == 2
        # Only the metadata fetch and the (cancelled) duration probe ran.
        assert run_registered.call_count == 2


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


class TestBuildMp3Flags:
    """Phase 5: resolve the libmp3lame quality/rate flags from the mp3 settings
    block. Pure function; the source bitrate/sample rate are probed by the caller
    and passed in (either may be None)."""

    def test_vbr_default(self):
        # target == "quality" -> -q:a from vbr_quality; High -> level 0.
        flags = build_mp3_flags({"target": "quality", "vbr_quality": 2, "encoder_quality": "High"}, None, None)
        assert flags == ["-q:a", "2", "-compression_level", "0"]

    def test_vbr_custom_quality(self):
        flags = build_mp3_flags({"target": "quality", "vbr_quality": 5}, None, None)
        assert flags[:2] == ["-q:a", "5"]

    def test_cbr_no_abr_flag(self):
        # constant_bitrate True -> true CBR, no -abr; match off keeps fixed kbps.
        flags = build_mp3_flags(
            {"target": "bitrate", "bitrate_kbps": 192, "constant_bitrate": True, "match_source_bitrate": False},
            None,
            None,
        )
        assert flags == ["-b:a", "192k", "-compression_level", "0"]
        assert "-abr" not in flags

    def test_abr_adds_abr_flag(self):
        # constant_bitrate False -> ABR (adds -abr 1).
        flags = build_mp3_flags(
            {"target": "bitrate", "bitrate_kbps": 128, "constant_bitrate": False, "match_source_bitrate": False},
            None,
            None,
        )
        assert flags[:4] == ["-b:a", "128k", "-abr", "1"]

    def test_match_source_rounds_up_to_next_standard(self):
        # 130 kbps source -> next standard >= 130 is 160.
        flags = build_mp3_flags(
            {"target": "bitrate", "match_source_bitrate": True, "constant_bitrate": True}, 130000, None
        )
        assert flags[:2] == ["-b:a", "160k"]

    def test_match_source_exact_standard_kept(self):
        # 128 kbps source -> exactly 128 (>= itself), not bumped up.
        flags = build_mp3_flags(
            {"target": "bitrate", "match_source_bitrate": True, "constant_bitrate": True}, 128000, None
        )
        assert flags[:2] == ["-b:a", "128k"]

    def test_match_source_caps_at_320(self):
        # A source above the LAME ceiling clamps to 320.
        flags = build_mp3_flags(
            {"target": "bitrate", "match_source_bitrate": True, "constant_bitrate": True}, 400000, None
        )
        assert flags[:2] == ["-b:a", "320k"]

    def test_match_source_unknown_falls_back_to_fixed(self):
        # source bitrate None -> use the fixed bitrate_kbps as-is.
        flags = build_mp3_flags(
            {"target": "bitrate", "bitrate_kbps": 256, "match_source_bitrate": True, "constant_bitrate": True},
            None,
            None,
        )
        assert flags[:2] == ["-b:a", "256k"]

    def test_downsample_mono_adds_ac(self):
        flags = build_mp3_flags({"target": "quality", "downsample_mono": True}, None, None)
        assert "-ac" in flags and flags[flags.index("-ac") + 1] == "1"

    def test_sample_rate_gate_downsamples_when_source_exceeds(self):
        flags = build_mp3_flags({"target": "quality", "max_sample_rate": 44100}, None, 48000)
        assert "-ar" in flags and flags[flags.index("-ar") + 1] == "44100"

    def test_sample_rate_gate_no_upsample_at_or_below_cap(self):
        # Equal to the cap and below it both leave -ar off (never upsample).
        assert "-ar" not in build_mp3_flags({"target": "quality", "max_sample_rate": 44100}, None, 44100)
        assert "-ar" not in build_mp3_flags({"target": "quality", "max_sample_rate": 44100}, None, 22050)

    def test_sample_rate_gate_unknown_source_no_ar(self):
        assert "-ar" not in build_mp3_flags({"target": "quality", "max_sample_rate": 44100}, None, None)

    @pytest.mark.parametrize(
        ("encoder_quality", "level"),
        [("High", "0"), ("Standard", "2"), ("Fast", "7"), ("Nonsense", "0")],
    )
    def test_compression_level_mapping(self, encoder_quality, level):
        flags = build_mp3_flags({"target": "quality", "encoder_quality": encoder_quality}, None, None)
        assert flags[flags.index("-compression_level") + 1] == level

    def test_empty_settings_use_defaults(self):
        # A missing/empty mp3 block resolves to the documented defaults: VBR q2,
        # High effort, no mono, no sample-rate cap change.
        flags = build_mp3_flags({}, None, None)
        assert flags == ["-q:a", "2", "-compression_level", "0"]

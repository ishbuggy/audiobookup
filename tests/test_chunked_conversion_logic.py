# tests/test_chunked_conversion_logic.py

import io
import json
import os
import subprocess
import threading
from unittest import mock

import pytest

from audible_downloader import chunked_conversion_logic as ccl
from audible_downloader.chunked_conversion_logic import (
    _embed_cover_art,
    _read_brand_span,
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


class TestReadBrandSpan:
    """Phase 6: pull one brand intro/outro span out of the chapter JSON. The live
    audible-cli JSON uses camelCase; snake_case is a defensive fallback. Anything
    unusable must read as 0 — a bad value must never cut audio out of a book."""

    def test_prefers_camel_case(self):
        info = {"brandIntroDurationMs": 2043, "brand_intro_duration_ms": 999}
        assert _read_brand_span(info, "brandIntroDurationMs", "brand_intro_duration_ms") == 2043

    def test_falls_back_to_snake_case(self):
        info = {"brand_outro_duration_ms": 5061}
        assert _read_brand_span(info, "brandOutroDurationMs", "brand_outro_duration_ms") == 5061

    def test_missing_keys_are_zero(self):
        assert _read_brand_span({}, "brandIntroDurationMs", "brand_intro_duration_ms") == 0

    @pytest.mark.parametrize("raw", [None, "", "abc", {}])
    def test_unusable_values_are_zero(self, raw):
        assert _read_brand_span({"brandIntroDurationMs": raw}, "brandIntroDurationMs", "brand_intro_duration_ms") == 0

    def test_negative_value_clamps_to_zero(self):
        info = {"brandIntroDurationMs": -100}
        assert _read_brand_span(info, "brandIntroDurationMs", "brand_intro_duration_ms") == 0

    def test_numeric_string_is_accepted(self):
        info = {"brandIntroDurationMs": "2043"}
        assert _read_brand_span(info, "brandIntroDurationMs", "brand_intro_duration_ms") == 2043


class _FakeWriteHandle(io.StringIO):
    """Stand-in for a `with open(path, "w") as f:` target that captures what was
    written into a dict instead of touching disk."""

    def __init__(self, sink, key):
        super().__init__()
        self._sink = sink
        self._key = key

    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        self._sink[self._key] = self.getvalue()
        return False


def _capture_chunk_command(chunk_info, context):
    """Run encode_chapter_chunk against a mocked ffmpeg and return the command
    list it built."""
    proc = mock.MagicMock()
    proc.returncode = 0
    proc.communicate.return_value = (b"", b"")
    with (
        mock.patch.object(ccl, "load_settings", return_value={}),
        mock.patch.object(ccl.subprocess, "Popen", return_value=proc) as popen,
        mock.patch.object(ccl.process_registry, "register"),
        mock.patch.object(ccl.process_registry, "unregister"),
    ):
        ccl.encode_chapter_chunk("B0X", 1, "/tmp/x", chunk_info, context)
    return popen.call_args.args[0]


class TestBrandingTrimPipeline:
    """Phase 6 end-to-end through prepare_book_assets: extraction of the brand
    spans, the three-way gate, and the trimmed values flowing into the sanitize
    recompute and the returned context. Everything external (audible-cli, ffmpeg,
    ffprobe, the DB, the filesystem) is mocked, so this runs on the host."""

    TEMP_DIR = "/tmp/x"
    # 60 minutes of master, three even 10-minute chapters.
    TOTAL_SEC = "3600.0"
    CHAPTERS = [
        {"title": "One", "start_offset_ms": 0, "length_ms": 600_000},
        {"title": "Two", "start_offset_ms": 600_000, "length_ms": 600_000},
        {"title": "Three", "start_offset_ms": 1_200_000, "length_ms": 2_400_000},
    ]
    BOOK_INFO = {
        "item": {
            "title": "Test Book",
            "runtime_length_min": 60,
            "authors": [{"name": "An Author"}],
            "narrators": [{"name": "A Narrator"}],
            "copy_right": "(c) Somebody",
            "publisher_name": "A Publisher",
            "language": "english",
            "release_date": "2020-01-01",
            "merchandising_summary": "",
        }
    }

    def _run_prepare(
        self,
        settings,
        intro_ms=None,
        outro_ms=None,
        snake_case=False,
        chapters=None,
        total_sec=None,
        flac_master=False,
    ):
        """Drive prepare_book_assets to completion, returning (context, error,
        written_files). `intro_ms`/`outro_ms` are injected into the chapter JSON's
        chapter_info block (camelCase unless `snake_case`); None omits the key.
        `flac_master` fails the AAC-copy decrypt so the FLAC fallback produces the
        master instead."""
        intro_key, outro_key = ("brandIntroDurationMs", "brandOutroDurationMs")
        if snake_case:
            intro_key, outro_key = ("brand_intro_duration_ms", "brand_outro_duration_ms")
        chapter_info = {"chapters": self.CHAPTERS if chapters is None else chapters}
        if intro_ms is not None:
            chapter_info[intro_key] = intro_ms
        if outro_ms is not None:
            chapter_info[outro_key] = outro_ms
        chapter_json = json.dumps({"content_metadata": {"chapter_info": chapter_info}})
        voucher_json = '{"content_license": {"license_response": {"key": "K", "iv": "IV"}}}'

        total_sec = total_sec or self.TOTAL_SEC
        book_info = json.loads(json.dumps(self.BOOK_INFO))
        # Keep the API runtime consistent with the probed duration so the
        # post-decrypt integrity check passes. Under a minute it resolves to 0,
        # which disables that check entirely (the real code skips a falsy value).
        book_info["item"]["runtime_length_min"] = int(float(total_sec) // 60)

        # Download Popen: no progress lines, exits 0. Decrypt Popen: exits 0.
        download_proc = mock.MagicMock()
        download_proc.stderr.readline.return_value = ""
        download_proc.stdout.read.return_value = ""
        download_proc.wait.return_value = 0
        decrypt_proc = mock.MagicMock()
        decrypt_proc.returncode = 0
        decrypt_proc.communicate.return_value = (b"", b"")

        entries = [
            _FakeDirEntry(f"{self.TEMP_DIR}/book.aaxc"),
            _FakeDirEntry(f"{self.TEMP_DIR}/book.voucher"),
            _FakeDirEntry(f"{self.TEMP_DIR}/cover.jpg"),
            _FakeDirEntry(f"{self.TEMP_DIR}/chapters.json"),
        ]

        # _run_registered order: metadata fetch, post-decrypt duration probe,
        # seek verification (AAC-copy strategy only), Phase 2 total-duration probe.
        run_results = [
            subprocess.CompletedProcess(["audible", "api"], 0, json.dumps(book_info), ""),
            subprocess.CompletedProcess(["ffprobe"], 0, total_sec, ""),
            subprocess.CompletedProcess(["ffmpeg"], 0, "", ""),
            subprocess.CompletedProcess(["ffprobe"], 0, total_sec, ""),
            # Only an MP3 split reaches a fifth registered call (the source
            # bitrate/sample-rate probe its per-part LAME flags need). Every
            # other run leaves this entry unconsumed, so it changes nothing.
            subprocess.CompletedProcess(["ffprobe"], 0, "bit_rate=128000\nsample_rate=44100\n", ""),
        ]
        popen_procs = [download_proc, decrypt_proc]
        if flac_master:
            # The AAC-copy decrypt fails, so the FLAC strategy runs instead. FLAC
            # skips the seek verification, so that probe result drops out too.
            failed_decrypt = mock.MagicMock()
            failed_decrypt.returncode = 1
            failed_decrypt.communicate.return_value = (b"", b"boom")
            popen_procs = [download_proc, failed_decrypt, decrypt_proc]
            run_results.pop(2)

        written = {}

        def _fake_open(path, mode="r", *args, **kwargs):
            if "w" in mode:
                return _FakeWriteHandle(written, path)
            if path.endswith(".voucher"):
                return io.StringIO(voucher_json)
            if path.endswith(".json"):
                return io.StringIO(chapter_json)
            # Anything prepare wrote earlier in this run can be read back (the
            # split path slices its book-tag block out of chapters.txt).
            if path in written:
                return io.StringIO(written[path])
            raise AssertionError(f"unexpected open of {path}")

        db_con = mock.MagicMock()
        db_con.execute.return_value.fetchone.return_value = None  # no user overrides
        db_ctx = mock.MagicMock()
        db_ctx.__enter__.return_value = db_con

        with (
            mock.patch.object(ccl, "load_settings", return_value=settings),
            mock.patch.object(ccl.subprocess, "Popen", side_effect=popen_procs),
            mock.patch.object(ccl, "_run_registered", side_effect=run_results) as run_registered,
            mock.patch.object(ccl.process_registry, "register"),
            mock.patch.object(ccl.process_registry, "unregister"),
            mock.patch.object(ccl.os, "scandir", side_effect=lambda _d: iter(entries)),
            mock.patch.object(ccl.os, "remove"),
            mock.patch.object(ccl, "get_db_connection", return_value=db_ctx),
            mock.patch("builtins.open", side_effect=_fake_open),
            mock.patch.object(ccl, "_yield_progress"),
        ):
            context, error = ccl.prepare_book_assets("B0X", 1, self.TEMP_DIR)
        # How many of the side-effect entries above were actually consumed. The
        # list is deliberately one longer than most runs need, so a test that
        # cares about the fifth (MP3-only) probe pins this instead of trusting
        # that the entry stayed unconsumed everywhere else.
        self.run_registered_calls = run_registered.call_count
        return context, error, written

    @staticmethod
    def _chapter_txt(written):
        return written[f"{TestBrandingTrimPipeline.TEMP_DIR}/chapters.txt"]

    def test_trim_shifts_starts_and_shortens_final_chapter(self):
        settings = {"conversion": {"output_format": "m4b", "chapters": {"strip_audible_branding": True}}}
        context, error, written = self._run_prepare(settings, intro_ms=2_043, outro_ms=5_061)

        assert error is None
        starts = [c["start_offset_ms"] for c in context["chapters"]]
        lengths = [c["length_ms"] for c in context["chapters"]]
        assert starts == [0, 597_957, 1_197_957]
        # The outro shortening lands on the final chapter: its end is exactly the
        # effective (trimmed) total, 3_600_000 - 2_043 - 5_061.
        assert starts[-1] + lengths[-1] == 3_592_896
        assert context["total_duration_sec"] == 3_592.896
        assert context["trim_intro_ms"] == 2_043
        assert context["trim_outro_ms"] == 5_061
        # The FFMETADATA chapters carry the same trimmed timeline.
        assert "END=3592896\n" in self._chapter_txt(written)

    def test_intro_only_trim(self):
        settings = {"conversion": {"output_format": "mp3", "chapters": {"strip_audible_branding": True}}}
        context, error, _ = self._run_prepare(settings, intro_ms=2_000, outro_ms=0)
        assert error is None
        assert [c["start_offset_ms"] for c in context["chapters"]] == [0, 598_000, 1_198_000]
        assert context["total_duration_sec"] == 3_598.0
        assert context["trim_intro_ms"] == 2_000

    def test_outro_only_trim_leaves_starts_alone(self):
        settings = {"conversion": {"output_format": "m4b", "chapters": {"strip_audible_branding": True}}}
        context, error, _ = self._run_prepare(settings, intro_ms=0, outro_ms=5_000)
        assert error is None
        starts = [c["start_offset_ms"] for c in context["chapters"]]
        assert starts == [0, 600_000, 1_200_000]
        assert starts[-1] + context["chapters"][-1]["length_ms"] == 3_595_000
        assert context["trim_intro_ms"] == 0
        assert context["trim_outro_ms"] == 5_000

    def test_setting_off_is_untrimmed(self):
        # Default settings: brand spans present in the JSON, but the trim is off.
        settings = {"conversion": {"output_format": "m4b", "chapters": {}}}
        context, error, _ = self._run_prepare(settings, intro_ms=2_043, outro_ms=5_061)
        assert error is None
        assert [c["start_offset_ms"] for c in context["chapters"]] == [0, 600_000, 1_200_000]
        assert [c["length_ms"] for c in context["chapters"]] == [600_000, 600_000, 2_400_000]
        assert context["total_duration_sec"] == 3_600.0
        assert context["trim_intro_ms"] == 0
        assert context["trim_outro_ms"] == 0

    def test_original_format_is_never_trimmed(self):
        # The remux path copies the master through untouched, so the trim must not
        # apply even with the setting on and brand spans reported.
        settings = {"conversion": {"output_format": "original", "chapters": {"strip_audible_branding": True}}}
        context, error, _ = self._run_prepare(settings, intro_ms=2_043, outro_ms=5_061)
        assert error is None
        assert [c["start_offset_ms"] for c in context["chapters"]] == [0, 600_000, 1_200_000]
        assert context["total_duration_sec"] == 3_600.0
        assert context["trim_intro_ms"] == 0

    def test_legacy_no_reencode_flag_also_blocks_the_trim(self):
        # Old settings.json files carry no output_format, only no_reencode.
        settings = {"conversion": {"no_reencode": True, "chapters": {"strip_audible_branding": True}}}
        context, error, _ = self._run_prepare(settings, intro_ms=2_043, outro_ms=5_061)
        assert error is None
        assert [c["start_offset_ms"] for c in context["chapters"]] == [0, 600_000, 1_200_000]
        assert context["trim_intro_ms"] == 0

    def test_title_without_branding_is_untrimmed(self):
        # Setting on, but the chapter JSON reports no brand spans at all.
        settings = {"conversion": {"output_format": "m4b", "chapters": {"strip_audible_branding": True}}}
        context, error, _ = self._run_prepare(settings)
        assert error is None
        assert [c["start_offset_ms"] for c in context["chapters"]] == [0, 600_000, 1_200_000]
        assert context["total_duration_sec"] == 3_600.0
        assert context["trim_intro_ms"] == 0

    def test_snake_case_keys_are_honored(self):
        settings = {"conversion": {"output_format": "m4b", "chapters": {"strip_audible_branding": True}}}
        context, error, _ = self._run_prepare(settings, intro_ms=2_000, outro_ms=5_000, snake_case=True)
        assert error is None
        assert [c["start_offset_ms"] for c in context["chapters"]] == [0, 598_000, 1_198_000]
        assert context["total_duration_sec"] == 3_593.0
        assert context["trim_intro_ms"] == 2_000

    def test_implausibly_long_spans_are_refused(self):
        # W1: a combined span past a minute is corrupt chapter JSON, not
        # branding. The book must come out exactly as it would with the trim off.
        settings = {"conversion": {"output_format": "m4b", "chapters": {"strip_audible_branding": True}}}
        context, error, _ = self._run_prepare(settings, intro_ms=40_000, outro_ms=25_000)
        assert error is None
        assert [c["start_offset_ms"] for c in context["chapters"]] == [0, 600_000, 1_200_000]
        assert [c["length_ms"] for c in context["chapters"]] == [600_000, 600_000, 2_400_000]
        assert context["total_duration_sec"] == 3_600.0
        assert context["trim_intro_ms"] == 0
        assert context["trim_outro_ms"] == 0

    def test_spans_swallowing_the_whole_book_are_refused(self):
        # W1: spans that reach (or pass) the book's own length would leave a
        # negative effective total — zero-length chapters / a negative MP3 -t.
        settings = {"conversion": {"output_format": "m4b", "chapters": {"strip_audible_branding": True}}}
        short_book = [{"title": "One", "start_offset_ms": 0, "length_ms": 30_000}]
        context, error, _ = self._run_prepare(
            settings, intro_ms=20_000, outro_ms=15_000, chapters=short_book, total_sec="30.0"
        )
        assert error is None
        assert context["chapters"] == [{"title": "One", "start_offset_ms": 0, "length_ms": 30_000}]
        assert context["total_duration_sec"] == 30.0
        assert context["trim_intro_ms"] == 0

    def test_auto_chunked_book_chunks_within_the_trimmed_timeline(self):
        # A chapterless/single-chapter book long enough to auto-chunk: the
        # synthetic "Part N" spans must be cut from the TRIMMED timeline, and the
        # per-chunk seek must add the intro back to reach the right source audio.
        settings = {"conversion": {"output_format": "m4b", "chapters": {"strip_audible_branding": True}}}
        single = [{"title": "Chapter 1", "start_offset_ms": 0, "length_ms": 3_600_000}]
        context, error, _ = self._run_prepare(settings, intro_ms=2_043, outro_ms=5_061, chapters=single)

        assert error is None
        effective_total_ms = 3_600_000 - 2_043 - 5_061
        chunks = context["chapters"]
        assert [c["title"] for c in chunks] == ["Part 1", "Part 2", "Part 3", "Part 4"]
        assert [c["start_offset_ms"] for c in chunks] == [0, 900_000, 1_800_000, 2_700_000]
        # No chunk may run past the end of the trimmed output, and the last one
        # ends exactly there.
        for chunk in chunks:
            assert chunk["start_offset_ms"] + chunk["length_ms"] <= effective_total_ms
        assert chunks[-1]["start_offset_ms"] + chunks[-1]["length_ms"] == pytest.approx(effective_total_ms, abs=1)
        assert context["total_duration_sec"] == pytest.approx(effective_total_ms / 1000.0)

        # The final chunk, as processing_logic would submit it.
        chunk_info = {
            "index": 3,
            "total_chunks": 4,
            "start": chunks[-1]["start_offset_ms"] / 1000.0,
            "duration": chunks[-1]["length_ms"] / 1000.0,
        }
        cmd = _capture_chunk_command(chunk_info, context)
        assert float(cmd[cmd.index("-ss") + 1]) == pytest.approx(2_702.043)

    def test_marker_inside_the_outro_is_dropped_from_the_output(self):
        # v0.23.0 regression: a marker sitting inside the brand outro used to
        # survive the trim, so the chapter before it ran past the trim boundary
        # (its chunk encode read outro audio) and the marker itself reached the
        # FFMETADATA list with a start beyond the end of the output.
        settings = {"conversion": {"output_format": "m4b", "chapters": {"strip_audible_branding": True}}}
        chapters = [
            {"title": "One", "start_offset_ms": 0, "length_ms": 600_000},
            {"title": "Two", "start_offset_ms": 600_000, "length_ms": 2_997_000},
            {"title": "Outro Marker", "start_offset_ms": 3_597_000, "length_ms": 3_000},
        ]
        context, error, written = self._run_prepare(settings, intro_ms=2_043, outro_ms=5_061, chapters=chapters)

        assert error is None
        effective_total_ms = 3_600_000 - 2_043 - 5_061
        out = context["chapters"]
        assert [c["title"] for c in out] == ["One", "Two"]
        # The chapter that preceded the dropped marker now ends exactly at the
        # trim boundary instead of running into the outro.
        assert out[-1]["start_offset_ms"] + out[-1]["length_ms"] == effective_total_ms
        assert f"END={effective_total_ms}\n" in self._chapter_txt(written)
        assert "Outro Marker" not in self._chapter_txt(written)

    def test_chapter_metadata_overrunning_the_master_is_capped(self):
        # The same normalization with the trim OFF: a start past the master's real
        # duration must not stretch the preceding chapter's chunk beyond the end
        # of the audio. The overrunning entry itself sanitizes to zero length and
        # is dropped.
        settings = {"conversion": {"output_format": "m4b"}}
        chapters = [
            {"title": "One", "start_offset_ms": 0, "length_ms": 600_000},
            {"title": "Two", "start_offset_ms": 600_000, "length_ms": 600_000},
            {"title": "Three", "start_offset_ms": 1_200_000, "length_ms": 2_500_000},
            {"title": "Overrun", "start_offset_ms": 3_700_000, "length_ms": 60_000},
        ]
        context, error, _ = self._run_prepare(settings, chapters=chapters)

        assert error is None
        out = context["chapters"]
        assert [c["title"] for c in out] == ["One", "Two", "Three"]
        assert out[-1]["length_ms"] == 2_400_000
        assert out[-1]["start_offset_ms"] + out[-1]["length_ms"] == 3_600_000

    def test_flac_fallback_master_trims_identically(self):
        # The trim math is master-agnostic: a title whose AAC-copy decrypt fell
        # back to FLAC takes the same re-encode path and the same timeline.
        settings = {"conversion": {"output_format": "m4b", "chapters": {"strip_audible_branding": True}}}
        context, error, _ = self._run_prepare(settings, intro_ms=2_043, outro_ms=5_061, flac_master=True)
        assert error is None
        assert context["audio_file"].endswith(".flac")
        assert [c["start_offset_ms"] for c in context["chapters"]] == [0, 597_957, 1_197_957]
        assert context["total_duration_sec"] == 3_592.896
        assert context["trim_intro_ms"] == 2_043


def _split_settings(minimum_file_duration=0, output_format="m4b", **chapter_overrides):
    """Settings block with per-chapter splitting on, for the prepare-level
    split-decision tests."""
    chapters = {"split_by_chapter": True, "minimum_file_duration": minimum_file_duration}
    chapters.update(chapter_overrides)
    return {"conversion": {"output_format": output_format, "chapters": chapters}}


class TestSplitDecision:
    """v0.24.0 Phase 2, D6/D7: prepare_book_assets decides whether a book is
    split into one file per chapter, applies the minimum-duration merge that
    goes with it, and carries the decision out in the context. With the setting
    off — the default — none of it fires and the context is today's."""

    # The same fully-mocked drive of prepare_book_assets as the branding-trim
    # tests, borrowed rather than duplicated so both exercise one harness.
    TEMP_DIR = TestBrandingTrimPipeline.TEMP_DIR
    TOTAL_SEC = TestBrandingTrimPipeline.TOTAL_SEC
    CHAPTERS = TestBrandingTrimPipeline.CHAPTERS
    BOOK_INFO = TestBrandingTrimPipeline.BOOK_INFO
    _run_prepare = TestBrandingTrimPipeline._run_prepare

    def test_setting_off_never_splits(self):
        context, error, _ = self._run_prepare({"conversion": {"output_format": "m4b", "chapters": {}}})
        assert error is None
        assert context["split_output"] is False
        assert context["part_titles"] is None
        assert context["book_tags_file"] is None
        assert [c["title"] for c in context["chapters"]] == ["One", "Two", "Three"]

    def test_eligible_book_splits(self):
        context, error, _ = self._run_prepare(_split_settings())
        assert error is None
        assert context["split_output"] is True
        assert len(context["chapters"]) == 3
        # The part titles are the strings the chapter atoms carry, so the two
        # renderings can never disagree.
        assert context["part_titles"] == ["One", "Two", "Three"]
        assert context["book_tags_file"] == f"{self.TEMP_DIR}/book_tags.txt"

    def test_part_titles_follow_the_chapter_title_template(self):
        context, _error, _ = self._run_prepare(
            _split_settings(chapter_title_template="{ch} of {ch_total} - {ch_title}")
        )
        assert context["part_titles"] == ["1 of 3 - One", "2 of 3 - Two", "3 of 3 - Three"]

    def test_short_chapters_are_merged_forward_before_splitting(self):
        # A 2-second announcement marker in front of a real chapter: with the
        # minimum at 3 seconds it folds forward, so three chapters become two
        # files instead of one unplayable fragment plus two books' worth.
        chapters = [
            {"title": "Chapter Nineteen", "start_offset_ms": 0, "length_ms": 2_000},
            {"title": "The Heist", "start_offset_ms": 2_000, "length_ms": 1_798_000},
            {"title": "After", "start_offset_ms": 1_800_000, "length_ms": 1_800_000},
        ]
        context, error, _ = self._run_prepare(_split_settings(minimum_file_duration=3), chapters=chapters)
        assert error is None
        assert context["split_output"] is True
        assert [c["title"] for c in context["chapters"]] == ["Chapter Nineteen The Heist", "After"]

    def test_zero_minimum_disables_the_merge(self):
        chapters = [
            {"title": "Blip", "start_offset_ms": 0, "length_ms": 2_000},
            {"title": "Real", "start_offset_ms": 2_000, "length_ms": 3_598_000},
        ]
        context, _error, _ = self._run_prepare(_split_settings(minimum_file_duration=0), chapters=chapters)
        assert context["split_output"] is True
        assert [c["title"] for c in context["chapters"]] == ["Blip", "Real"]

    @pytest.mark.parametrize("junk", ["", "abc", None, {}])
    def test_unusable_minimum_is_treated_as_no_merge(self, junk):
        # A hand-edited settings.json must not raise its way through prepare.
        chapters = [
            {"title": "Blip", "start_offset_ms": 0, "length_ms": 2_000},
            {"title": "Real", "start_offset_ms": 2_000, "length_ms": 3_598_000},
        ]
        context, error, _ = self._run_prepare(_split_settings(minimum_file_duration=junk), chapters=chapters)
        assert error is None
        assert context["split_output"] is True
        assert len(context["chapters"]) == 2

    def test_an_out_of_range_minimum_is_clamped_to_the_settings_maximum(self):
        # Review M3: the settings input offers 0..3600 seconds but a hand-edited
        # settings.json (or, before the client-side guard, a typo) could carry
        # anything. Unclamped, an absurd minimum merges every chapter forward
        # until nothing is left to split and the book quietly converts to a single
        # file with the toggle still on. Two 2-hour chapters stay two files.
        chapters = [
            {"title": "One", "start_offset_ms": 0, "length_ms": 7_200_000},
            {"title": "Two", "start_offset_ms": 7_200_000, "length_ms": 7_200_000},
        ]
        context, error, _ = self._run_prepare(
            _split_settings(minimum_file_duration=100_000), chapters=chapters, total_sec="14400.0"
        )
        assert error is None
        assert context["split_output"] is True
        assert [c["title"] for c in context["chapters"]] == ["One", "Two"]

    def test_single_chapter_book_is_not_split(self):
        # Short enough that auto-chunking doesn't fire either, so this really is
        # the "fewer than two chapters" gate.
        chapters = [{"title": "Only", "start_offset_ms": 0, "length_ms": 600_000}]
        context, error, _ = self._run_prepare(_split_settings(), chapters=chapters, total_sec="600.0")
        assert error is None
        assert context["split_output"] is False
        assert context["book_tags_file"] is None

    def test_merge_collapsing_to_one_chapter_does_not_split_or_merge(self):
        # Two chapters, the first below the minimum: the merge would leave a
        # single chapter, which is not a split — so the book goes out as one
        # file with the chapter markers every other single-file conversion gets.
        chapters = [
            {"title": "Tiny", "start_offset_ms": 0, "length_ms": 2_000},
            {"title": "Rest", "start_offset_ms": 2_000, "length_ms": 3_598_000},
        ]
        context, error, _ = self._run_prepare(_split_settings(minimum_file_duration=600), chapters=chapters)
        assert error is None
        assert context["split_output"] is False
        assert [c["title"] for c in context["chapters"]] == ["Tiny", "Rest"]

    def test_auto_chunked_book_is_not_split(self):
        # A chapterless 60-minute title becomes synthetic "Part N" markers to
        # give the encode parallel work; those are not chapters and must not
        # become files (D7).
        chapters = [{"title": "Whole Book", "start_offset_ms": 0, "length_ms": 3_600_000}]
        context, error, _ = self._run_prepare(_split_settings(), chapters=chapters)
        assert error is None
        assert context["split_output"] is False
        assert [c["title"] for c in context["chapters"]][:2] == ["Part 1", "Part 2"]

    def test_aac_output_splits_with_reencoded_parts(self):
        context, _error, _ = self._run_prepare(_split_settings(output_format="m4b"))
        assert context["split_output"] is True
        assert context["split_encode_mode"] == "aac"
        # Only the MP3 variant needs the master's own audio parameters.
        assert context["mp3_source_bitrate_bps"] is None
        assert context["mp3_source_sample_rate"] is None

    def test_mp3_output_splits_with_lame_parts(self):
        # Phase 3: the format no longer blocks the split — MP3 gets N LAME
        # encodes in place of its single pass.
        context, error, _ = self._run_prepare(_split_settings(output_format="mp3"))
        assert error is None
        assert context["split_output"] is True
        assert context["split_encode_mode"] == "mp3"
        assert [c["title"] for c in context["chapters"]] == ["One", "Two", "Three"]

    def test_mp3_split_probes_the_master_once_for_the_whole_book(self):
        # One probe, carried in the context: N parts probing independently would
        # let a single failed probe give one book parts at two bitrates.
        context, _error, _ = self._run_prepare(_split_settings(output_format="mp3"))
        assert context["mp3_source_bitrate_bps"] == 128_000
        assert context["mp3_source_sample_rate"] == 44_100
        # Exactly one registered subprocess more than the non-MP3 prepare path
        # runs: the four shared ones plus this probe. Pinning the count means a
        # future fifth call on the shared path can't hide in the spare entry.
        assert self.run_registered_calls == 5

    def test_mp3_split_source_probe_cancellation_is_a_clean_cancel(self):
        # A SIGTERMed probe is the job stopping, not an unreadable master: the
        # book must come back as a cancel (None, None), never as an ERROR.
        with mock.patch.object(ccl, "_probe_source_audio_params", side_effect=ccl._ProbeCancelled):
            context, error, _ = self._run_prepare(_split_settings(output_format="mp3"))
        assert context is None
        assert error is None

    def test_lossless_remux_splits_with_copy_cut_parts(self):
        # D14 passed, so the lossless path splits too — by cutting the AAC
        # master with "-c copy" rather than re-encoding it.
        settings = _split_settings(output_format="original")
        context, error, _ = self._run_prepare(settings)
        assert error is None
        assert context["audio_file"].endswith(".m4b")
        assert context["split_output"] is True
        assert context["split_encode_mode"] == "copy"

    def test_lossless_falling_back_to_flac_splits_by_re_encoding(self):
        # A FLAC master can't be cut into .m4b parts with -c copy, so that title
        # takes the AAC re-encode path after all — the D14 spike's stated scope
        # limit, and the behavior Phase 2 already had.
        settings = _split_settings(output_format="original")
        context, error, _ = self._run_prepare(settings, flac_master=True)
        assert error is None
        assert context["audio_file"].endswith(".flac")
        assert context["split_output"] is True
        assert context["split_encode_mode"] == "aac"

    @pytest.mark.parametrize("output_format", ["m4b", "mp3", "original"])
    def test_auto_chunked_book_is_not_split_in_any_format(self, output_format):
        # D7 holds for all three variants: synthetic "Part N" markers exist to
        # give the encode parallel work and must never become files.
        chapters = [{"title": "Whole Book", "start_offset_ms": 0, "length_ms": 3_600_000}]
        context, error, _ = self._run_prepare(_split_settings(output_format=output_format), chapters=chapters)
        assert error is None
        assert context["split_output"] is False
        assert context["split_encode_mode"] is None

    @pytest.mark.parametrize("output_format", ["m4b", "mp3", "original"])
    def test_setting_off_never_splits_in_any_format(self, output_format):
        context, error, _ = self._run_prepare({"conversion": {"output_format": output_format, "chapters": {}}})
        assert error is None
        assert context["split_output"] is False
        assert context["split_encode_mode"] is None
        assert [c["title"] for c in context["chapters"]] == ["One", "Two", "Three"]

    def test_book_tags_file_drops_the_chapters_and_the_book_title(self):
        _context, _error, written = self._run_prepare(_split_settings())
        tags = written[f"{self.TEMP_DIR}/book_tags.txt"]
        assert tags.startswith(";FFMETADATA1\n")
        # No chapter atoms: in split mode the file IS the chapter (D8).
        assert "[CHAPTER]" not in tags
        # The book's own title line is gone — each part writes its own — but the
        # album (which groups the parts in a player) and the rest stay.
        assert "\ntitle=" not in f"\n{tags}"
        assert "album=Test Book\n" in tags
        assert "artist=An Author\n" in tags
        assert "AUDIBLE_ASIN=B0X\n" in tags


class TestWriteBookTagsFile:
    """The book-tag slice is line-oriented and stops at the first chapter atom."""

    def test_slices_the_header_and_drops_title(self, tmp_path):
        source = tmp_path / "chapters.txt"
        source.write_text(
            ";FFMETADATA1\ntitle=Book\nalbum=Book\nartist=A\n[CHAPTER]\nTIMEBASE=1/1000\ntitle=One\n",
            encoding="utf-8",
        )
        path = ccl._write_book_tags_file(str(tmp_path), str(source))
        assert path == str(tmp_path / "book_tags.txt")
        assert (tmp_path / "book_tags.txt").read_text(encoding="utf-8") == ";FFMETADATA1\nalbum=Book\nartist=A\n"


class TestEncodeChapterChunkSplitTagging:
    """D8: in split mode each chunk is one of the book's output files, so it is
    tagged at encode time from a per-part FFMETADATA input instead of having its
    metadata stripped. With splitting off the command is unchanged."""

    MASTER = "/tmp/x/master_intermediate.m4b"

    def _context(self, tmp_path, **overrides):
        book_tags = tmp_path / "book_tags.txt"
        book_tags.write_text(";FFMETADATA1\nalbum=Test Book\nartist=An Author\n", encoding="utf-8")
        context = {
            "decryption_args": [],
            "audio_file": self.MASTER,
            "split_output": True,
            "part_titles": ["One", "Two", "Three"],
            "book_tags_file": str(book_tags),
        }
        context.update(overrides)
        return context

    def _encode(self, tmp_path, context, index=1):
        chunk_info = {"index": index, "total_chunks": 3, "start": 600.0, "duration": 600.0}
        proc = mock.MagicMock()
        proc.returncode = 0
        proc.communicate.return_value = (b"", b"")
        with (
            mock.patch.object(ccl, "load_settings", return_value={}),
            mock.patch.object(ccl.subprocess, "Popen", return_value=proc) as popen,
            mock.patch.object(ccl.process_registry, "register"),
            mock.patch.object(ccl.process_registry, "unregister"),
        ):
            result = ccl.encode_chapter_chunk("B0X", 1, str(tmp_path), chunk_info, context)
        return result, popen.call_args.args[0]

    def test_split_chunk_is_tagged_from_a_metadata_input(self, tmp_path):
        _result, cmd = self._encode(tmp_path, self._context(tmp_path))
        assert "-map_metadata" in cmd
        assert cmd[cmd.index("-map_metadata") + 1] == "1"
        assert cmd[cmd.index("-movflags") + 1] == "+faststart+use_metadata_tags"

    def test_split_chunk_drops_the_masters_chapter_atoms(self, tmp_path):
        # Regression from the Phase 2 dev-container smoke: without an explicit
        # "-map_chapters -1", ffmpeg copies the chapter atoms of the first input
        # that has any — the decrypted master, when the AAC Copy strategy
        # preserved them — sliced nonsensically against the chunk's -ss/-t
        # window. A part carries no chapter atoms at all: the file IS the
        # chapter (D8).
        _result, cmd = self._encode(tmp_path, self._context(tmp_path))
        assert "-map_chapters" in cmd
        assert cmd[cmd.index("-map_chapters") + 1] == "-1"

    def test_metadata_input_precedes_the_duration_cap(self, tmp_path):
        # Load-bearing ordering: ffmpeg reads options positionally, so a "-i"
        # after "-t" would make the duration an INPUT option on the metadata
        # file and let the chunk run to the end of the master.
        _result, cmd = self._encode(tmp_path, self._context(tmp_path))
        assert cmd.index(str(tmp_path / "chunk_001.ffmeta")) < cmd.index("-t")
        assert cmd[cmd.index("-t") + 1] == "600.0"

    def test_part_metadata_carries_the_book_tags_title_and_track(self, tmp_path):
        self._encode(tmp_path, self._context(tmp_path))
        written = (tmp_path / "chunk_001.ffmeta").read_text(encoding="utf-8")
        assert written.startswith(";FFMETADATA1\n")
        assert "album=Test Book\n" in written
        assert "artist=An Author\n" in written
        # 1-based part number, and the total, so players order the files.
        assert "title=Two\n" in written
        assert "track=2/3\n" in written
        assert "[CHAPTER]" not in written

    def test_missing_book_tag_block_still_produces_a_tagged_part(self, tmp_path):
        context = self._context(tmp_path, book_tags_file=str(tmp_path / "gone.txt"))
        result, _cmd = self._encode(tmp_path, context)
        assert result is not None
        written = (tmp_path / "chunk_001.ffmeta").read_text(encoding="utf-8")
        assert written == ";FFMETADATA1\ntitle=Two\ntrack=2/3\n"

    def test_unwritable_metadata_fails_the_chunk(self, tmp_path):
        # An untagged part is not an acceptable output, so the chunk fails —
        # which fails the book — rather than encoding a nameless file.
        chunk_info = {"index": 1, "total_chunks": 3, "start": 600.0, "duration": 600.0}
        with (
            mock.patch.object(ccl, "load_settings", return_value={}),
            mock.patch.object(ccl, "_write_part_metadata", return_value=None),
            mock.patch.object(ccl.subprocess, "Popen") as popen,
        ):
            result = ccl.encode_chapter_chunk("B0X", 1, str(tmp_path), chunk_info, self._context(tmp_path))
        assert result is None
        popen.assert_not_called()

    def test_splitting_off_leaves_the_command_untouched(self, tmp_path):
        context = {"decryption_args": [], "audio_file": self.MASTER}
        _result, cmd = self._encode(tmp_path, context)
        assert cmd == [
            "ffmpeg",
            "-y",
            "-hide_banner",
            "-loglevel",
            "error",
            "-ss",
            "600.0",
            "-i",
            self.MASTER,
            "-t",
            "600.0",
            "-map",
            "0:a",
            "-c:a",
            "aac",
            "-b:a",
            "128k",
            "-map_metadata",
            "-1",
            str(tmp_path / "chunk_001.m4b"),
        ]
        assert not (tmp_path / "chunk_001.ffmeta").exists()


class TestEncodeChapterChunkSplitVariants:
    """v0.24.0 Phase 3: the same cut, three ways. "copy" cuts the AAC master
    without re-encoding it, "mp3" runs LAME per part and muxes the cover inline,
    and "aac" is Phase 2's re-encode, unchanged."""

    MASTER = "/tmp/x/master_intermediate.m4b"
    CHUNK = {"index": 1, "total_chunks": 3, "start": 600.0, "duration": 600.0}

    def _context(self, tmp_path, mode, cover_file=None, **overrides):
        book_tags = tmp_path / "book_tags.txt"
        book_tags.write_text(";FFMETADATA1\nalbum=Test Book\nartist=An Author\n", encoding="utf-8")
        context = {
            "decryption_args": [],
            "audio_file": self.MASTER,
            "split_output": True,
            "split_encode_mode": mode,
            "part_titles": ["One", "Two", "Three"],
            "book_tags_file": str(book_tags),
            "cover_file": cover_file,
        }
        context.update(overrides)
        return context

    def _encode(self, tmp_path, context, settings=None):
        proc = mock.MagicMock()
        proc.returncode = 0
        proc.communicate.return_value = (b"", b"")
        with (
            mock.patch.object(ccl, "load_settings", return_value=settings or {}),
            mock.patch.object(ccl.subprocess, "Popen", return_value=proc) as popen,
            mock.patch.object(ccl.process_registry, "register"),
            mock.patch.object(ccl.process_registry, "unregister"),
        ):
            result = ccl.encode_chapter_chunk("B0X", 1, str(tmp_path), self.CHUNK, context)
        return result, popen.call_args.args[0]

    # --- Lossless ("copy") ------------------------------------------------

    def test_copy_cut_is_the_whole_command(self, tmp_path):
        # Pinned whole, because the spike proved this exact shape accurate to
        # within 8 ms: "-ss" BEFORE the input (input seeking, which is what
        # makes it both fast and frame-accurate) and "-t" after it.
        _result, cmd = self._encode(tmp_path, self._context(tmp_path, "copy"))
        assert cmd == [
            "ffmpeg",
            "-y",
            "-hide_banner",
            "-loglevel",
            "error",
            "-ss",
            "600.0",
            "-i",
            self.MASTER,
            "-i",
            str(tmp_path / "chunk_001.ffmeta"),
            "-t",
            "600.0",
            "-map",
            "0:a",
            "-map_metadata",
            "1",
            "-map_chapters",
            "-1",
            "-c",
            "copy",
            "-movflags",
            "+faststart+use_metadata_tags",
            str(tmp_path / "chunk_001.m4b"),
        ]

    def test_copy_cut_carries_no_encoder_flags(self, tmp_path):
        # The whole point of the lossless variant: nothing re-encodes, whatever
        # the AAC quality setting says.
        settings = {"conversion": {"quality": "Low"}}
        _result, cmd = self._encode(tmp_path, self._context(tmp_path, "copy"), settings=settings)
        assert "-c:a" not in cmd
        assert "-b:a" not in cmd
        assert "aac" not in cmd

    def test_copy_cut_still_drops_the_masters_chapter_track(self, tmp_path):
        # Load-bearing for a second reason here (D14 spike): the AAC-copy master
        # carries a QuickTime chapter TRACK, and copying without this leaves a
        # dangling tref/chap reference that warns on every later read.
        _result, cmd = self._encode(tmp_path, self._context(tmp_path, "copy"))
        assert cmd[cmd.index("-map_chapters") + 1] == "-1"

    def test_copy_cut_never_inlines_a_cover(self, tmp_path):
        # mp4 can't hold an attached picture next to +use_metadata_tags, so the
        # cover still comes from AtomicParsley at promotion time.
        cover = tmp_path / "cover.jpg"
        cover.write_bytes(b"jpeg")
        _result, cmd = self._encode(tmp_path, self._context(tmp_path, "copy", cover_file=str(cover)))
        assert str(cover) not in cmd
        assert "attached_pic" not in cmd

    # --- MP3 --------------------------------------------------------------

    def test_mp3_part_is_the_whole_command(self, tmp_path):
        cover = tmp_path / "cover.jpg"
        cover.write_bytes(b"jpeg")
        context = self._context(
            tmp_path,
            "mp3",
            cover_file=str(cover),
            mp3_source_bitrate_bps=None,
            mp3_source_sample_rate=None,
        )
        _result, cmd = self._encode(tmp_path, context)
        assert cmd == [
            "ffmpeg",
            "-y",
            "-hide_banner",
            "-loglevel",
            "error",
            "-ss",
            "600.0",
            "-i",
            self.MASTER,
            "-i",
            str(tmp_path / "chunk_001.ffmeta"),
            "-i",
            str(cover),
            "-t",
            "600.0",
            "-map",
            "0:a",
            "-map_metadata",
            "1",
            "-map_chapters",
            "-1",
            "-map",
            "2:v",
            "-c:v",
            "copy",
            "-disposition:v",
            "attached_pic",
            "-metadata:s:v",
            "title=Album cover",
            "-metadata:s:v",
            "comment=Cover (front)",
            "-c:a",
            "libmp3lame",
            "-q:a",
            "2",
            "-compression_level",
            "0",
            "-id3v2_version",
            "3",
            str(tmp_path / "chunk_001.mp3"),
        ]

    def test_mp3_part_reuses_the_shared_lame_flag_matrix(self, tmp_path):
        # Same resolver the single-pass encoder uses, fed the source parameters
        # prepare probed once for the book.
        settings = {
            "conversion": {
                "mp3": {"target": "bitrate", "bitrate_kbps": 96, "match_source_bitrate": True, "max_sample_rate": 22050}
            }
        }
        context = self._context(tmp_path, "mp3", mp3_source_bitrate_bps=128_000, mp3_source_sample_rate=44_100)
        _result, cmd = self._encode(tmp_path, context, settings=settings)
        expected = ccl.build_mp3_flags(settings["conversion"]["mp3"], 128_000, 44_100)
        start = cmd.index("libmp3lame") + 1
        assert cmd[start : start + len(expected)] == expected
        assert expected[:2] == ["-b:a", "128k"]

    def test_mp3_part_carries_no_mp4_flags(self, tmp_path):
        # "-movflags" is an mp4 muxer option; ffmpeg rejects it for an .mp3.
        _result, cmd = self._encode(tmp_path, self._context(tmp_path, "mp3"))
        assert "-movflags" not in cmd
        assert cmd[-1].endswith(".mp3")

    def test_mp3_part_without_a_cover_maps_no_picture(self, tmp_path):
        _result, cmd = self._encode(tmp_path, self._context(tmp_path, "mp3"))
        assert "attached_pic" not in cmd
        assert "2:v" not in cmd
        # ...and the metadata input is still the only extra one.
        assert cmd.count("-i") == 2

    def test_mp3_part_tags_come_from_the_same_metadata_writer(self, tmp_path):
        self._encode(tmp_path, self._context(tmp_path, "mp3"))
        written = (tmp_path / "chunk_001.ffmeta").read_text(encoding="utf-8")
        assert "album=Test Book\n" in written
        assert "title=Two\n" in written
        assert "track=2/3\n" in written
        assert "[CHAPTER]" not in written

    # --- AAC (Phase 2, unchanged) -----------------------------------------

    def test_aac_mode_is_phase_twos_command(self, tmp_path):
        _result, cmd = self._encode(tmp_path, self._context(tmp_path, "aac"))
        assert cmd[cmd.index("-c:a") + 1] == "aac"
        assert cmd[cmd.index("-movflags") + 1] == "+faststart+use_metadata_tags"
        assert cmd[-1] == str(tmp_path / "chunk_001.m4b")

    def test_missing_mode_key_still_re_encodes_as_aac(self, tmp_path):
        # Defensive: a context that predates the key must not raise or silently
        # produce an uncut copy.
        context = self._context(tmp_path, "aac")
        del context["split_encode_mode"]
        _result, cmd = self._encode(tmp_path, context)
        assert cmd[cmd.index("-c:a") + 1] == "aac"


class TestEncodeChapterChunkTrim:
    """Phase 6 seek compensation: chunk starts are output-timeline, so the source
    seek adds the intro span back. With no trim the command must be identical to
    what it has always been."""

    CHUNK = {"index": 0, "total_chunks": 3, "start": 597.957, "duration": 600.0}
    MASTER = "/tmp/x/master_intermediate.m4b"

    def _run(self, context):
        return _capture_chunk_command(self.CHUNK, context)

    def test_no_trim_seeks_to_the_chunk_start(self):
        # Pinned as a whole command: with the trim inactive this must stay
        # byte-for-byte what the AAC path has always run.
        context = {"decryption_args": [], "audio_file": self.MASTER}
        cmd = self._run(context)
        assert cmd == [
            "ffmpeg",
            "-y",
            "-hide_banner",
            "-loglevel",
            "error",
            "-ss",
            "597.957",
            "-i",
            self.MASTER,
            "-t",
            "600.0",
            "-map",
            "0:a",
            "-c:a",
            "aac",
            "-b:a",
            "128k",
            "-map_metadata",
            "-1",
            "/tmp/x/chunk_000.m4b",
        ]

    def test_trim_adds_the_intro_back_to_the_seek(self):
        context = {
            "decryption_args": [],
            "audio_file": self.MASTER,
            "trim_intro_ms": 2_043,
        }
        cmd = self._run(context)
        assert cmd[cmd.index("-ss") + 1] == "600.0"
        # Duration is untouched — it already comes from the trimmed chapter length.
        assert cmd[cmd.index("-t") + 1] == "600.0"


class TestEncodeBookMp3Trim:
    """Phase 6: the single-pass MP3 encode gains an input-side -ss (past the brand
    intro) and an output-side -t (the trimmed length) only when the trim is active."""

    MASTER = "/tmp/x/master_intermediate.m4b"
    CHAPTER_FILE = "/tmp/x/chapters.txt"

    def _run(self, context):
        proc = mock.MagicMock()
        proc.stdout.readline.return_value = ""
        proc.stderr.read.return_value = ""
        proc.wait.return_value = 0
        with (
            mock.patch.object(ccl, "load_settings", return_value={}),
            mock.patch.object(ccl, "_probe_source_audio_params", return_value=(None, None)),
            mock.patch.object(ccl.subprocess, "Popen", return_value=proc) as popen,
            mock.patch.object(ccl.process_registry, "register"),
            mock.patch.object(ccl.process_registry, "unregister"),
            mock.patch.object(ccl, "_yield_progress"),
            # The encode writes to "<final>.part" and renames on success; there is
            # no real file here, so the promotion is stubbed out.
            mock.patch.object(ccl.os, "replace"),
        ):
            result = ccl.encode_book_mp3("B0X", 1, "/tmp/x", "/data/out.mp3", context)
        return result, popen.call_args.args[0]

    def test_inactive_trim_leaves_the_command_untouched(self):
        context = {
            "audio_file": self.MASTER,
            "chapter_file": self.CHAPTER_FILE,
            "cover_file": None,
            "total_duration_sec": 3_600.0,
            "trim_intro_ms": 0,
            "trim_outro_ms": 0,
        }
        result, cmd = self._run(context)
        assert result is True
        assert cmd == [
            "ffmpeg",
            "-y",
            "-hide_banner",
            "-loglevel",
            "error",
            "-progress",
            "pipe:1",
            "-i",
            self.MASTER,
            "-i",
            self.CHAPTER_FILE,
            "-map",
            "0:a",
            "-map_metadata",
            "1",
            "-map_chapters",
            "1",
            "-c:a",
            "libmp3lame",
            "-q:a",
            "2",
            "-compression_level",
            "0",
            "-id3v2_version",
            "3",
            "-f",
            "mp3",
            "/data/out.mp3.part",
        ]

    def test_context_without_trim_keys_is_also_untouched(self):
        # An in-flight job whose context predates Phase 6 must still encode.
        context = {
            "audio_file": self.MASTER,
            "chapter_file": self.CHAPTER_FILE,
            "cover_file": None,
            "total_duration_sec": 3_600.0,
        }
        _, cmd = self._run(context)
        assert "-ss" not in cmd
        assert "-t" not in cmd

    def test_active_trim_seeks_past_the_intro_and_caps_the_duration(self):
        context = {
            "audio_file": self.MASTER,
            "chapter_file": self.CHAPTER_FILE,
            "cover_file": None,
            "total_duration_sec": 3_592.896,
            "trim_intro_ms": 2_043,
            "trim_outro_ms": 5_061,
        }
        _, cmd = self._run(context)
        # -ss must precede the master's -i so it applies to input 0 only.
        assert cmd[cmd.index("-ss") + 1] == "2.043"
        assert cmd.index("-ss") < cmd.index("-i")
        # -t is an output option: after the codec flags, before the output path.
        assert cmd[cmd.index("-t") + 1] == "3592.896"
        assert cmd.index("-t") > cmd.index("libmp3lame")
        assert cmd[-1] == "/data/out.mp3.part"

    def test_outro_only_trim_still_caps_the_duration(self):
        context = {
            "audio_file": self.MASTER,
            "chapter_file": self.CHAPTER_FILE,
            "cover_file": None,
            "total_duration_sec": 3_595.0,
            "trim_intro_ms": 0,
            "trim_outro_ms": 5_000,
        }
        _, cmd = self._run(context)
        assert cmd[cmd.index("-ss") + 1] == "0.0"
        assert cmd[cmd.index("-t") + 1] == "3595.0"


class TestEncodeBookMp3PartialContainment:
    """B2 regression: ffmpeg writes to "<final>.part", promoted with an atomic
    rename only on a clean exit. A truncated .mp3 left at the library path is
    fully probe-readable (unlike a moov-less .m4b), so the next deep sync would
    adopt it and mark the book DOWNLOADED."""

    FINAL = "/data/out.mp3"
    PART = "/data/out.mp3.part"
    CONTEXT = {
        "audio_file": "/tmp/x/master_intermediate.m4b",
        "chapter_file": "/tmp/x/chapters.txt",
        "cover_file": None,
        "total_duration_sec": 3_600.0,
    }

    def _run(self, returncode):
        proc = mock.MagicMock()
        proc.stdout.readline.return_value = ""
        proc.stderr.read.return_value = ""
        proc.wait.return_value = returncode
        with (
            mock.patch.object(ccl, "load_settings", return_value={}),
            mock.patch.object(ccl, "_probe_source_audio_params", return_value=(None, None)),
            mock.patch.object(ccl.subprocess, "Popen", return_value=proc) as popen,
            mock.patch.object(ccl.process_registry, "register"),
            mock.patch.object(ccl.process_registry, "unregister"),
            mock.patch.object(ccl, "_yield_progress"),
            mock.patch.object(ccl.os, "replace") as replace,
            mock.patch.object(ccl.os, "remove") as remove,
        ):
            result = ccl.encode_book_mp3("B0X", 1, "/tmp/x", self.FINAL, self.CONTEXT)
        return result, popen.call_args.args[0], replace, remove

    def test_success_renames_the_part_file_into_place(self):
        result, cmd, replace, remove = self._run(0)
        assert result is True
        assert cmd[-1] == self.PART
        replace.assert_called_once_with(self.PART, self.FINAL)
        remove.assert_not_called()

    def test_part_output_names_the_mp3_muxer_explicitly(self):
        # ffmpeg infers the muxer from the output extension, and ".part" maps to
        # none of them ("Unable to choose an output format"), so hiding the real
        # ".mp3" suffix behind it has to be paired with an explicit "-f mp3" —
        # without it every single-file MP3 encode fails before writing a byte.
        _, cmd, _, _ = self._run(0)
        assert cmd[-3:] == ["-f", "mp3", self.PART]

    def test_failure_removes_the_part_file_and_never_promotes_it(self):
        result, _, replace, remove = self._run(1)
        assert result is False
        replace.assert_not_called()
        remove.assert_called_once_with(self.PART)

    def test_cancellation_removes_the_part_file(self):
        result, _, replace, remove = self._run(-15)
        assert result is False
        replace.assert_not_called()
        remove.assert_called_once_with(self.PART)

    def test_missing_part_file_on_cleanup_is_non_fatal(self):
        # Best-effort cleanup: ffmpeg may never have created the file.
        proc = mock.MagicMock()
        proc.stdout.readline.return_value = ""
        proc.stderr.read.return_value = ""
        proc.wait.return_value = 1
        with (
            mock.patch.object(ccl, "load_settings", return_value={}),
            mock.patch.object(ccl, "_probe_source_audio_params", return_value=(None, None)),
            mock.patch.object(ccl.subprocess, "Popen", return_value=proc),
            mock.patch.object(ccl.process_registry, "register"),
            mock.patch.object(ccl.process_registry, "unregister"),
            mock.patch.object(ccl, "_yield_progress"),
            mock.patch.object(ccl.os, "remove", side_effect=OSError("gone")),
        ):
            assert ccl.encode_book_mp3("B0X", 1, "/tmp/x", self.FINAL, self.CONTEXT) is False

    def test_unexpected_exception_still_removes_the_part_file(self):
        # v0.23.0 ND2: only OSError is handled here. Anything else — a closed
        # progress pipe raising ValueError mid-read, say — propagates to the
        # caller as before, but must not orphan the ".part" file in /data.
        proc = mock.MagicMock()
        proc.stdout.readline.side_effect = ValueError("I/O operation on closed file")
        proc.stderr.read.return_value = ""
        with (
            mock.patch.object(ccl, "load_settings", return_value={}),
            mock.patch.object(ccl, "_probe_source_audio_params", return_value=(None, None)),
            mock.patch.object(ccl.subprocess, "Popen", return_value=proc),
            mock.patch.object(ccl.process_registry, "register"),
            mock.patch.object(ccl.process_registry, "unregister") as unregister,
            mock.patch.object(ccl, "_yield_progress"),
            mock.patch.object(ccl.os, "replace") as replace,
            mock.patch.object(ccl.os, "remove") as remove,
        ):
            with pytest.raises(ValueError):
                ccl.encode_book_mp3("B0X", 1, "/tmp/x", self.FINAL, self.CONTEXT)
        replace.assert_not_called()
        remove.assert_called_once_with(self.PART)
        # The cleanup shares the finally with the registry unregister; neither
        # may be skipped by the escaping exception.
        unregister.assert_called_once()

    def test_bail_before_the_encoder_starts_touches_no_files(self):
        # The stop-event bail happens before ffmpeg spawns, so no ".part" of this
        # run's making exists — the cleanup guard must not reach for one.
        stop_event = threading.Event()
        stop_event.set()
        with (
            mock.patch.object(ccl, "load_settings", return_value={}),
            mock.patch.object(ccl, "_probe_source_audio_params", return_value=(None, None)),
            mock.patch.object(ccl.subprocess, "Popen") as popen,
            mock.patch.object(ccl.process_registry, "register"),
            mock.patch.object(ccl.process_registry, "unregister"),
            mock.patch.object(ccl, "_yield_progress"),
            mock.patch.object(ccl.os, "replace") as replace,
            mock.patch.object(ccl.os, "remove") as remove,
        ):
            result = ccl.encode_book_mp3("B0X", 1, "/tmp/x", self.FINAL, self.CONTEXT, stop_event=stop_event)
        assert result is False
        popen.assert_not_called()
        replace.assert_not_called()
        remove.assert_not_called()

    def test_failed_promotion_reports_failure_and_cleans_up(self):
        proc = mock.MagicMock()
        proc.stdout.readline.return_value = ""
        proc.stderr.read.return_value = ""
        proc.wait.return_value = 0
        with (
            mock.patch.object(ccl, "load_settings", return_value={}),
            mock.patch.object(ccl, "_probe_source_audio_params", return_value=(None, None)),
            mock.patch.object(ccl.subprocess, "Popen", return_value=proc),
            mock.patch.object(ccl.process_registry, "register"),
            mock.patch.object(ccl.process_registry, "unregister"),
            mock.patch.object(ccl, "_yield_progress"),
            mock.patch.object(ccl.os, "replace", side_effect=OSError("cross-device")),
            mock.patch.object(ccl.os, "remove") as remove,
        ):
            assert ccl.encode_book_mp3("B0X", 1, "/tmp/x", self.FINAL, self.CONTEXT) is False
        remove.assert_called_once_with(self.PART)


class TestEncodeBookMp3CancelRace:
    """v0.23.0 regression: process_registry's cancel is a one-shot snapshot, so a
    cancel that killed the source probe is spent by the time the encoder spawns —
    the fresh ffmpeg would escape it and encode the whole book for a cancelled
    job. A -15 probe exit and the job's stop event both stop the spawn."""

    FINAL = "/data/out.mp3"
    CONTEXT = {
        "audio_file": "/tmp/x/master_intermediate.m4b",
        "chapter_file": "/tmp/x/chapters.txt",
        "cover_file": None,
        "total_duration_sec": 3_600.0,
    }

    def _run(self, probe_returncode, stop_event=None, probe_stdout="", settings=None):
        """Drive encode_book_mp3 with a real _probe_source_audio_params over a
        mocked ffprobe result, returning (result, the Popen mock)."""
        proc = mock.MagicMock()
        proc.stdout.readline.return_value = ""
        proc.stderr.read.return_value = ""
        proc.wait.return_value = 0
        probe_res = subprocess.CompletedProcess(["ffprobe"], probe_returncode, probe_stdout, "")
        with (
            mock.patch.object(ccl, "load_settings", return_value=settings if settings is not None else {}),
            mock.patch.object(ccl, "_run_registered", return_value=probe_res),
            mock.patch.object(ccl.subprocess, "Popen", return_value=proc) as popen,
            mock.patch.object(ccl.process_registry, "register"),
            mock.patch.object(ccl.process_registry, "unregister"),
            mock.patch.object(ccl, "_yield_progress"),
            mock.patch.object(ccl.os, "replace"),
            mock.patch.object(ccl.os, "remove"),
        ):
            result = ccl.encode_book_mp3("B0X", 1, "/tmp/x", self.FINAL, self.CONTEXT, stop_event=stop_event)
        return result, popen

    def test_probe_killed_by_cancel_never_spawns_the_encoder(self):
        result, popen = self._run(-15)
        assert result is False
        popen.assert_not_called()

    def test_stop_event_already_set_never_spawns_the_encoder(self):
        stop_event = threading.Event()
        stop_event.set()
        result, popen = self._run(0, stop_event=stop_event)
        assert result is False
        popen.assert_not_called()

    def test_clear_stop_event_encodes_as_usual(self):
        result, popen = self._run(0, stop_event=threading.Event())
        assert result is True
        popen.assert_called_once()

    # A sample-rate cap only takes effect when the probe actually reported a
    # HIGHER source rate, so these two cases share settings and differ only in
    # whether the probe succeeded — which is what separates "benign failure" from
    # "cancellation" at the same (None, None) return.
    CAP_SETTINGS = {"conversion": {"mp3": {"max_sample_rate": 44100}}}
    PROBE_STDOUT = "bit_rate=64000\nsample_rate=48000"

    def test_successful_probe_applies_the_sample_rate_cap(self):
        result, popen = self._run(0, probe_stdout=self.PROBE_STDOUT, settings=self.CAP_SETTINGS)
        assert result is True
        cmd = popen.call_args.args[0]
        assert cmd[cmd.index("-ar") + 1] == "44100"

    def test_ordinary_probe_failure_still_encodes_with_fallback_flags(self):
        # A probe that simply couldn't read the master is benign: the encode
        # proceeds, and with no source sample rate to compare against, the cap
        # that the successful probe above applied is suppressed.
        result, popen = self._run(1, probe_stdout=self.PROBE_STDOUT, settings=self.CAP_SETTINGS)
        assert result is True
        cmd = popen.call_args.args[0]
        assert "-q:a" in cmd
        assert "-ar" not in cmd


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

    def test_zero_bitrate_floors_at_32(self):
        # Clearing the UI Bitrate field saves Number("") === 0; with source
        # matching off that would emit "-b:a 0k" and fail every encode, so the
        # explicit bitrate is floored at 32 kbps.
        flags = build_mp3_flags(
            {"target": "bitrate", "bitrate_kbps": 0, "match_source_bitrate": False, "constant_bitrate": True},
            None,
            None,
        )
        assert flags[:2] == ["-b:a", "32k"]

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

    @pytest.mark.parametrize("bad_cap", [-1, -44100, 0])
    def test_sample_rate_gate_rejects_non_positive_caps(self, bad_cap):
        # The UI's min/max attributes are decorative (a bare Number() reads the
        # field), so a typed "-1" would otherwise emit "-ar -1" and fail every
        # encode. Zero and negative both mean "no cap".
        assert "-ar" not in build_mp3_flags({"target": "quality", "max_sample_rate": bad_cap}, None, 48000)

    @pytest.mark.parametrize("junk_cap", [True, False, "44100", None, float("nan"), float("inf")])
    def test_sample_rate_gate_rejects_non_integer_caps(self, junk_cap):
        # v0.23.0 ND1: a hand-edited settings.json can carry any JSON value here,
        # and none of these is a rate. Only True/False are the actual regression
        # (with the float cases in the next test): `bool` is an `int` subclass, so
        # a bare `true` passed the old numeric gate and was formatted straight onto
        # the command line as "-ar True", failing every encode. None, "44100", NaN
        # and Infinity were all rejected before this change too (json.load() does
        # accept NaN/Infinity, so they can reach here) — they are kept to pin that
        # pre-existing behaviour rather than to cover the fix.
        assert "-ar" not in build_mp3_flags({"target": "quality", "max_sample_rate": junk_cap}, None, 48000)

    @pytest.mark.parametrize(("float_cap", "emitted"), [(44100.0, "44100"), (44100.9, "44100")])
    def test_sample_rate_gate_truncates_float_caps(self, float_cap, emitted):
        # A float is a plausible way to hand-write a rate, but "-ar 44100.0" is
        # not something ffmpeg parses — truncate to whole Hz instead of dropping
        # the cap the user clearly asked for.
        flags = build_mp3_flags({"target": "quality", "max_sample_rate": float_cap}, None, 48000)
        assert flags[flags.index("-ar") + 1] == emitted

    def test_null_bitrate_falls_back_to_the_default(self):
        # A hand-edited settings.json can carry an explicit null, which would
        # otherwise blow up in the floor clamp.
        flags = build_mp3_flags({"target": "bitrate", "bitrate_kbps": None, "match_source_bitrate": False}, None, None)
        assert flags[flags.index("-b:a") + 1] == "128k"

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


class TestAnnotationsFetch:
    """v0.23.0 Phase 6: the download-time annotations fetch. The dump must land in
    a SUBDIRECTORY of the book's temp dir — the download's file detection takes
    the FIRST .json in the temp root as the chapter file — "no annotations" is a
    normal outcome rather than a failure, every other failure is swallowed so a
    bonus sidecar can never fail a conversion, and a SIGTERM still reports the
    cancel."""

    def _fetch(self, tmp_path, *, returncode=0, write_dump=False, exc=None):
        """Drive _fetch_annotations against a real temp dir with only the
        subprocess helper faked, optionally writing the dump audible-cli would.
        Returns ((annotations_file, cancelled), run_mock, captured_kwargs)."""

        captured = []

        def fake_run(cmd, job_id, **kwargs):
            captured.append(kwargs)
            if exc is not None:
                raise exc
            out_dir = cmd[cmd.index("-o") + 1]
            if write_dump:
                with open(os.path.join(out_dir, "Dracula-annotations.json"), "w", encoding="utf-8") as f:
                    f.write('{"payload": {"records": []}}')
            return subprocess.CompletedProcess(cmd, returncode, "", "")

        with mock.patch.object(ccl, "_run_registered", side_effect=fake_run) as run:
            result = ccl._fetch_annotations("B0X", 1, str(tmp_path), {"HOME": "/database"})
        return result, run, captured

    def test_dump_lands_in_a_subdirectory_never_the_temp_root(self, tmp_path):
        (annotations_file, cancelled), run, captured = self._fetch(tmp_path, write_dump=True)
        assert cancelled is False
        assert annotations_file == str(tmp_path / "annotations" / "Dracula-annotations.json")
        # The auth env must reach the subprocess: audible-cli only finds its
        # credentials under HOME=/database, and because this fetch swallows its
        # own failures, dropping `env=env` would fail silently forever.
        assert captured[0]["env"] == {"HOME": "/database"}
        # The critical invariant: nothing new in the temp root, so the chapter
        # JSON detection there cannot pick up the annotations dump instead.
        assert list(tmp_path.glob("*.json")) == []
        # The audible-cli call is the annotation download, into that subdir.
        cmd = run.call_args[0][0]
        assert cmd[:5] == ["audible", "download", "-a", "B0X", "--annotation"]
        assert cmd[cmd.index("-o") + 1] == str(tmp_path / "annotations")

    def test_no_dump_is_normal_not_a_failure(self, tmp_path):
        # audible-cli exits 0 and writes nothing for a title with no annotations.
        assert self._fetch(tmp_path)[0] == (None, False)

    def test_nonzero_exit_is_swallowed(self, tmp_path):
        assert self._fetch(tmp_path, returncode=1)[0] == (None, False)

    def test_existing_dump_survives_a_failed_retry(self, tmp_path):
        # A retried download strategy re-runs the fetch; audible-cli may refuse to
        # overwrite the file an earlier attempt already wrote, and that non-zero
        # exit must not throw away what is sitting on disk.
        (annotations_file, cancelled), _run, _kwargs = self._fetch(tmp_path, returncode=1, write_dump=True)
        assert cancelled is False
        assert annotations_file.endswith("/annotations/Dracula-annotations.json")

    def test_existing_dump_survives_an_exception(self, tmp_path):
        # Same rule one layer out: the retry's call may not even get as far as an
        # exit code (makedirs/Popen raising OSError). The swallowed exception
        # must still hand back the dump the first attempt left behind.
        annotations_dir = tmp_path / "annotations"
        annotations_dir.mkdir()
        (annotations_dir / "Dracula-annotations.json").write_text("{}", encoding="utf-8")
        (annotations_file, cancelled), _run, _kwargs = self._fetch(tmp_path, exc=OSError("boom"))
        assert cancelled is False
        assert annotations_file == str(annotations_dir / "Dracula-annotations.json")

    def test_unexpected_exception_is_swallowed(self, tmp_path):
        assert self._fetch(tmp_path, exc=OSError("boom"))[0] == (None, False)

    def test_sigterm_reports_cancellation(self, tmp_path):
        assert self._fetch(tmp_path, returncode=-15)[0] == (None, True)

    def test_prepare_aborts_when_the_fetch_is_cancelled(self):
        # A cancel during the annotations fetch must surface as prepare's clean
        # (None, None) cancellation signal, not as a download-strategy failure
        # that falls back to (an equally cancelled) AAX download.
        download_proc = mock.MagicMock()
        download_proc.stderr.readline.return_value = ""
        download_proc.stdout.read.return_value = ""
        download_proc.wait.return_value = 0

        with (
            mock.patch.object(ccl, "load_settings", return_value={"conversion": {"save_annotations": True}}),
            mock.patch.object(ccl.subprocess, "Popen", return_value=download_proc) as popen,
            mock.patch.object(
                ccl, "_run_registered", return_value=subprocess.CompletedProcess(["audible"], -15)
            ) as run,
            mock.patch.object(ccl.os, "makedirs"),
            mock.patch.object(ccl.process_registry, "register"),
            mock.patch.object(ccl.process_registry, "unregister"),
            mock.patch.object(ccl, "_yield_progress"),
        ):
            result = ccl.prepare_book_assets("B0X", 1, "/tmp/x")

        assert result == (None, None)
        # Only the download ran; no fallback strategy was started after the cancel.
        assert popen.call_count == 1
        # End-to-end companion to the helper-level check: prepare's env (with
        # HOME pointed at /database for audible-cli's auth) is what reaches the
        # fetch, not a bare inherited environment.
        assert run.call_args.kwargs["env"]["HOME"] == ccl.DATABASE_DIR

    def test_prepare_skips_the_fetch_when_the_setting_is_off(self):
        # Default install (and every old settings.json, which has no such key):
        # the first registered call after the download is the metadata fetch, not
        # an annotations download. The cancel raised from it is the module's
        # existing path, used here only to stop prepare after the assertion point.
        download_proc = mock.MagicMock()
        download_proc.stderr.readline.return_value = ""
        download_proc.stdout.read.return_value = ""
        download_proc.wait.return_value = 0

        with (
            mock.patch.object(ccl, "load_settings", return_value={}),
            mock.patch.object(ccl.subprocess, "Popen", return_value=download_proc),
            mock.patch.object(
                ccl, "_run_registered", side_effect=subprocess.CalledProcessError(-15, ["audible", "api"])
            ) as run,
            mock.patch.object(ccl.process_registry, "register"),
            mock.patch.object(ccl.process_registry, "unregister"),
            mock.patch.object(ccl, "_yield_progress"),
        ):
            result = ccl.prepare_book_assets("B0X", 1, "/tmp/x")

        assert result == (None, None)
        assert run.call_count == 1
        assert "--annotation" not in run.call_args_list[0][0][0]

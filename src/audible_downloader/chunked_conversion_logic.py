# audible_downloader/chunked_conversion_logic.py

# --- Attribution ---
# The core concepts for the ffmpeg metadata and chapter generation in this file
# are adapted from the work of Jan van Brügge in the original audible-convert.sh script.
# Original Source: https://github.com/jvanbruegge/nix-config/blob/master/scripts/audible-convert.sh
# License: MIT (included in the project's LICENSE.txt file)
# --- End Attribution ---

import json
import os
import re
import subprocess
from collections import deque
from threading import Lock, Thread

from . import (
    DATABASE_DIR,
    announcer,  # Import announcer for progress updates
)
from .chapter_transforms import (
    apply_branding_trim,
    drop_zero_length_chapters,
    flatten_chapter_tree,
    merge_credit_chapters,
    merge_short_chapters,
    render_chapter_title,
    strip_unabridged,
)
from .db import get_db_connection
from .logger import log
from .process_registry import process_registry
from .settings import load_settings, resolve_output_format

# A lock to safely track progress across multiple threads, which will still be useful.
progress_lock = Lock()

# A single-chapter book longer than AUTO_CHUNK_TRIGGER_SEC is split into synthetic
# AUTO_CHUNK_SIZE_SEC "Part N" chapters so the per-chapter re-encode has parallel
# work (and the listener gets navigation points). Seconds.
AUTO_CHUNK_TRIGGER_SEC = 1800
AUTO_CHUNK_SIZE_SEC = 900

# Upper bound on the COMBINED Audible brand intro + outro span the trim will act
# on. Real branding is roughly 2s + 5s; a combined span past a minute is corrupt
# chapter-JSON data, not branding. Milliseconds.
MAX_PLAUSIBLE_BRAND_SPAN_MS = 60_000

# The MP3 (LAME) frame bitrates, ascending. When "match source bitrate" is on we
# round the master's bitrate UP to the smallest of these that isn't lower, so a
# re-encode never throws away bits the source had (capped at 320, LAME's ceiling).
MP3_STANDARD_BITRATES_KBPS = [32, 40, 48, 56, 64, 80, 96, 112, 128, 160, 192, 224, 256, 320]


def _should_auto_chunk(lossless, audio_file, chapters_list, total_duration_sec):
    """
    Decide whether to replace a chapterless/single-chapter book's chapters with
    synthetic time-based "Part N" markers.

    Skipped ONLY when this title will actually be remuxed losslessly — i.e.
    no-re-encode mode is on AND the fast AAC-copy decrypt produced a ".m4b"
    master. The gate deliberately checks the *real* master codec, not just the
    requested `lossless` flag: if the decrypt fell back to FLAC, the orchestrator
    routes that title to the normal re-encode path, which still needs the
    chunking — so it must NOT be skipped for a FLAC master.
    """
    will_remux_lossless = lossless and audio_file.lower().endswith(".m4b")
    if will_remux_lossless:
        return False
    return len(chapters_list) <= 1 and total_duration_sec > AUTO_CHUNK_TRIGGER_SEC


def _read_brand_span(chapter_info, camel_key, snake_key):
    """
    Read one brand intro/outro span (milliseconds) out of the chapter JSON's
    `chapter_info` block, preferring the camelCase key audible-cli writes and
    falling back to the snake_case spelling. Anything missing, null, or
    non-numeric reads as 0, i.e. "no branding to trim" — a bad value must never
    take a chunk out of someone's audiobook.
    """
    raw = chapter_info.get(camel_key)
    if raw is None:
        raw = chapter_info.get(snake_key)
    try:
        return max(0, int(raw))
    except (TypeError, ValueError):
        return 0


def _summarize_subprocess_error(exc, fallback):
    """
    Build a concise, human-readable reason from a failed subprocess. The bare
    CalledProcessError string only says "returned non-zero exit status N" — all
    the user would otherwise see. The useful message lives in the output streams,
    but *which* stream varies: audible-cli prints user-facing errors
    ("error: Asin ... not found in library.") to stdout, while ffmpeg writes to
    stderr. So collect candidate lines from both, drop tqdm progress bars and the
    generic "Aborted!" noise, prefer an explicit error line, and otherwise return
    the last few lines. Falls back to `fallback` when nothing usable is found.
    """
    candidates = []
    for stream in (getattr(exc, "output", None), getattr(exc, "stderr", None)):
        if isinstance(stream, bytes):
            stream = stream.decode("utf-8", errors="replace")
        if not stream:
            continue
        # tqdm redraws progress with carriage returns; split those out so the
        # bar fragments can be filtered instead of masking the real message.
        for line in stream.replace("\r", "\n").splitlines():
            line = line.strip()
            if not line or "%|" in line or line == "Aborted!":
                continue
            candidates.append(line)

    if not candidates:
        return fallback
    for line in candidates:
        lowered = line.lower()
        if lowered.startswith("error") or "not found" in lowered or "not available" in lowered:
            return line
    return " | ".join(candidates[-3:])


def _run_registered(cmd, job_id, *, check=False, text=True, encoding=None, errors=None, env=None):
    """
    Run `cmd` to completion like subprocess.run, but registered with the
    process_registry for its lifetime so a job cancel (SIGTERM) reaches these
    otherwise-unregistered short probe/metadata calls (house rule: every
    ffmpeg/ffprobe/audible subprocess must be cancellable). stdout and stderr are
    always captured. Returns a subprocess.CompletedProcess; with check=True a
    non-zero exit raises CalledProcessError carrying output/stderr, exactly as
    subprocess.run would — so the existing fallback and error-summary handling is
    unchanged. A SIGTERM surfaces as returncode -15, which the callers already
    treat as cancellation.
    """
    proc = subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=text,
        encoding=encoding,
        errors=errors,
        env=env,
    )
    process_registry.register(job_id, proc)
    try:
        stdout, stderr = proc.communicate()
    finally:
        process_registry.unregister(job_id, proc)

    result = subprocess.CompletedProcess(cmd, proc.returncode, stdout, stderr)
    if check:
        result.check_returncode()
    return result


def annotations_command(asin, out_dir):
    """
    The audible-cli invocation that dumps a title's own annotations (clips,
    notes, bookmarks) as raw Audible JSON into `out_dir`. Shared with the
    on-demand route in routes.py so the two can never drift apart. Callers must
    still supply an env with HOME=DATABASE_DIR, as with every audible-cli call.
    """
    return ["audible", "download", "-a", asin, "--annotation", "-o", out_dir]


def find_annotations_file(directory):
    """
    The annotations dump inside `directory`, or None when there isn't one.

    audible-cli names the file after the book's title, not its ASIN
    ("Project_Hail_Mary-annotations.json"), so it can only be found by suffix.
    Sorted for determinism in the (not expected) case of more than one match.
    """
    try:
        matches = sorted(
            entry.path
            for entry in os.scandir(directory)
            if entry.is_file() and entry.name.endswith("-annotations.json")
        )
    except OSError:
        return None
    return matches[0] if matches else None


def _fetch_annotations(asin, job_id, temp_dir, env):
    """
    Best-effort fetch of a title's annotations during download. Returns
    (annotations_file, cancelled) — the absolute path to the dump (or None), and
    whether the call was SIGTERMed, i.e. the job was cancelled.

    The dump MUST land in a subdirectory of the book's temp dir, never the temp
    root: the file detection in prepare_book_assets takes the FIRST .json it
    finds there as the chapter file, so a second .json beside it could win that
    race and break every chapter in the book.

    audible-cli exits 0 either way — a title with annotations writes
    "<Title>-annotations.json", a title without writes nothing and just prints
    "No annotations found for <title>." — so the file's presence is the only
    reliable signal and its absence is normal, not a failure. Every other
    failure mode is logged and swallowed: annotations are a bonus sidecar and
    must never turn a good conversion into an error. A SIGTERM (-15) is the one
    exception, reported back so the caller can bail like every other cancel.
    """
    annotations_dir = os.path.join(temp_dir, "annotations")
    try:
        os.makedirs(annotations_dir, exist_ok=True)
        result = _run_registered(
            annotations_command(asin, annotations_dir),
            job_id,
            encoding="utf-8",
            errors="replace",
            env=env,
        )
        if result.returncode == -15:
            return None, True

        # Globbed regardless of the exit code: when a download strategy failed
        # and retried, an earlier attempt may already have written the file, and
        # a second (now-failing) call must not throw away what is on disk.
        annotations_file = find_annotations_file(annotations_dir)
        if annotations_file:
            log.info(f"PREPARE ({asin}): Fetched annotations dump {os.path.basename(annotations_file)}")
        elif result.returncode != 0:
            log.warning(
                f"PREPARE ({asin}): Annotations fetch exited {result.returncode}; continuing without annotations."
            )
        else:
            log.info(f"PREPARE ({asin}): No annotations (clips/notes/bookmarks) found for this title.")
        return annotations_file, False
    except Exception as e:
        # Same invariant as the returncode path above, and for the same reason: a
        # dump an earlier download attempt already wrote is still valid, so an
        # exception on a later attempt (makedirs/Popen hitting ENOSPC or ENOMEM,
        # say) must report what is on disk rather than discard it.
        annotations_file = find_annotations_file(annotations_dir)
        if annotations_file:
            log.warning(
                f"PREPARE ({asin}): Annotations fetch failed: {e}. Keeping the dump an earlier attempt already wrote."
            )
        else:
            log.warning(f"PREPARE ({asin}): Annotations fetch failed: {e}. Continuing without annotations.")
        return annotations_file, False


def _yield_progress(asin, status_text, progress, job_id=None):
    """
    A helper function to format and announce progress updates via the global announcer.
    This replaces the `yield` statements from the old generator.
    """
    payload = {
        "asin": asin,
        "status_text": status_text,
        "progress": progress,
    }

    # --- INFER FINAL STATUS ---
    # This ensures the frontend knows to mark the item green/red and log it.
    if status_text == "Complete!":
        payload["final_status"] = "success"
    elif status_text in ["Failed!", "Cancelled"]:
        payload["final_status"] = "error"

    # Announce the update to all listening clients.
    announcer.announce(f"event: job_update\ndata: {json.dumps(payload)}\n\n")


def prepare_book_assets(asin, job_id, temp_dir, lossless=False):
    """
    Handles Phase 1 of conversion: downloading all necessary files from Audible,
    fetching metadata, and preparing the chapter file for ffmpeg.

    Implements:
    1. Download Strategy: AAXC (Fast) -> AAX (Reliable)
    2. Decrypt Strategy: AAC Copy (Fast) -> FLAC (Safe)

    `lossless` (no-re-encode mode) gates ONLY the synthetic single-chapter
    auto-chunking below, and only when it actually applies: that chunking exists
    to parallelize the per-chapter re-encode, which the lossless remux skips, so
    a chapterless title keeps its native chapters instead of being cut into
    "Part N" markers. It is still applied if the fast AAC-copy decrypt fell back
    to FLAC, because that title takes the normal re-encode path and needs the
    chunking (see the codec check at the auto-chunking site).
    Everything else — download, decrypt, integrity checks, metadata — is
    identical regardless of the flag.

    Returns a (context, error) tuple:
      - success:   (context_dict, None)
      - failure:   (None, "human-readable reason")  -- surfaced to the UI
      - cancelled: (None, None)                      -- job was stopped, not an error
    """
    log.info(f"PREPARE ({asin}): Starting asset preparation in {temp_dir}")
    env = os.environ.copy()
    env["HOME"] = DATABASE_DIR

    settings = load_settings()
    want_pdf = settings.get("conversion", {}).get("download_supplementary_pdf", True)

    # Download-quality request: what we ask Audible to serve (a distinct axis from
    # the output encode quality). Validate against the flag values audible-cli's
    # `download --quality` accepts; anything unexpected in an old/hand-edited
    # settings.json falls back to the audible-cli default of "best".
    download_quality = settings.get("conversion", {}).get("download_quality", "best")
    if download_quality not in ("best", "high", "normal"):
        download_quality = "best"

    # Retain the raw AAX/AAXC master (+ voucher) next to the finished book instead
    # of deleting it after decrypt. Read once here; consumed at the delete site
    # below and threaded into the returned context for the sidecar placement.
    retain_aax = settings.get("conversion", {}).get("retain_aax", False)

    # Save the listener's annotations (clips, notes, bookmarks) as a raw JSON
    # sidecar. Read once here; the fetch happens right after the audio download
    # below and the result is threaded into the returned context.
    save_annotations = settings.get("conversion", {}).get("save_annotations", False)

    # Variables to hold state across the retry loops
    audio_file = None
    cover_file = None
    pdf_file = None
    book_info = None
    chapters_list = None
    # Audible brand intro/outro spans, read off the chapter JSON below. Defined
    # before the retry loop so the Phase 2 gate always has a value (0 = the title
    # reports no branding, which is also what a chapter JSON without the keys
    # means).
    brand_intro_ms = 0
    brand_outro_ms = 0
    decryption_args = []
    # When retain_aax is on, these carry the raw encrypted master and its AAXC
    # voucher out to the context (both None otherwise). Initialized before the
    # retry loop so they're always defined at the return statement in Phase 2.
    retained_raw_audio_file = None
    retained_voucher_file = None
    # The annotations dump, when the setting is on and the title actually has
    # annotations (both are optional). Same precedent as the two above: defined
    # before the retry loop so it always has a value at the return statement.
    annotations_file = None

    # Strategy Definition: (Flag, Name)
    download_strategies = [("--aaxc", "AAXC (Fast)"), ("--aax-fallback", "AAX (Reliable)")]

    for attempt_idx, (dl_flag, strategy_name) in enumerate(download_strategies):
        log.info(f"PREPARE ({asin}): Attempting download using strategy: {strategy_name}")
        _yield_progress(asin, f"Downloading ({strategy_name})...", 5, job_id)

        try:
            # --- 1. Download Book Files ---
            download_command = [
                "audible",
                "download",
                "-a",
                asin,
                dl_flag,
                "--cover",
                "--cover-size",
                "1215",
                "--chapter",
                "--quality",
                download_quality,
                "-o",
                temp_dir,
            ]
            # Also pull the companion PDF when enabled. audible-cli simply
            # downloads nothing extra for titles without one, so this is safe.
            if want_pdf:
                download_command.append("--pdf")

            process = subprocess.Popen(
                download_command,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
                errors="replace",
                env=env,
            )
            process_registry.register(job_id, process)

            # audible-cli writes its user-facing error text (e.g. "error: Asin ...
            # not found in library.") to stdout, which we never read for progress.
            # Drain it concurrently in a thread rather than only after wait(): if
            # the child ever filled the ~64KB stdout pipe while we were busy
            # reading stderr below, an undrained stdout would deadlock it. The
            # collected text is joined after wait().
            stdout_chunks = []

            def _drain_stdout():
                if process.stdout:
                    stdout_chunks.append(process.stdout.read())

            stdout_thread = Thread(target=_drain_stdout, daemon=True)
            stdout_thread.start()

            # Capture stderr while scanning it for progress. audible-cli writes
            # its error messages here too, but this loop drains the stream to
            # EOF, so a later process.stderr.read() would come back empty — keep
            # a bounded tail so a failure can be explained instead of surfacing
            # as a bare "non-zero exit status". Unregister in a finally so the
            # dead Popen is dropped from the registry on every exit — success,
            # failure (retry), and SIGTERM cancel alike (register/unregister must
            # be paired, or a cancel leaves stale process references behind).
            try:
                stderr_tail = deque(maxlen=50)
                for line in iter(process.stderr.readline, ""):
                    stderr_tail.append(line)
                    match = re.search(r"(\d+)%", line)
                    if match:
                        download_percent = int(match.group(1))
                        overall_progress = 5 + int(download_percent * 0.20)
                        _yield_progress(asin, f"Downloading... {download_percent}%", overall_progress, job_id)

                returncode = process.wait()
                stdout_thread.join()
                stdout_text = "".join(stdout_chunks)
            finally:
                process_registry.unregister(job_id, process)

            if returncode != 0:
                if returncode == -15:
                    log.info(f"PREPARE ({asin}): Download cancelled.")
                    return None, None
                raise subprocess.CalledProcessError(
                    returncode, download_command, output=stdout_text, stderr="".join(stderr_tail)
                )

            log.info(f"PREPARE ({asin}): Download finished.")

            # --- 1b. Optional: Annotations (clips / notes / bookmarks) ---
            # A separate audible-cli call, so it runs only once the audio is
            # safely down. _fetch_annotations swallows its own failures (the
            # sidecar is a bonus, never worth failing a book over) and writes
            # into a SUBDIRECTORY so the chapter-JSON detection below is
            # untouched; only a cancel comes back for us to act on.
            if save_annotations:
                # Announce the step like every other boundary here: a title with
                # many clips takes a few seconds, and without this the panel
                # holds "Downloading... 100%" and reads as a stall.
                _yield_progress(asin, "Fetching annotations...", 25, job_id)
                annotations_file, annotations_cancelled = _fetch_annotations(asin, job_id, temp_dir, env)
                if annotations_cancelled:
                    log.info(f"PREPARE ({asin}): Cancelled during annotations fetch.")
                    return None, None

            # --- 2. Get Metadata & Detect Files ---
            _yield_progress(asin, "Preparing metadata...", 25, job_id)

            endpoint, params = f"/1.0/library/{asin}", "response_groups=media,contributors,series,category_ladders"
            meta_command = ["audible", "api", "-p", params, endpoint]
            result = _run_registered(meta_command, job_id, check=True, encoding="utf-8", errors="replace", env=env)
            book_info = json.loads(result.stdout).get("item")

            def _find_file_by_ext(directory, extensions):
                for entry in os.scandir(directory):
                    if entry.is_file() and any(entry.name.lower().endswith(ext) for ext in extensions):
                        return entry.path
                return None

            voucher_file = _find_file_by_ext(temp_dir, [".voucher"])
            raw_audio_file = _find_file_by_ext(temp_dir, [".aax", ".aaxc"])
            cover_file = _find_file_by_ext(temp_dir, [".jpg", ".png"])
            json_file = _find_file_by_ext(temp_dir, [".json"])
            # Optional: not every title ships a companion PDF, so its absence is fine.
            pdf_file = _find_file_by_ext(temp_dir, [".pdf"])

            if not all([raw_audio_file, cover_file, json_file]):
                raise FileNotFoundError("Missing one or more critical files after download.")

            # Determine Keys
            current_decryption_args = []
            if raw_audio_file.lower().endswith(".aax"):
                log.debug(f"PREPARE ({asin}): Detected AAX file. Using activation bytes.")
                res = _run_registered(
                    ["audible", "activation-bytes"],
                    job_id,
                    check=True,
                    encoding="utf-8",
                    errors="replace",
                    env=env,
                )
                raw_output = res.stdout.strip()
                match = re.search(r"([a-fA-F0-9]{8})$", raw_output)
                activation_bytes = match.group(1) if match else raw_output.splitlines()[-1].strip()
                if not activation_bytes or len(activation_bytes) != 8:
                    raise ValueError(f"Could not parse valid activation bytes. Output: {raw_output}")
                current_decryption_args = ["-activation_bytes", activation_bytes]

            elif voucher_file:
                log.debug(f"PREPARE ({asin}): Detected AAXC file with voucher.")
                with open(voucher_file) as f:
                    voucher_data = json.load(f)
                key = voucher_data["content_license"]["license_response"]["key"]
                iv = voucher_data["content_license"]["license_response"]["iv"]
                current_decryption_args = ["-audible_key", key, "-audible_iv", iv]
            else:
                raise ValueError("No valid decryption method found.")

            # --- 3. INNER LOOP: Decryption Strategy ---
            # Try Fast Copy first, then Safe FLAC
            decryption_strategies = [
                ("AAC Copy (Fast)", ["-c", "copy"], ".m4b"),
                ("FLAC Decode (Safe)", ["-c:a", "flac", "-compression_level", "5"], ".flac"),
            ]

            decryption_success = False

            for dec_name, dec_flags, dec_ext in decryption_strategies:
                _yield_progress(asin, f"Decrypting ({dec_name})...", 28, job_id)
                log.info(f"PREPARE ({asin}): Attempting decryption: {dec_name}")

                decrypted_master_file = os.path.join(temp_dir, f"master_intermediate{dec_ext}")

                # Build Command
                decrypt_cmd = (
                    ["ffmpeg", "-y", "-hide_banner", "-loglevel", "error"]
                    + current_decryption_args
                    + ["-i", raw_audio_file]
                    + dec_flags
                    + ["-map", "0:a"]
                    + [decrypted_master_file]
                )

                try:
                    # Run Decryption. Register/unregister must be paired in a
                    # finally so an exceptional communicate() exit can't leak a
                    # live Popen into the registry (house try/finally rule).
                    d_proc = subprocess.Popen(decrypt_cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
                    try:
                        process_registry.register(job_id, d_proc)
                        _, d_stderr = d_proc.communicate()
                    finally:
                        process_registry.unregister(job_id, d_proc)

                    if d_proc.returncode != 0:
                        if d_proc.returncode == -15:
                            return None, None  # Cancelled
                        raise subprocess.CalledProcessError(d_proc.returncode, decrypt_cmd, stderr=d_stderr)

                    # --- 1. GET ACTUAL DURATION (Needed for both checks) ---
                    # We do this for FLAC too, just to be sure the file is readable.
                    dur_cmd = [
                        "ffprobe",
                        "-v",
                        "error",
                        "-show_entries",
                        "format=duration",
                        "-of",
                        "default=noprint_wrappers=1:nokey=1",
                        decrypted_master_file,
                    ]
                    dur_res = _run_registered(dur_cmd, job_id)
                    # A SIGTERM (-15) here is a cancel, not a corrupt file. Bail
                    # cleanly like the decrypt SIGTERM above rather than letting the
                    # empty-stdout ValueError below be caught by the inner
                    # `except` (which would `continue` into a full — and equally
                    # cancelled — FLAC decode that kill_job_processes won't reach).
                    if dur_res.returncode == -15:
                        log.info(f"PREPARE ({asin}): Cancelled during duration probe.")
                        return None, None
                    try:
                        actual_dur_sec = float(dur_res.stdout.strip())
                    except ValueError:
                        raise ValueError("Could not determine file duration. File likely corrupt.")

                    # --- 2. INTEGRITY CHECK: Duration vs Metadata ---
                    expected_min = book_info.get("runtime_length_min")
                    if expected_min:
                        expected_sec = expected_min * 60
                        # Tolerance: Allow 5% difference or 5 minutes, whichever is larger
                        diff = abs(actual_dur_sec - expected_sec)
                        tolerance = max(300, expected_sec * 0.05)

                        if diff > tolerance:
                            log.warning(
                                f"PREPARE ({asin}): Duration Mismatch! "
                                f"Expected ~{expected_min}m, Got {int(actual_dur_sec / 60)}m."
                            )
                            raise ValueError("Downloaded file is incomplete (Duration mismatch).")
                        log.info(f"PREPARE ({asin}): Duration integrity check passed.")

                    # --- 3. SEEK VERIFICATION (Copy Only) ---
                    # If we used 'Copy', we MUST verify seekability at the end of the file.
                    if "Copy" in dec_name:
                        log.info(f"PREPARE ({asin}): Verifying seek integrity for Copy strategy...")

                        # Use the duration we just calculated above
                        seek_target = max(0, actual_dur_sec - 60)

                        verify_cmd = [
                            "ffmpeg",
                            "-v",
                            "error",
                            "-ss",
                            str(seek_target),
                            "-i",
                            decrypted_master_file,
                            "-t",
                            "1",
                            "-f",
                            "null",
                            "-",
                        ]

                        v_proc = _run_registered(verify_cmd, job_id)
                        # Same as the duration probe: a -15 is cancellation, so
                        # return the cancel signal instead of raising into the
                        # inner except and falling back to FLAC after cancel.
                        if v_proc.returncode == -15:
                            log.info(f"PREPARE ({asin}): Cancelled during seek verification.")
                            return None, None
                        if v_proc.returncode != 0:
                            log.warning(f"PREPARE ({asin}): Verification failed for {dec_name}. Seek error detected.")
                            raise ValueError("Verification failed: File is not seekable.")

                        log.info(f"PREPARE ({asin}): Verification passed.")

                    # If we got here, this strategy worked!
                    audio_file = decrypted_master_file
                    decryption_success = True
                    break  # Break inner decryption loop

                except Exception as e:
                    log.warning(f"PREPARE ({asin}): Decryption strategy {dec_name} failed: {e}")
                    # Clean up failed file
                    if os.path.exists(decrypted_master_file):
                        os.remove(decrypted_master_file)
                    continue  # Try next decryption strategy (FLAC)

            if not decryption_success:
                raise RuntimeError("All decryption strategies failed.")

            # --- END SUCCESS ---
            # Clean up the raw encrypted file — unless the user asked to retain
            # it. When retained we skip the delete and carry the raw master (and
            # its AAXC voucher, if any — an AAXC is useless without it) in the
            # context; _place_sidecar_files copies both next to the finished book
            # at finalize time, the same best-effort pattern as the companion PDF.
            if retain_aax:
                retained_raw_audio_file = raw_audio_file
                retained_voucher_file = voucher_file
            else:
                try:
                    os.remove(raw_audio_file)
                except OSError:
                    pass

            decryption_args = []  # Clear args as file is now clean

            # Load Chapters for next phase
            with open(json_file, encoding="utf-8") as cj:
                chapter_data = json.load(cj)
            chapter_info = chapter_data.get("content_metadata", {}).get("chapter_info", {})
            chapters_list = chapter_info.get("chapters", [])

            # Brand intro/outro span lengths, in milliseconds, sitting alongside
            # "chapters" at the chapter_info level. audible-cli's chapter JSON
            # spells them camelCase; the snake_case names are accepted as a
            # fallback in case an older/other dump uses them. A title with no
            # branding simply omits both (or reports 0), which disables the trim.
            brand_intro_ms = _read_brand_span(chapter_info, "brandIntroDurationMs", "brand_intro_duration_ms")
            brand_outro_ms = _read_brand_span(chapter_info, "brandOutroDurationMs", "brand_outro_duration_ms")

            break  # Break outer download loop

        except Exception as e:
            # A registered probe/metadata subprocess killed by SIGTERM (-15) means
            # the job was cancelled — bail cleanly like the download SIGTERM path
            # above, rather than treating it as this strategy failing and either
            # falling back to (an equally-cancelled) AAX or stranding the book in
            # ERROR on the last strategy.
            if isinstance(e, subprocess.CalledProcessError) and e.returncode == -15:
                log.info(f"PREPARE ({asin}): Cancelled during preparation.")
                return None, None
            reason = _summarize_subprocess_error(e, str(e))
            log.warning(f"PREPARE ({asin}): Download strategy {strategy_name} failed: {reason}")
            # Cleanup for retry
            for fname in os.listdir(temp_dir):
                fpath = os.path.join(temp_dir, fname)
                try:
                    if os.path.isfile(fpath):
                        os.remove(fpath)
                except OSError:
                    pass

            if attempt_idx == len(download_strategies) - 1:
                log.error(f"PREPARE ({asin}): All download strategies exhausted. Last error: {reason}")
                return None, f"Download/preparation failed: {reason}"
            else:
                log.info(f"PREPARE ({asin}): Falling back to next strategy...")

    # =========================================================================
    # PHASE 2: Chapter Logic (Common)
    # =========================================================================

    try:
        # Get Total Duration
        probe_cmd = [
            "ffprobe",
            "-v",
            "error",
            "-show_entries",
            "format=duration",
            "-of",
            "default=noprint_wrappers=1:nokey=1",
            audio_file,
        ]
        probe_result = _run_registered(probe_cmd, job_id, check=True, encoding="utf-8", errors="replace")
        total_duration_sec = float(probe_result.stdout.strip())
        total_duration_ms = int(total_duration_sec * 1000)

        # Chapter-processing options (Phase 4), from the settings already loaded
        # at the top of this function. All default off/identity, so with a
        # stock settings.json the chapter list below is untouched.
        chapter_settings = settings.get("conversion", {}).get("chapters", {})
        combine_nested = chapter_settings.get("combine_nested_titles", False)
        merge_credits = chapter_settings.get("merge_credit_chapters", False)
        strip_branding = chapter_settings.get("strip_audible_branding", False)

        # 0a. Flatten nested chapter trees (joining parent/child titles with
        # ": "). Runs BEFORE the sanitize recompute so lengths are derived from
        # the flat list. When off, today's top-level-only list is kept exactly.
        if chapters_list and combine_nested:
            log.info(f"PREPARE ({asin}): Flattening nested chapter tree (combine_nested_titles).")
            chapters_list = flatten_chapter_tree(chapters_list, join_titles=True)

        # 0b. Fold Opening/End Credits chapters into their neighbors on the flat
        # list. Also runs before sanitize, which then recomputes the lengths.
        if chapters_list and merge_credits:
            log.info(f"PREPARE ({asin}): Merging credit chapters.")
            chapters_list = merge_credit_chapters(chapters_list)

        # 0c. Audible branding trim (Phase 6). Everything downstream of this point
        # works in the TRIMMED output timeline: effective_total_* replaces the
        # master's duration for the sanitize recompute, the auto-chunk spans, and
        # the MP3 progress denominator carried in the context.
        #
        # Gated on all three of: the setting, a re-encoding output format
        # (the "original" format is a straight remux of the master — there is no
        # encode pass to cut a span out of, and the settings help text says
        # Original files are never trimmed), and the title actually reporting a
        # brand span. When any of those is false every value below is exactly the
        # untrimmed one, so the pipeline output is unchanged.
        effective_total_ms = total_duration_ms
        effective_total_sec = total_duration_sec
        trim_intro_ms = 0
        trim_outro_ms = 0
        if strip_branding and resolve_output_format(settings) != "original" and (brand_intro_ms or brand_outro_ms):
            # Plausibility guard on the reported spans. A combined span past
            # MAX_PLAUSIBLE_BRAND_SPAN_MS — or one long enough to swallow the
            # whole title — is corrupt chapter-JSON data, and trimming on it
            # would leave a negative effective total: every chapter clamped to
            # zero length on the re-encode path, a negative -t on the MP3 path.
            # Skip the trim entirely and ship the untrimmed book, holding the
            # same line as _read_brand_span: a bad value never cuts audio.
            brand_span_ms = brand_intro_ms + brand_outro_ms
            if brand_span_ms > MAX_PLAUSIBLE_BRAND_SPAN_MS or brand_span_ms >= total_duration_ms:
                log.warning(
                    f"PREPARE ({asin}): Implausible Audible branding spans (intro {brand_intro_ms}ms, "
                    f"outro {brand_outro_ms}ms, book {total_duration_ms}ms); skipping the branding trim."
                )
            else:
                log.info(
                    f"PREPARE ({asin}): Trimming Audible branding (intro {brand_intro_ms}ms, outro {brand_outro_ms}ms)."
                )
                chapters_before_trim = len(chapters_list)
                chapters_list, effective_total_ms = apply_branding_trim(
                    chapters_list, brand_intro_ms, brand_outro_ms, total_duration_ms
                )
                # The trim drops markers whose whole span sits inside the outro.
                # Say so in the log: losing a chapter is worth a line when a user
                # later asks why the book has one fewer than Audible shows.
                dropped_in_outro = chapters_before_trim - len(chapters_list)
                if dropped_in_outro:
                    log.info(
                        f"PREPARE ({asin}): Dropped {dropped_in_outro} chapter marker(s) inside the trimmed outro."
                    )
                effective_total_sec = effective_total_ms / 1000.0
                trim_intro_ms = brand_intro_ms
                trim_outro_ms = brand_outro_ms

        # 1. Sanitize Chapter Durations
        if chapters_list:
            log.info(f"PREPARE ({asin}): Sanitizing chapter durations...")
            chapters_list.sort(key=lambda x: x.get("start_offset_ms", 0))
            for i in range(len(chapters_list)):
                current_start = chapters_list[i].get("start_offset_ms", 0)
                if i < len(chapters_list) - 1:
                    next_start = chapters_list[i + 1].get("start_offset_ms", 0)
                    # Every chapter ends at the end of the OUTPUT at the latest.
                    # A next start beyond it (chapter metadata that overruns the
                    # master's real duration) would otherwise give this chapter a
                    # chunk encode that reads past the end of the retained audio.
                    new_length = min(next_start, effective_total_ms) - current_start
                else:
                    # The final chapter runs to the end of the OUTPUT, which is
                    # where the branding trim (when active) shortens it by the
                    # outro span.
                    new_length = effective_total_ms - current_start
                chapters_list[i]["length_ms"] = max(0, new_length)

            # Two chapters sharing a start offset (a flattened parent and its
            # first child, or early chapters the branding trim clamped to 0) come
            # out of the loop above with length 0. Drop them here, before both the
            # chunk list and the FFMETADATA writer, so no zero-length chapter can
            # reach any output: a "-t 0" chunk encode writes a header-only file
            # with no audio stream, and one in first position fails the merge.
            kept_chapters = drop_zero_length_chapters(chapters_list)
            dropped = len(chapters_list) - len(kept_chapters)
            if dropped:
                log.info(f"PREPARE ({asin}): Dropped {dropped} zero-length chapter(s) after sanitizing.")
            chapters_list = kept_chapters

        # 2. Time-Based Auto-Chunking (see _should_auto_chunk for the gate).
        # The answer is kept in a variable because the per-chapter split gate
        # below needs it too (D7: a synthetic "Part N" book is never split).
        auto_chunked = _should_auto_chunk(lossless, audio_file, chapters_list, effective_total_sec)
        if auto_chunked:
            log.info(f"PREPARE ({asin}): Single chapter detected. Applying auto-chunking (15m).")
            new_chapters = []
            num_chunks = int(effective_total_sec // AUTO_CHUNK_SIZE_SEC)
            if effective_total_sec % AUTO_CHUNK_SIZE_SEC > 0:
                num_chunks += 1

            for i in range(num_chunks):
                start_sec = i * AUTO_CHUNK_SIZE_SEC
                end_sec = min((i + 1) * AUTO_CHUNK_SIZE_SEC, effective_total_sec)
                new_chapters.append(
                    {
                        "title": f"Part {i + 1}",
                        "start_offset_ms": int(start_sec * 1000),
                        "length_ms": int((end_sec - start_sec) * 1000),
                    }
                )
            chapters_list = new_chapters

        # 2b. Per-chapter splitting decision (v0.24.0). Everything here is inert
        # while `conversion.chapters.split_by_chapter` is off — which is the
        # default — so `split_output` stays False, the chapter list is exactly
        # the one today's pipeline produces, and no extra file is written.
        #
        # The gates, in order:
        #   - D7: a synthetic auto-chunked "Part N" book is never split. Those
        #     markers exist only to give the re-encode parallel work; cutting
        #     them into files would ship the book as a pile of arbitrary
        #     15-minute slices.
        #   - D6: the minimum-duration merge runs AFTER the existing transform
        #     chain, folding the very list the user would otherwise have got.
        #   - D7 again: at least two chapters must survive that merge. One
        #     chapter is not a split, it is the single file we already make.
        #
        # All three output formats pass this gate (Phase 3 — Phase 2 shipped the
        # AAC path alone and skipped the other two here, because reshaping their
        # chapter markers before anything could split them would have changed
        # their single-file output). What differs per format is only HOW each
        # part is cut, which is what `split_encode_mode` below names:
        #   "aac"  — the chunked AAC re-encode, exactly as Phase 2 (also where a
        #            lossless title whose decrypt fell back to FLAC ends up, since
        #            a FLAC master cannot be copied into an .m4b part).
        #   "copy" — the lossless variant: the same cut with "-c copy" instead of
        #            encode flags, straight off the AAC master (D14's spike put
        #            the worst boundary error at 7.7 ms).
        #   "mp3"  — N independent LAME encodes in place of the single-pass one.
        split_output = False
        part_titles = None
        book_tags_path = None
        split_encode_mode = None
        mp3_source_bitrate_bps = None
        mp3_source_sample_rate = None
        if chapter_settings.get("split_by_chapter", False) and not auto_chunked:
            # Read off the settings, like the branding-trim gate above, rather
            # than off the caller's `lossless` flag: they are the same axis (the
            # orchestrator sets that flag from this very value), and one of them
            # has to be authoritative here.
            output_format = resolve_output_format(settings)
            # Named for the unsplit path it was written for, where this exact
            # condition is what sends a book to the lossless remux. In SPLIT mode
            # it only means "the master is AAC, so its chapters can be copy-cut"
            # — such a book never reaches _remux_and_finalize at all. Same test,
            # two destinations; kept as one name so the call sites stay put.
            will_remux_lossless = output_format == "original" and audio_file.lower().endswith(".m4b")
            # The setting is in SECONDS; the transform takes milliseconds.
            # A hand-edited settings.json can hold anything, and anything
            # unusable means "no merge" rather than an exception.
            try:
                min_ms = max(0, int(float(chapter_settings.get("minimum_file_duration", 0) or 0))) * 1000
            except (TypeError, ValueError):
                min_ms = 0
            merged_chapters = merge_short_chapters(chapters_list, min_ms)
            if len(merged_chapters) >= 2:
                merged_away = len(chapters_list) - len(merged_chapters)
                if merged_away:
                    log.info(
                        f"PREPARE ({asin}): Merged {merged_away} chapter(s) shorter than "
                        f"{min_ms / 1000:g}s into the chapter that follows them."
                    )
                chapters_list = merged_chapters
                split_output = True
                if output_format == "mp3":
                    split_encode_mode = "mp3"
                elif will_remux_lossless:
                    # See above: here the flag reads as "the master is AAC and
                    # can be cut with -c copy", not "this book will be remuxed".
                    split_encode_mode = "copy"
                else:
                    split_encode_mode = "aac"
                log.info(
                    f"PREPARE ({asin}): Splitting this book into {len(chapters_list)} chapter file(s) "
                    f"({split_encode_mode} parts)."
                )
            else:
                # Deliberately keep the UNMERGED list here: the book is going
                # out as a single file after all, and its chapter markers
                # should be the ones every other single-file conversion
                # produces rather than a merge nothing consumed.
                log.info(
                    f"PREPARE ({asin}): Per-chapter splitting is on, but fewer than two chapters survive the "
                    f"minimum-duration merge; writing a single file."
                )

        # The MP3 quality flags depend on the master's own bitrate/sample rate
        # (see build_mp3_flags), which the single-pass encoder probes once for
        # the whole book. Probe once here for the same reason: N parts asking N
        # times would cost N ffprobes for one answer that cannot change, and a
        # probe that failed for only some of them would give a single book parts
        # at two different bitrates.
        #
        # What this freezes into the context is the PROBE RESULT, not the quality
        # settings: those are still read per chunk in encode_chapter_chunk, the
        # same way the AAC path has always read conversion.quality per chunk.
        if split_encode_mode == "mp3":
            try:
                mp3_source_bitrate_bps, mp3_source_sample_rate = _probe_source_audio_params(audio_file, job_id)
            except _ProbeCancelled:
                # A cancelled probe is the job stopping, not an unreadable
                # master: take the clean cancel exit rather than falling into the
                # generic handler below, which would mark the book ERROR.
                log.info(f"PREPARE ({asin}): Cancelled during the MP3 source probe.")
                return None, None
            if mp3_source_bitrate_bps is None and mp3_source_sample_rate is None:
                # Not fatal — build_mp3_flags falls back per its spec — but with
                # "match source bitrate" on, a silent failure quietly changes
                # every part's bitrate. Say so once, so app.log can answer "why
                # are these not at the source bitrate?" later.
                log.warning(
                    f"PREPARE ({asin}): Could not read the master's audio parameters; the chapter files will "
                    f"use the configured MP3 bitrate instead of matching the source."
                )

        # User metadata overrides (custom title/author) win over the Audible
        # values for the embedded tags, matching what the UI displays.
        with get_db_connection() as con:
            overrides = con.execute(
                "SELECT custom_title, custom_author FROM audiobooks WHERE asin = ?", (asin,)
            ).fetchone()
        custom_title = overrides["custom_title"] if overrides else None
        custom_author = overrides["custom_author"] if overrides else None

        # 3. Write Metadata File
        chapter_txt_path = os.path.join(temp_dir, "chapters.txt")
        with open(chapter_txt_path, "w", encoding="utf-8") as f:
            f.write(";FFMETADATA1\n")

            # --- Standard Tags ---
            # strip_unabridged applies ONLY to the Audible-derived title, never
            # to a user's custom_title (we never mangle explicit user input).
            strip_unabridged_flag = chapter_settings.get("strip_unabridged", False)
            if custom_title:
                title = custom_title
            else:
                raw_title = book_info.get("title", "N/A")
                title = strip_unabridged(raw_title) if strip_unabridged_flag else raw_title
            f.write(f"title={title}\n")
            f.write(f"album={title}\n")  # Players often treat Audiobooks as Albums

            authors = custom_author or ", ".join([a.get("name", "N/A") for a in book_info.get("authors", [])])
            f.write(f"artist={authors}\n")
            f.write(f"album_artist={authors}\n")

            narrators = ", ".join([n.get("name", "N/A") for n in book_info.get("narrators", [])])
            f.write(f"composer={narrators}\n")  # 'Composer' is the standard field for Narrator in M4B

            # Genre Extraction (e.g., "Fiction > Sci-Fi")
            categories = []
            if book_info.get("category_ladders"):
                for ladder in book_info["category_ladders"]:
                    if ladder.get("ladder"):
                        # Get the last (most specific) category name
                        categories.append(ladder["ladder"][-1].get("name", ""))
            genre_str = ", ".join(filter(None, categories))
            if genre_str:
                f.write(f"genre={genre_str}\n")

            # Dates
            release_date = book_info.get("release_date", "") or ""
            f.write(f"date={release_date}\n")  # Full YYYY-MM-DD
            release_year = release_date.split("-")[0] if release_date else ""
            f.write(f"year={release_year}\n")

            # Copyright (Prefer API data over file probe)
            copyright_info = book_info.get("copy_right")
            if not copyright_info:
                # Fallback to probe if API is empty
                ffprobe_command = ["ffprobe"] + [audio_file]
                probe_result = _run_registered(ffprobe_command, job_id, encoding="utf-8", errors="replace")
                for line in probe_result.stderr.splitlines():
                    if "copyright" in line.lower():
                        if ":" in line:
                            copyright_info = line.split(":", 1)[1].strip()
                        else:
                            copyright_info = line.strip()
                        break
            f.write(f"copyright={copyright_info or 'Unknown'}\n")

            # Summary / Description
            summary = (
                (book_info.get("merchandising_summary") or "")
                .replace("</p>", "\n")
                .replace("<p>", "")
                .replace("<br />", "\n")
                .strip()
            )
            f.write(f"description={summary}\n")
            f.write(f"comment={summary}\n")  # 'comment' is often used by players for the long description

            # --- Custom / Extended Tags (Uppercase for custom keys) ---
            f.write(f"PUBLISHER={book_info.get('publisher_name', 'N/A')}\n")
            f.write(f"LANGUAGE={book_info.get('language', 'N/A')}\n")
            f.write(f"AUDIBLE_ASIN={asin}\n")  # Used by some library tools to match metadata
            f.write(f"asin={asin}\n")  # Lowercase fallback

            # Series Info
            if book_info.get("series"):
                f.write(f"series={book_info['series'][0].get('title', 'N/A')}\n")
                f.write(f"series-part={book_info['series'][0].get('sequence', 'N/A')}\n")

            # Write chapters. Each chapter title is rendered through the
            # per-chapter template; the default "{ch_title}" reproduces the raw
            # chapter title (today's byte-for-byte output).
            chapter_title_template = chapter_settings.get("chapter_title_template", "{ch_title}")
            ch_total = len(chapters_list)
            # Kept so a split book's per-part titles are the SAME strings this
            # writer puts in the chapter atoms (D8) — one rendering, one place it
            # can drift from. Unused (and returned as None) when not splitting.
            rendered_titles = []
            for idx, chapter in enumerate(chapters_list):
                f.write("[CHAPTER]\nTIMEBASE=1/1000\n")
                f.write(f"START={chapter.get('start_offset_ms', 0)}\n")
                f.write(f"END={chapter.get('start_offset_ms', 0) + chapter.get('length_ms', 0)}\n")
                rendered_title = render_chapter_title(
                    chapter_title_template,
                    ch_num=idx + 1,
                    ch_total=ch_total,
                    ch_title=chapter.get("title", "Chapter"),
                    book_title=title,
                )
                rendered_titles.append(rendered_title)
                f.write(f"title={rendered_title}\n")

        # 4. Split mode only: the book-level tag block on its own, for the
        #    per-part FFMETADATA files the chunk encodes write (D8 — each part
        #    carries the book's tags and its own title, with no chapter atoms,
        #    because the file IS the chapter). Sliced off the file just written
        #    rather than duplicating the tag writer above.
        if split_output:
            part_titles = rendered_titles
            book_tags_path = _write_book_tags_file(temp_dir, chapter_txt_path)

        return {
            "decryption_args": decryption_args,
            "audio_file": audio_file,
            "cover_file": cover_file,
            "pdf_file": pdf_file,
            "chapter_file": chapter_txt_path,
            "chapters": chapters_list,
            "book_info": book_info,
            # Output duration in seconds, carried so the single-pass MP3 encode
            # (Phase 5) has a denominator for its -progress percentage without a
            # second ffprobe — and, when the branding trim is active, so it knows
            # where to stop (-t). This is the TRIMMED length whenever the trim
            # applied, and the raw master duration otherwise.
            "total_duration_sec": effective_total_sec,
            # Branding trim spans in ms, both 0 when the trim is inactive. The
            # intro is added back to every source seek (the chapter starts above
            # are output-timeline); the outro is carried only so the encoders can
            # tell an outro-only trim from no trim at all.
            "trim_intro_ms": trim_intro_ms,
            "trim_outro_ms": trim_outro_ms,
            # Populated only when retain_aax is on (both None otherwise); copied
            # next to the finished book by BookProcessor._place_sidecar_files.
            "raw_audio_file": retained_raw_audio_file,
            "voucher_file": retained_voucher_file,
            # Populated only when save_annotations is on AND the title has
            # annotations (None otherwise); copied next to the finished book by
            # BookProcessor._place_sidecar_files.
            "annotations_file": annotations_file,
            # Per-chapter splitting (v0.24.0). False for every book today's
            # pipeline produces, in which case the two keys below are None and
            # nothing downstream behaves differently. True means each chapter in
            # "chapters" becomes its own output file: the encode tags each chunk
            # instead of stripping its metadata, and the orchestrator finalizes
            # the chunks in place instead of merging them.
            "split_output": split_output,
            # The rendered per-part titles, index-aligned with "chapters".
            "part_titles": part_titles,
            # FFMETADATA holding the book-level tags with no chapter atoms; the
            # base every per-part metadata file is built on.
            "book_tags_file": book_tags_path,
            # How each part is cut: "aac" (re-encode), "copy" (lossless cut off
            # the AAC master) or "mp3" (per-part LAME). None when not splitting;
            # encode_chapter_chunk reads it as "aac" in that case, which is the
            # single behavior that path had before splitting existed.
            "split_encode_mode": split_encode_mode,
            # The master's own bitrate/sample rate, probed once for the whole
            # book. Populated only for "mp3" parts (the one variant whose flags
            # depend on them); either value may be None when the probe could not
            # read it, which build_mp3_flags handles.
            "mp3_source_bitrate_bps": mp3_source_bitrate_bps,
            "mp3_source_sample_rate": mp3_source_sample_rate,
        }, None

    except Exception as e:
        # SIGTERM (-15) on the registered duration probe here is a cancellation,
        # not a corrupt file — surface it as the clean cancel signal.
        if isinstance(e, subprocess.CalledProcessError) and e.returncode == -15:
            log.info(f"PREPARE ({asin}): Cancelled during metadata/chapter phase.")
            return None, None
        log.error(f"PREPARE ({asin}): Failed during metadata/chapter phase: {e}", exc_info=True)
        return None, f"Failed during metadata/chapter processing: {e}"


def _write_book_tags_file(temp_dir, chapter_file_path):
    """
    Write "<temp_dir>/book_tags.txt": the book-level tag block of the FFMETADATA
    file at `chapter_file_path`, with the chapter atoms and the book's own
    `title=` line removed. Returns the path.

    Two removals, for two different reasons. The `[CHAPTER]` sections go because
    a split book's part IS one chapter — interior chapter markers describing the
    whole book would be wrong in every part (D8). The `title=` line goes because
    each part overrides it with its own rendered chapter title; leaving the book
    title in front of that would work only by relying on ffmpeg's last-key-wins
    parsing of a repeated key. `album=` deliberately stays: it holds the book
    title, which is what groups the parts together in a player.

    Line-oriented, matching how ffmpeg reads the file the tag writer produces —
    that writer does not escape the newlines inside `description=`, so ffmpeg
    already sees each of those continuation lines as its own key.
    """
    kept = []
    with open(chapter_file_path, encoding="utf-8") as f:
        for line in f:
            if line.startswith("[CHAPTER]"):
                break
            if line.startswith("title="):
                continue
            kept.append(line)

    book_tags_path = os.path.join(temp_dir, "book_tags.txt")
    with open(book_tags_path, "w", encoding="utf-8") as f:
        f.writelines(kept)
    return book_tags_path


def _write_part_metadata(asin, temp_dir, chunk_index, total_chunks, context):
    """
    Write the FFMETADATA file for ONE per-chapter output file and return its
    path, or None if it could not be written (which fails the chunk, and with it
    the book — an untagged part is not an acceptable output).

    Contents: the book-level tags (see _write_book_tags_file), then this part's
    own title — the very string the chapter atom would have carried, rendered
    once by prepare from `conversion.chapters.chapter_title_template` — and
    `track=N/M`. No `[CHAPTER]` section: the file is the chapter.

    What `track=N/M` actually becomes on disk depends on the container. An .m4b
    part is written with `-movflags +use_metadata_tags`, which makes the mp4
    muxer write Apple `mdta` keys instead of the classic iTunes `ilst` atoms, so
    the value is carried as an `mdta` key rather than a `trkn` atom; that is the
    same tag shape today's merged single-file books already ship with. An .mp3
    part gets an ordinary id3v2 `TRCK` frame instead. Either way part ORDER must
    not be assumed to come from a track atom: the durable ordering guarantee is
    the zero-padded `{ch}` in the part filenames (see render_chapter_filename),
    which sorts correctly in every file browser and player regardless of tags.

    The same file serves all three split variants — the tags a part carries do
    not depend on how its audio was cut.
    """
    part_titles = context.get("part_titles") or []
    part_title = part_titles[chunk_index] if chunk_index < len(part_titles) else ""

    header = ";FFMETADATA1\n"
    book_tags_file = context.get("book_tags_file")
    if book_tags_file:
        try:
            with open(book_tags_file, encoding="utf-8") as f:
                header = f.read()
        except OSError as e:
            # A part with only its title and track number is still a usable
            # file, and the alternative (failing the book) is worse.
            log.warning(f"ENCODE ({asin}): Could not read the book tag block: {e}. Tagging this part minimally.")
            header = ";FFMETADATA1\n"
    if not header.startswith(";FFMETADATA1"):
        header = ";FFMETADATA1\n" + header
    if not header.endswith("\n"):
        header += "\n"

    metadata_path = os.path.join(temp_dir, f"chunk_{chunk_index:03d}.ffmeta")
    try:
        with open(metadata_path, "w", encoding="utf-8") as f:
            f.write(header)
            f.write(f"title={part_title}\n")
            f.write(f"track={chunk_index + 1}/{total_chunks}\n")
    except OSError as e:
        log.error(f"ENCODE ({asin}): Could not write the metadata for chapter file {chunk_index + 1}: {e}")
        return None
    return metadata_path


def encode_chapter_chunk(asin, job_id, temp_dir, chunk_info, context):
    """
    Handles Phase 2 of conversion: encoding a single chapter of the book.
    This function is designed to be run in parallel in the global worker pool.

    In split mode the chunk is not an intermediate but one of the book's final
    files, and the context's "split_encode_mode" says how to cut it: "aac" (the
    re-encode this function has always done), "copy" (lossless, no encode) or
    "mp3" (per-part LAME, replacing the single-pass encoder). Everything else —
    the seek, the duration cap, the per-part tagging — is shared by all three.

    Args:
        asin (str): The ASIN of the book.
        job_id (int): The parent job ID for logging.
        temp_dir (str): The path to the temporary directory for this book.
        chunk_info (dict): A dictionary containing the 'index', 'start', and 'duration' for this chunk.
        context (dict): The context dictionary from the prepare_book_assets step.

    Returns:
        str: The path to the successfully encoded chunk file, or None on failure.
    """
    chunk_index = chunk_info["index"]
    total_chunks = chunk_info["total_chunks"]
    log.debug(f"ENCODE ({asin}): Starting encoding for chunk {chunk_index + 1}/{total_chunks}")

    settings = load_settings()
    quality = settings.get("conversion", {}).get("quality", "High")
    audio_flags = {
        "High": ["-c:a", "aac", "-b:a", "128k"],
        "Standard": ["-c:a", "aac", "-b:a", "96k"],
        "Low": ["-c:a", "aac", "-b:a", "64k"],
    }.get(quality, ["-c:a", "aac", "-b:a", "128k"])

    # Which of the three split variants this chunk is (v0.24.0 Phase 3; see the
    # 2b gate in prepare_book_assets). Outside split mode there is exactly one
    # behavior — the AAC re-encode this path has always done — so the mode is
    # not even consulted, and an older context that predates the key reads as
    # "aac" rather than raising.
    split_output = bool(context.get("split_output"))
    split_mode = (context.get("split_encode_mode") or "aac") if split_output else "aac"

    # The container follows the variant, because ffmpeg picks the muxer off the
    # extension: MP3 parts must be written as ".mp3" (and are the book's final
    # files), everything else stays in the ".m4b" the merge path also consumes.
    chunk_ext = ".mp3" if split_mode == "mp3" else ".m4b"
    output_path = os.path.join(temp_dir, f"chunk_{chunk_index:03d}{chunk_ext}")
    # Branding trim (Phase 6): chunk starts are in the trimmed OUTPUT timeline,
    # so the seek into the (untrimmed) master has to add the intro span back.
    # 0 when the trim is inactive, leaving the seek exactly as it was. The
    # duration needs no adjustment: it comes from the recomputed chapter lengths,
    # whose final entry was already shortened by the outro.
    source_start_sec = chunk_info["start"] + context.get("trim_intro_ms", 0) / 1000.0

    # Per-chapter splitting (v0.24.0). In split mode this chunk is not an
    # intermediate that the merge will tag afterwards — it IS one of the book's
    # output files — so it is tagged here, at encode time, from a per-part
    # FFMETADATA input instead of being stripped with "-map_metadata -1".
    #
    # The metadata input has to be spliced in BEFORE "-t": ffmpeg reads options
    # positionally, so an "-i" following "-t" would turn that duration cap into
    # an INPUT option on the metadata file and let the chunk run to the end of
    # the master. The same goes for the MP3 variant's cover input below. With
    # splitting off the assembled command is byte-for-byte the one this path has
    # always run.
    #
    # "-map_chapters -1" is load-bearing twice over (D8: the file IS the chapter,
    # so it carries no chapter atoms of its own). Without it ffmpeg copies the
    # chapter atoms of the first input that has any — the decrypted master, when
    # the AAC Copy strategy preserved them — sliced against this chunk's -ss/-t
    # window into a zero-length marker for the previous chapter, one spanning the
    # file, and one pinned to its end. On the "-c copy" variant it also drops the
    # master's QuickTime chapter TRACK, which would otherwise leave a dangling
    # tref/chap reference that makes every later ffmpeg/ffprobe read of the part
    # warn (D14's spike). The merge path never saw either problem because its
    # concat step rebuilds the chapter atoms from chapters.txt afterwards.
    if split_output:
        metadata_path = _write_part_metadata(asin, temp_dir, chunk_index, total_chunks, context)
        if not metadata_path:
            return None
        metadata_args = ["-i", metadata_path]

        # Cover art, MP3 only. The mp4 muxer cannot write an attached picture
        # alongside +use_metadata_tags, so .m4b parts get theirs from
        # AtomicParsley at finalize time exactly as the merged single file does;
        # MP3 has no such conflict and carries the APIC frame straight out of
        # this one pass, which is why _promote_split_parts skips AtomicParsley
        # for .mp3 parts. Input order: 0 = master, 1 = FFMETADATA, 2 = cover.
        cover_file = context.get("cover_file")
        inline_cover = split_mode == "mp3" and bool(cover_file and os.path.exists(cover_file))
        if inline_cover:
            metadata_args += ["-i", cover_file]

        output_args = ["-map", "0:a", "-map_metadata", "1", "-map_chapters", "-1"]
        if inline_cover:
            output_args += [
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
            ]

        if split_mode == "copy":
            # The lossless variant: no encode at all, just a cut. The surrounding
            # flags are the same ones the AAC variant uses, and D14's spike
            # confirmed they do not move the cut (identical PCM at the boundary).
            output_args += ["-c", "copy"]
        elif split_mode == "mp3":
            # The same LAME flag matrix the single-pass encoder resolves, off the
            # source parameters prepare probed once for the whole book.
            output_args += (
                ["-c:a", "libmp3lame"]
                + build_mp3_flags(
                    settings.get("conversion", {}).get("mp3", {}),
                    context.get("mp3_source_bitrate_bps"),
                    context.get("mp3_source_sample_rate"),
                )
                + ["-id3v2_version", "3"]
            )
        else:
            output_args += audio_flags

        if split_mode != "mp3":
            # mp4 only: `+use_metadata_tags` matches the merge path, which needs
            # it for the custom uppercase tags (PUBLISHER, AUDIBLE_ASIN,
            # series...); `+faststart` puts the moov atom first so a part starts
            # playing without reading the whole file. Neither exists for MP3,
            # where the id3v2 frames above already carry both.
            output_args += ["-movflags", "+faststart+use_metadata_tags"]
    else:
        metadata_args = []
        output_args = ["-map", "0:a"] + audio_flags + ["-map_metadata", "-1"]

    split_command = (
        ["ffmpeg", "-y", "-hide_banner", "-loglevel", "error"]
        + context["decryption_args"]
        + ["-ss", str(source_start_sec), "-i", context["audio_file"]]
        + metadata_args
        + ["-t", str(chunk_info["duration"])]
        + output_args
        + [output_path]
    )

    process = None
    try:
        # Switch to Popen to capture PID
        # FIX: Not using text=True here as ffmpeg output is usually binary/mixed, but handled decoding in exception
        log.debug(f"ENCODE ({asin}): Command: {' '.join(split_command)}")
        process = subprocess.Popen(split_command, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        process_registry.register(job_id, process)

        # Wait for finish
        _, stderr = process.communicate()

        if process.returncode != 0:
            # Handle Cancellation (SIGTERM is -15)
            if process.returncode == -15:
                log.info(f"ENCODE ({asin}): Chunk {chunk_index + 1} cancelled.")
                return None
            # Handle actual errors
            raise subprocess.CalledProcessError(process.returncode, split_command, stderr=stderr)

        log.debug(f"ENCODE ({asin}): Finished encoding chunk {chunk_index + 1}/{total_chunks}")
        return output_path

    except subprocess.CalledProcessError as e:
        # FIX: Safer decoding of stderr
        err_text = e.stderr.decode("utf-8", errors="replace") if isinstance(e.stderr, bytes) else str(e.stderr)
        log.error(f"ENCODE ({asin}): Failed to encode chunk {chunk_index + 1}. Stderr: {err_text}")
        return None
    finally:
        if process:
            process_registry.unregister(job_id, process)


def merge_book_chunks(asin, job_id, temp_dir, final_output_path, context, encoded_chunk_paths):
    """
    Handles Phase 3 of conversion: merging all encoded chapter chunks into
    a single, final .m4b file with all metadata and cover art.

    Args:
        asin (str): The ASIN of the book.
        job_id (int): The parent job ID for logging.
        temp_dir (str): The path to the temporary directory for this book.
        final_output_path (str): The absolute path for the final audiobook file.
        context (dict): The context dictionary from the prepare_book_assets step.
        encoded_chunk_paths (list): A list of paths to the successfully encoded chunks.

    Returns:
        bool: True on success, False on failure.
    """
    log.info(f"MERGE ({asin}): Starting final merge process...")
    _yield_progress(asin, "Merging final file...", 95, job_id)

    # Create the file list for ffmpeg's concat demuxer
    merge_list_path = os.path.join(temp_dir, "mergelist.txt")
    with open(merge_list_path, "w", encoding="utf-8") as f:
        # It's crucial that the paths are sorted correctly
        for chunk_path in sorted(encoded_chunk_paths):
            # Format for ffmpeg, quoting is not needed here
            f.write(f"file '{os.path.basename(chunk_path)}'\n")

    # Merge audio + chapters + metadata. The cover is deliberately NOT added
    # here: ffmpeg's mp4 muxer can't write an attached-picture stream together
    # with the custom "use_metadata_tags" fields — enabling the tags turns the
    # cover into an unusable 'bin_data' stream — so the cover is embedded in a
    # separate AtomicParsley step below, which keeps both the tags and the art.
    merge_command = (
        ["ffmpeg", "-y", "-f", "concat", "-safe", "0", "-i", merge_list_path]
        + ["-i", context["chapter_file"]]
        + ["-map", "0:a", "-map_metadata", "1", "-map_chapters", "1"]
        + ["-c", "copy"]  # Use fast, lossless copy since chunks are already encoded
        + ["-id3v2_version", "3"]
        + ["-movflags", "+faststart+use_metadata_tags", final_output_path]
    )

    process = None
    try:
        log.debug(f"MERGE ({asin}): Command: {' '.join(merge_command)}")
        # Using Popen to capture logs in real-time if needed for debugging
        # FIX: Added errors="replace"
        process = subprocess.Popen(
            merge_command,
            cwd=temp_dir,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
        process_registry.register(job_id, process)  # <--- REGISTER

        _, stderr = process.communicate()  # Wait for completion

        if process.returncode != 0:
            # Handle Cancellation
            if process.returncode == -15:
                log.info(f"MERGE ({asin}): Merge process cancelled.")
                return False
            raise subprocess.CalledProcessError(process.returncode, merge_command, stderr=stderr)

        log.info(f"MERGE ({asin}): Successfully merged and finalized file at {final_output_path}")
        _embed_cover_art(asin, job_id, final_output_path, context.get("cover_file"))
        return True

    except subprocess.CalledProcessError as e:
        log.error(f"MERGE ({asin}): Final merge failed. Stderr:\n{e.stderr}")
        return False
    finally:
        if process:
            process_registry.unregister(job_id, process)  # <--- UNREGISTER


def remux_book_lossless(asin, job_id, temp_dir, final_output_path, context):
    """
    Lossless finalize for no-re-encode mode: mux chapters + metadata onto the
    already-decrypted AAC master with `-c copy` (no transcode), then embed the
    cover. Used only when resolve_output_format(settings) == "original" AND the
    fast AAC-copy decrypt succeeded, so context["audio_file"] is the ".m4b"
    master.

    This is deliberately a separate, additive path — merge_book_chunks with a
    single "-i master" in place of the concat demuxer — so the load-bearing
    re-encode merge stays untouched. Same metadata/cover/cancellation handling.

    Returns True on success, False on failure.
    """
    log.info(f"REMUX ({asin}): Starting lossless remux (no re-encode)...")
    _yield_progress(asin, "Finalizing (lossless)...", 95, job_id)

    # Copy the audio straight through; only the chapters/metadata are (re)muxed.
    # The cover is embedded afterward via AtomicParsley for the same reason as
    # merge_book_chunks: +use_metadata_tags and an attached picture can't coexist
    # in ffmpeg's mp4 muxer.
    remux_command = (
        ["ffmpeg", "-y", "-hide_banner", "-loglevel", "error", "-i", context["audio_file"]]
        + ["-i", context["chapter_file"]]
        + ["-map", "0:a", "-map_metadata", "1", "-map_chapters", "1"]
        + ["-c", "copy"]
        + ["-id3v2_version", "3"]
        + ["-movflags", "+faststart+use_metadata_tags", final_output_path]
    )

    process = None
    try:
        log.debug(f"REMUX ({asin}): Command: {' '.join(remux_command)}")
        process = subprocess.Popen(
            remux_command,
            cwd=temp_dir,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
        process_registry.register(job_id, process)

        _, stderr = process.communicate()

        if process.returncode != 0:
            # Handle Cancellation (SIGTERM is -15), matching the merge path.
            if process.returncode == -15:
                log.info(f"REMUX ({asin}): Remux process cancelled.")
                return False
            raise subprocess.CalledProcessError(process.returncode, remux_command, stderr=stderr)

        log.info(f"REMUX ({asin}): Successfully remuxed lossless file at {final_output_path}")
        _embed_cover_art(asin, job_id, final_output_path, context.get("cover_file"))
        return True

    except subprocess.CalledProcessError as e:
        log.error(f"REMUX ({asin}): Lossless remux failed. Stderr:\n{e.stderr}")
        return False
    finally:
        if process:
            process_registry.unregister(job_id, process)


def build_mp3_flags(mp3_settings, source_bitrate_bps, source_sample_rate):
    """
    Resolve the ffmpeg libmp3lame quality/rate flags from the `conversion.mp3`
    settings block. Pure (no I/O) so the whole matrix is unit-testable; the
    source bitrate/sample rate are probed once by the caller and passed in
    (either may be None when the probe couldn't read them).

    Order of the emitted flags:
      1. Quality axis — VBR (`-q:a N`) when target == "quality", else CBR/ABR
         (`-b:a Nk`, plus `-abr 1` for ABR when not constant_bitrate). With
         match_source_bitrate on and a known source bitrate, the target kbps is
         rounded up to the nearest standard LAME bitrate (>= source, capped 320).
         The emitted bitrate is floored at 32 kbps so a cleared UI field can't
         produce "-b:a 0k".
      2. `-compression_level` from encoder_quality (LAME effort; 0 = best).
      3. `-ac 1` when downsample_mono.
      4. `-ar N` ONLY when the source sample rate exceeds a POSITIVE whole-number
         max_sample_rate (never upsample a source that's already at/below the
         cap). Any other cap — cleared/zero/negative, a bool, a string, or other
         junk from a hand-edited settings.json — omits the flag entirely rather
         than handing ffmpeg a value it would reject ("-ar 0", "-ar -1",
         "-ar True"); an unknown source sample rate omits it too.
    """
    mp3 = mp3_settings or {}
    flags = []

    target = mp3.get("target", "quality")
    if target == "bitrate":
        kbps = mp3.get("bitrate_kbps", 128)
        if kbps is None:
            # An explicit null in a hand-edited settings.json would blow up on the
            # max() below. Only None falls back — a 0 from a cleared UI field is
            # left alone so the 32 kbps floor there still applies.
            kbps = 128
        if mp3.get("match_source_bitrate", True) and source_bitrate_bps:
            needed_kbps = source_bitrate_bps / 1000
            kbps = next((b for b in MP3_STANDARD_BITRATES_KBPS if b >= needed_kbps), 320)
        # Floor the explicit bitrate: clearing the UI number field saves 0, which
        # would emit "-b:a 0k" and fail every encode with an opaque ffmpeg error.
        # 32 kbps is the lowest LAME rate that's still usable for spoken audio.
        flags += ["-b:a", f"{max(kbps, 32)}k"]
        # ABR (variable around a target) unless the user asked for true CBR.
        if not mp3.get("constant_bitrate", False):
            flags += ["-abr", "1"]
    else:
        flags += ["-q:a", str(mp3.get("vbr_quality", 2))]

    # LAME effort. "High" spends the most CPU for the best quality (level 0).
    compression = {"High": "0", "Standard": "2", "Fast": "7"}.get(mp3.get("encoder_quality", "High"), "0")
    flags += ["-compression_level", compression]

    if mp3.get("downsample_mono", False):
        flags += ["-ac", "1"]

    # The cap must be a POSITIVE WHOLE number to be emitted: the UI's min/max
    # attributes are decorative (the page reads the field with a bare Number()),
    # so a typed "-1" reaches here and "-ar -1" would fail every encode. A zero or
    # negative cap is treated as "no cap", exactly like a missing source sample
    # rate. Two more shapes survive a hand-edited settings.json: `bool` is an
    # `int` subclass, so a bare `true` would be formatted as "-ar True", and a
    # float would be formatted as "-ar 44100.0" — ffmpeg rejects both. A bool is
    # not a rate at all so it means "no cap"; a float is a plausible way to write
    # a rate, so it's truncated to whole Hz instead of being thrown away.
    max_sample_rate = mp3.get("max_sample_rate", 44100)
    if isinstance(max_sample_rate, bool) or not isinstance(max_sample_rate, (int, float)):
        max_sample_rate = None
    elif isinstance(max_sample_rate, float):
        try:
            max_sample_rate = int(max_sample_rate)
        except (OverflowError, ValueError):
            # json.load() accepts Infinity/NaN; neither is a sample rate.
            max_sample_rate = None
    if source_sample_rate and max_sample_rate is not None and 0 < max_sample_rate < source_sample_rate:
        flags += ["-ar", str(max_sample_rate)]

    return flags


class _ProbeCancelled(Exception):
    """Raised when the MP3 path's source probe was SIGTERMed by a job cancel.

    A cancelled probe and a failed probe both come back with no values, but they
    mean opposite things to the caller: a failure is benign (encode anyway with
    fallback flags), while a cancellation must stop the encode from starting at
    all. Kept module-private — `encode_book_mp3` is the only caller.
    """


def _probe_source_audio_params(master_file, job_id):
    """
    Read the master audio stream's bit_rate and sample_rate via one ffprobe.
    Returns (bit_rate_bps, sample_rate_hz) as ints, each None when the value is
    absent (e.g. a FLAC master often reports no stream bit_rate) or the probe
    fails. Registered with process_registry so a cancel can reach it; a None
    result is acceptable — build_mp3_flags falls back per its spec.

    Raises `_ProbeCancelled` when the probe exited on SIGTERM (-15), which is the
    job being cancelled rather than an unreadable master.
    """
    probe_cmd = [
        "ffprobe",
        "-v",
        "error",
        "-select_streams",
        "a:0",
        "-show_entries",
        "stream=bit_rate,sample_rate",
        "-of",
        "default=noprint_wrappers=1",
        master_file,
    ]
    res = _run_registered(probe_cmd, job_id, encoding="utf-8", errors="replace")
    if res.returncode == -15:
        raise _ProbeCancelled()
    if res.returncode != 0:
        return None, None

    def _parse_int(raw):
        raw = (raw or "").strip()
        try:
            return int(raw)
        except (TypeError, ValueError):
            return None

    values = {}
    for line in res.stdout.splitlines():
        if "=" in line:
            key, _, val = line.partition("=")
            values[key.strip()] = val.strip()

    return _parse_int(values.get("bit_rate")), _parse_int(values.get("sample_rate"))


def encode_book_mp3(asin, job_id, temp_dir, final_output_path, context, stop_event=None):
    """
    Single-pass MP3 (LAME) encode of the whole book: one ffmpeg run muxes the
    decrypted master's audio, the FFMETADATA chapters/tags, and (when present)
    the cover as an id3v2 APIC attached picture straight into the final .mp3.

    Deliberately NOT chunked-parallel like the AAC path: concatenating
    independently-encoded MP3 chunks accumulates LAME encoder-delay/padding gaps
    and chapter drift, so the whole title is encoded in one gapless pass. There
    is no `-movflags` and no AtomicParsley here (both are mp4-only) — MP3 carries
    chapters as id3v2 CHAP frames and the cover as APIC in the same pass.

    Structured like remux_book_lossless (Popen + process_registry in try/finally,
    -15 -> cancelled/False, stderr summarized on failure). ffmpeg writes to a
    sibling ".part" file that is renamed into place only after a clean exit, so a
    failed or cancelled encode can never leave a truncated book at the library
    path for a later deep sync to adopt; the ".part" file itself is discarded in
    the `finally`, so an exception escaping this function can't orphan it either.
    Progress is driven by ffmpeg's `-progress pipe:1` stream, occupying the 30..90
    band; the final verify/finalize takes it to 100.

    `stop_event` is the job's cancellation flag (optional; a caller that doesn't
    pass one behaves exactly as before). The registry's cancel is a one-shot
    snapshot of the processes running at that instant, so a kill that landed on
    the source probe above is already spent by the time the encoder spawns —
    hence the recheck immediately before the spawn, plus the probe's own
    cancellation signal. Both bail before ffmpeg starts, so no ".part" file
    exists to clean up.

    Returns True on success, False on failure or cancellation.
    """
    log.info(f"MP3 ({asin}): Starting single-pass MP3 encode...")
    _yield_progress(asin, "Encoding MP3...", 30, job_id)

    settings = load_settings()
    mp3_settings = settings.get("conversion", {}).get("mp3", {})

    master = context["audio_file"]
    chapter_file = context["chapter_file"]
    cover_file = context.get("cover_file")
    total_duration_sec = context.get("total_duration_sec")
    have_cover = bool(cover_file and os.path.exists(cover_file))

    # Branding trim (Phase 6): skip into the master past the brand intro and stop
    # after the trimmed length (context's total_duration_sec is already trimmed),
    # which drops the outro. Both spans are 0 when the trim is inactive, and then
    # neither flag is added at all — the command stays exactly as it was.
    trim_intro_ms = context.get("trim_intro_ms", 0)
    trim_outro_ms = context.get("trim_outro_ms", 0)
    trim_active = bool(total_duration_sec and (trim_intro_ms or trim_outro_ms))

    try:
        source_bitrate_bps, source_sample_rate = _probe_source_audio_params(master, job_id)
    except _ProbeCancelled:
        log.info(f"MP3 ({asin}): Cancelled during the source probe; not starting the encoder.")
        return False
    quality_flags = build_mp3_flags(mp3_settings, source_bitrate_bps, source_sample_rate)

    # Input order: 0 = audio master, 1 = FFMETADATA, 2 = cover (when present).
    command = ["ffmpeg", "-y", "-hide_banner", "-loglevel", "error", "-progress", "pipe:1"]
    if trim_active:
        # Input-side seek: must precede the master's -i to apply to input 0 only
        # (the FFMETADATA/cover inputs are unaffected).
        command += ["-ss", str(trim_intro_ms / 1000.0)]
    command += ["-i", master, "-i", chapter_file]
    if have_cover:
        command += ["-i", cover_file]
    command += ["-map", "0:a", "-map_metadata", "1", "-map_chapters", "1"]
    if have_cover:
        command += [
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
        ]
    command += ["-c:a", "libmp3lame"] + quality_flags + ["-id3v2_version", "3"]
    if trim_active:
        # Output-side duration cap: the trimmed length, measured from the -ss
        # point above.
        command += ["-t", str(total_duration_sec)]
    # Encode to a ".part" file next to the destination, never straight to the
    # library path. A cancelled or failed encode would otherwise leave a
    # truncated .mp3 behind — and unlike a half-written .m4b (no moov atom, so
    # ffprobe rejects it) a truncated MP3 is perfectly readable, ID3 tags and
    # all, so the next deep sync would adopt it and mark the book DOWNLOADED.
    # Same directory means the same filesystem, so the rename below is atomic.
    part_path = final_output_path + ".part"
    # ffmpeg picks the output muxer from the file extension, and ".part" maps to
    # nothing ("Unable to choose an output format"), so the format has to be named
    # explicitly the moment the real ".mp3" suffix is hidden behind it.
    command += ["-f", "mp3", part_path]

    process = None
    stderr_chunks = []
    # Flipped only once the finished encode has been renamed into place. Every
    # other way out of the try below — an ffmpeg failure, a cancellation, or an
    # exception type this function does not handle (which still propagates) —
    # leaves it False, and the single cleanup point in `finally` discards the
    # partial. Nothing else is watching /data for orphaned ".part" files.
    promoted = False

    def _discard_partial():
        """Best-effort cleanup of the ".part" file after a failed/cancelled run."""
        try:
            os.remove(part_path)
        except OSError as e:
            log.debug(f"MP3 ({asin}): Could not remove partial encode '{part_path}': {e}")

    try:
        # Last check before a multi-hour encode commits: a cancel that arrived
        # while this book's assets were still being probed has already had its
        # one shot at the process registry, so nothing would reach this ffmpeg.
        # Mirrors the between-tasks _cancelled() checks on the chunked path.
        if stop_event is not None and stop_event.is_set():
            log.info(f"MP3 ({asin}): Cancelled before the encoder started.")
            return False

        log.debug(f"MP3 ({asin}): Command: {' '.join(command)}")
        process = subprocess.Popen(
            command,
            cwd=temp_dir,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
        process_registry.register(job_id, process)

        # ffmpeg writes progress to stdout (pipe:1) and errors to stderr. Drain
        # stderr concurrently so a full stderr pipe can't deadlock the child
        # while we read progress; keep it for the failure summary.
        def _drain_stderr():
            if process.stderr:
                stderr_chunks.append(process.stderr.read())

        stderr_thread = Thread(target=_drain_stderr, daemon=True)
        stderr_thread.start()

        last_percent = -1
        for line in iter(process.stdout.readline, ""):
            line = line.strip()
            # ffmpeg's out_time_ms is actually MICROSECONDS (long-standing quirk).
            if line.startswith("out_time_ms=") and total_duration_sec:
                try:
                    out_sec = int(line.split("=", 1)[1]) / 1_000_000
                except ValueError:
                    continue
                progress = 30 + min(60, int(out_sec / total_duration_sec * 60))
                if progress != last_percent:
                    last_percent = progress
                    _yield_progress(asin, "Encoding MP3...", progress, job_id)

        returncode = process.wait()
        stderr_thread.join()

        if returncode != 0:
            if returncode == -15:
                log.info(f"MP3 ({asin}): Encode cancelled.")
                return False
            stderr_text = "".join(stderr_chunks)
            reason = _summarize_subprocess_error(
                subprocess.CalledProcessError(returncode, command, stderr=stderr_text),
                "MP3 encode failed.",
            )
            log.error(f"MP3 ({asin}): Encode failed: {reason}")
            return False

        # Promote the finished encode into the library in one atomic rename, so
        # the final path never exists in a half-written state.
        try:
            os.replace(part_path, final_output_path)
        except OSError as e:
            log.error(f"MP3 ({asin}): Could not move the finished encode into place: {e}")
            return False
        promoted = True

        log.info(f"MP3 ({asin}): Successfully encoded MP3 at {final_output_path}")
        return True

    except OSError as e:
        log.error(f"MP3 ({asin}): Could not run ffmpeg for MP3 encode: {e}")
        return False
    finally:
        if process:
            process_registry.unregister(job_id, process)
        # The one cleanup point for the ".part" file, covering the failure and
        # cancellation returns above AND any exception this function doesn't
        # handle (an unreadable progress stream, a broken pipe mid-drain): those
        # still propagate to the caller, but no longer leave a truncated MP3
        # sitting in /data, where — unlike a moov-less .m4b — it stays fully
        # probe-readable and a later deep sync would adopt it as DOWNLOADED.
        # Only ffmpeg creates that file, so a bail before the spawn (a set stop
        # event, a Popen that never started) has nothing to clean up.
        if process and not promoted:
            _discard_partial()


def _embed_cover_art(asin, job_id, output_path, cover_file):
    """
    Add cover art to the finished .m4b via AtomicParsley, which writes the
    standard mp4 `covr` atom without disturbing the metadata ffmpeg already
    wrote (see the note in merge_book_chunks on why ffmpeg can't do both).

    Best-effort: a missing cover or an AtomicParsley failure logs a warning but
    does not fail the book — a book without embedded art is still usable.

    Despite the leading underscore this helper is DELIBERATELY consumed from
    another module: processing_logic's split finalize (D8) covers each
    per-chapter .m4b with it while the part is still in the temp dir, so a split
    book's art is embedded by exactly the same code path as a merged book's.
    Treat it as having an external caller before renaming or reshaping it.
    (AtomicParsley is mp4-only, so .mp3 parts are not routed here at all — they
    mux their APIC frame inline during the encode.)
    """
    if not cover_file or not os.path.exists(cover_file):
        log.warning(f"MERGE ({asin}): No cover file available; skipping cover art embedding.")
        return

    cover_cmd = ["AtomicParsley", output_path, "--artwork", cover_file, "--overWrite"]
    process = None
    try:
        process = subprocess.Popen(
            cover_cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, encoding="utf-8", errors="replace"
        )
        process_registry.register(job_id, process)
        _, stderr = process.communicate()
        if process.returncode == 0:
            log.info(f"MERGE ({asin}): Embedded cover art.")
        elif process.returncode != -15:  # -15 is cancellation, not a failure
            log.warning(f"MERGE ({asin}): Cover art embedding failed (exit {process.returncode}): {stderr}")
    except OSError as e:
        log.warning(f"MERGE ({asin}): Could not run AtomicParsley for cover art: {e}")
    finally:
        if process:
            process_registry.unregister(job_id, process)

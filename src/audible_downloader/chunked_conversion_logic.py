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
from threading import Lock

from . import (
    DATABASE_DIR,
    announcer,  # Import announcer for progress updates
)
from .db import get_db_connection
from .logger import log
from .process_registry import process_registry
from .settings import load_settings

# A lock to safely track progress across multiple threads, which will still be useful.
progress_lock = Lock()

# A single-chapter book longer than AUTO_CHUNK_TRIGGER_SEC is split into synthetic
# AUTO_CHUNK_SIZE_SEC "Part N" chapters so the per-chapter re-encode has parallel
# work (and the listener gets navigation points). Seconds.
AUTO_CHUNK_TRIGGER_SEC = 1800
AUTO_CHUNK_SIZE_SEC = 900


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

    want_pdf = load_settings().get("conversion", {}).get("download_supplementary_pdf", True)

    # Variables to hold state across the retry loops
    audio_file = None
    cover_file = None
    pdf_file = None
    book_info = None
    chapters_list = None
    decryption_args = []

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

            # Capture stderr while scanning it for progress. audible-cli writes
            # its error messages here too, but this loop drains the stream to
            # EOF, so a later process.stderr.read() would come back empty — keep
            # a bounded tail so a failure can be explained instead of surfacing
            # as a bare "non-zero exit status".
            stderr_tail = deque(maxlen=50)
            for line in iter(process.stderr.readline, ""):
                stderr_tail.append(line)
                match = re.search(r"(\d+)%", line)
                if match:
                    download_percent = int(match.group(1))
                    overall_progress = 5 + int(download_percent * 0.20)
                    _yield_progress(asin, f"Downloading... {download_percent}%", overall_progress, job_id)

            if process.wait() != 0:
                if process.returncode == -15:
                    log.info(f"PREPARE ({asin}): Download cancelled.")
                    return None, None
                # audible-cli writes its user-facing error to stdout (e.g.
                # "error: Asin ... not found in library."), which we never read
                # for progress — grab it now so the failure can be explained.
                stdout_text = process.stdout.read() if process.stdout else ""
                raise subprocess.CalledProcessError(
                    process.returncode, download_command, output=stdout_text, stderr="".join(stderr_tail)
                )

            process_registry.unregister(job_id, process)
            log.info(f"PREPARE ({asin}): Download finished.")

            # --- 2. Get Metadata & Detect Files ---
            _yield_progress(asin, "Preparing metadata...", 25, job_id)

            endpoint, params = f"/1.0/library/{asin}", "response_groups=media,contributors,series,category_ladders"
            meta_command = ["audible", "api", "-p", params, endpoint]
            result = subprocess.run(
                meta_command, capture_output=True, text=True, check=True, encoding="utf-8", errors="replace", env=env
            )
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
                res = subprocess.run(
                    ["audible", "activation-bytes"],
                    capture_output=True,
                    text=True,
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
                    # Run Decryption
                    d_proc = subprocess.Popen(decrypt_cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
                    process_registry.register(job_id, d_proc)
                    _, d_stderr = d_proc.communicate()
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
                    dur_res = subprocess.run(dur_cmd, capture_output=True, text=True)
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

                        v_proc = subprocess.run(verify_cmd, capture_output=True, text=True)
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
            # Clean up the raw encrypted file
            try:
                os.remove(raw_audio_file)
            except OSError:
                pass

            decryption_args = []  # Clear args as file is now clean

            # Load Chapters for next phase
            with open(json_file, encoding="utf-8") as cj:
                chapter_data = json.load(cj)
            chapters_list = chapter_data.get("content_metadata", {}).get("chapter_info", {}).get("chapters", [])

            break  # Break outer download loop

        except Exception as e:
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
        probe_result = subprocess.run(
            probe_cmd, capture_output=True, text=True, check=True, encoding="utf-8", errors="replace"
        )
        total_duration_sec = float(probe_result.stdout.strip())
        total_duration_ms = int(total_duration_sec * 1000)

        # 1. Sanitize Chapter Durations
        if chapters_list:
            log.info(f"PREPARE ({asin}): Sanitizing chapter durations...")
            chapters_list.sort(key=lambda x: x.get("start_offset_ms", 0))
            for i in range(len(chapters_list)):
                current_start = chapters_list[i].get("start_offset_ms", 0)
                if i < len(chapters_list) - 1:
                    next_start = chapters_list[i + 1].get("start_offset_ms", 0)
                    new_length = next_start - current_start
                else:
                    new_length = total_duration_ms - current_start
                chapters_list[i]["length_ms"] = max(0, new_length)

        # 2. Time-Based Auto-Chunking (see _should_auto_chunk for the gate).
        if _should_auto_chunk(lossless, audio_file, chapters_list, total_duration_sec):
            log.info(f"PREPARE ({asin}): Single chapter detected. Applying auto-chunking (15m).")
            new_chapters = []
            num_chunks = int(total_duration_sec // AUTO_CHUNK_SIZE_SEC)
            if total_duration_sec % AUTO_CHUNK_SIZE_SEC > 0:
                num_chunks += 1

            for i in range(num_chunks):
                start_sec = i * AUTO_CHUNK_SIZE_SEC
                end_sec = min((i + 1) * AUTO_CHUNK_SIZE_SEC, total_duration_sec)
                new_chapters.append(
                    {
                        "title": f"Part {i + 1}",
                        "start_offset_ms": int(start_sec * 1000),
                        "length_ms": int((end_sec - start_sec) * 1000),
                    }
                )
            chapters_list = new_chapters

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
            title = custom_title or book_info.get("title", "N/A")
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
                probe_result = subprocess.run(
                    ffprobe_command, capture_output=True, text=True, encoding="utf-8", errors="replace"
                )
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

            # Write chapters
            for chapter in chapters_list:
                f.write("[CHAPTER]\nTIMEBASE=1/1000\n")
                f.write(f"START={chapter.get('start_offset_ms', 0)}\n")
                f.write(f"END={chapter.get('start_offset_ms', 0) + chapter.get('length_ms', 0)}\n")
                f.write(f"title={chapter.get('title', 'Chapter')}\n")

        return {
            "decryption_args": decryption_args,
            "audio_file": audio_file,
            "cover_file": cover_file,
            "pdf_file": pdf_file,
            "chapter_file": chapter_txt_path,
            "chapters": chapters_list,
            "book_info": book_info,
        }, None

    except Exception as e:
        log.error(f"PREPARE ({asin}): Failed during metadata/chapter phase: {e}", exc_info=True)
        return None, f"Failed during metadata/chapter processing: {e}"


def encode_chapter_chunk(asin, job_id, temp_dir, chunk_info, context):
    """
    Handles Phase 2 of conversion: encoding a single chapter of the book.
    This function is designed to be run in parallel in the global worker pool.

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

    output_path = os.path.join(temp_dir, f"chunk_{chunk_index:03d}.m4b")
    split_command = (
        ["ffmpeg", "-y", "-hide_banner", "-loglevel", "error"]
        + context["decryption_args"]
        + ["-ss", str(chunk_info["start"]), "-i", context["audio_file"], "-t", str(chunk_info["duration"])]
        + ["-map", "0:a"]
        + audio_flags
        + ["-map_metadata", "-1", output_path]
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
    cover. Used only when conversion.no_reencode is on AND the fast AAC-copy
    decrypt succeeded, so context["audio_file"] is the ".m4b" master.

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


def _embed_cover_art(asin, job_id, output_path, cover_file):
    """
    Add cover art to the finished .m4b via AtomicParsley, which writes the
    standard mp4 `covr` atom without disturbing the metadata ffmpeg already
    wrote (see the note in merge_book_chunks on why ffmpeg can't do both).

    Best-effort: a missing cover or an AtomicParsley failure logs a warning but
    does not fail the book — a book without embedded art is still usable.
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

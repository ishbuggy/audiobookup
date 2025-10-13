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
from threading import Lock

from . import (
    DATABASE_DIR,
    announcer,  # Import announcer for progress updates
)
from .logger import log
from .settings import load_settings

# A lock to safely track progress across multiple threads, which will still be useful.
progress_lock = Lock()
00


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
    # Announce the update to all listening clients.
    announcer.announce(f"event: job_update\ndata: {json.dumps(payload)}\n\n")


def prepare_book_assets(asin, job_id, temp_dir):
    """
    Handles Phase 1 of conversion: downloading all necessary files from Audible,
    fetching metadata, and preparing the chapter file for ffmpeg.

    Args:
        asin (str): The ASIN of the book.
        job_id (int): The parent job ID for logging.
        temp_dir (str): The path to the temporary directory for this book.

    Returns:
        dict: A context dictionary containing paths and arguments for subsequent tasks.
              Returns None on failure.
    """
    log.info(f"PREPARE ({asin}): Starting asset preparation in {temp_dir}")
    env = os.environ.copy()
    env["HOME"] = DATABASE_DIR
    _yield_progress(asin, "Downloading...", 5, job_id)

    # --- 1. Download Book Files ---
    download_command = [
        "audible",
        "download",
        "-a",
        asin,
        "--aaxc",
        "--cover",
        "--cover-size",
        "1215",
        "--chapter",
        "-o",
        temp_dir,
    ]
    try:
        process = subprocess.Popen(
            download_command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, encoding="utf-8", env=env
        )
        for line in iter(process.stderr.readline, ""):
            match = re.search(r"(\d+)%", line)
            if match:
                download_percent = int(match.group(1))
                overall_progress = 5 + int(download_percent * 0.20)  # Download is ~20% of the job
                _yield_progress(asin, f"Downloading... {download_percent}%", overall_progress, job_id)
        if process.wait() != 0:
            raise subprocess.CalledProcessError(process.returncode, download_command, stderr=process.stderr.read())
        log.info(f"PREPARE ({asin}): Download finished.")
    except subprocess.CalledProcessError as e:
        log.error(f"PREPARE ({asin}): Download failed. Stderr: {e.stderr}")
        return None

    # --- 2. Get Metadata & Decryption Keys ---
    _yield_progress(asin, "Preparing metadata...", 25, job_id)
    try:
        endpoint, params = f"/1.0/library/{asin}", "response_groups=media,contributors,series,category_ladders"
        meta_command = ["audible", "api", "-p", params, endpoint]
        result = subprocess.run(meta_command, capture_output=True, text=True, check=True, encoding="utf-8", env=env)
        book_info = json.loads(result.stdout).get("item")

        def _find_file_by_ext(directory, extensions):
            for entry in os.scandir(directory):
                if entry.is_file() and any(entry.name.lower().endswith(ext) for ext in extensions):
                    return entry.path
            return None

        voucher_file = _find_file_by_ext(temp_dir, [".voucher"])
        audio_file = _find_file_by_ext(temp_dir, [".aax", ".aaxc"])
        cover_file = _find_file_by_ext(temp_dir, [".jpg", ".png"])
        json_file = _find_file_by_ext(temp_dir, [".json"])

        if not all([audio_file, cover_file, json_file]):
            raise FileNotFoundError("Missing one or more critical files after download.")

        decryption_args = []
        if voucher_file:
            with open(voucher_file) as f:
                voucher_data = json.load(f)
            key, iv = (
                voucher_data["content_license"]["license_response"]["key"],
                voucher_data["content_license"]["license_response"]["iv"],
            )
            decryption_args = ["-audible_key", key, "-audible_iv", iv]
        elif audio_file and audio_file.lower().endswith(".aax"):
            result = subprocess.run(
                ["audible", "activation-bytes"], capture_output=True, text=True, check=True, encoding="utf-8", env=env
            )
            activation_bytes = result.stdout.strip()
            if not activation_bytes:
                raise ValueError("audible-cli returned empty activation bytes.")
            decryption_args = ["-activation_bytes", activation_bytes]
        else:
            raise ValueError("No voucher or AAX file found for decryption.")

        # --- 3. Prepare FFmpeg Metadata File ---
        chapter_txt_path = os.path.join(temp_dir, "chapters.txt")
        log.info(f"PREPARE ({asin}): Writing metadata to {chapter_txt_path}")
        with open(chapter_txt_path, "w", encoding="utf-8") as f:
            f.write(";FFMETADATA1\n")
            f.write(f"title={book_info.get('title', 'N/A')}\n")
            authors = ", ".join([a.get("name", "N/A") for a in book_info.get("authors", [])])
            f.write(f"artist={authors}\n")
            narrators = ", ".join([n.get("name", "N/A") for n in book_info.get("narrators", [])])
            f.write(f"composer={narrators}\n")
            release_date = book_info.get("release_date", "") or ""
            release_year = release_date.split("-")[0] if release_date else ""
            f.write(f"year={release_year}\n")

            # This part requires an ffprobe call, which was also missing
            ffprobe_command = ["ffprobe"] + decryption_args + [audio_file]
            probe_result = subprocess.run(ffprobe_command, capture_output=True, text=True, encoding="utf-8")
            copyright_info = "Unknown"
            for line in probe_result.stderr.splitlines():
                if "copyright" in line.lower():
                    if ":" in line:
                        copyright_info = line.split(":", 1)[1].strip()
                    else:
                        copyright_info = line.strip()
                    break  # Stop after finding the first match
            f.write(f"copyright={copyright_info}\n")

            summary = (
                (book_info.get("merchandising_summary") or "")
                .replace("</p>", "\n")
                .replace("<p>", "")
                .replace("<br />", "\n")
                .strip()
            )
            f.write(f"description={summary}\n")
            f.write(f"asin={asin}\n")
            if book_info.get("series"):
                f.write(f"series={book_info['series'][0].get('title', 'N/A')}\n")
                f.write(f"series-part={book_info['series'][0].get('sequence', 'N/A')}\n")

            with open(json_file, encoding="utf-8") as cj:
                chapter_data = json.load(cj)
            chapters_list = chapter_data.get("content_metadata", {}).get("chapter_info", {}).get("chapters", [])
            for chapter in chapters_list:
                f.write("[CHAPTER]\nTIMEBASE=1/1000\n")
                f.write(f"START={chapter.get('start_offset_ms', 0)}\n")
                f.write(f"END={chapter.get('start_offset_ms', 0) + chapter.get('length_ms', 0)}\n")
                f.write(f"title={chapter.get('title', 'Chapter')}\n")
        with open(json_file, encoding="utf-8") as f:
            chapter_data = json.load(f)
        chapters = chapter_data.get("content_metadata", {}).get("chapter_info", {}).get("chapters", [])

        # --- 4. Return all context needed for later steps ---
        return {
            "decryption_args": decryption_args,
            "audio_file": audio_file,
            "cover_file": cover_file,
            "chapter_file": chapter_txt_path,
            "chapters": chapters,
            "book_info": book_info,
        }

    except Exception as e:
        log.error(f"PREPARE ({asin}): Failed during metadata preparation: {e}", exc_info=True)
        return None


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
    log.info(f"ENCODE ({asin}): Starting encoding for chunk {chunk_index + 1}/{total_chunks}")

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
    try:
        subprocess.run(split_command, check=True)
        log.info(f"ENCODE ({asin}): Finished encoding chunk {chunk_index + 1}/{total_chunks}")
        return output_path
    except subprocess.CalledProcessError as e:
        log.error(f"ENCODE ({asin}): Failed to encode chunk {chunk_index + 1}. Stderr: {e.stderr}")
        return None


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

    merge_command = (
        ["ffmpeg", "-y", "-f", "concat", "-safe", "0", "-i", merge_list_path]
        + ["-i", context["cover_file"], "-i", context["chapter_file"]]
        + ["-map", "0:a", "-map", "1:v", "-map_metadata", "2", "-map_chapters", "2"]
        + ["-c", "copy"]  # Use fast, lossless copy since chunks are already encoded
        + ["-id3v2_version", "3", "-disposition:v", "attached_pic"]
        + ["-movflags", "+faststart+use_metadata_tags"]
        + ["-metadata:s:v", 'title="Album cover"', "-metadata:s:v", 'comment="Cover (front)"', final_output_path]
    )

    try:
        # Using Popen to capture logs in real-time if needed for debugging
        process = subprocess.Popen(
            merge_command, cwd=temp_dir, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, encoding="utf-8"
        )
        _, stderr = process.communicate()  # Wait for completion

        if process.returncode != 0:
            raise subprocess.CalledProcessError(process.returncode, merge_command, stderr=stderr)

        log.info(f"MERGE ({asin}): Successfully merged and finalized file at {final_output_path}")
        return True
    except subprocess.CalledProcessError as e:
        log.error(f"MERGE ({asin}): Final merge failed. Stderr:\n{e.stderr}")
        return False

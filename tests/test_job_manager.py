# tests/test_job_manager.py

from contextlib import nullcontext
from threading import Event
from unittest import mock

from audible_downloader import job_manager


def _capture_con():
    con = mock.MagicMock()
    con.__enter__.return_value = con
    return con


def _final_status(con):
    # The worker's last jobs-table write is "UPDATE jobs SET status = ?, end_time = ?"
    # with (final_status, end_time, job_id); the initial RUNNING and the FAILED
    # writes inline the status, so this matches only the normal terminal write.
    for call in reversed(con.execute.call_args_list):
        sql = call.args[0]
        if "UPDATE jobs SET status = ?" in sql:
            return call.args[1][0]
    return None


class TestImportWorkerCancellation:
    """WF#4 (adversarial review): a cancel arriving *during* the final/only import
    file (the common single-file scan) must report CANCELLED, not COMPLETED — the
    stop_event is re-checked after each adoption, not only at the loop top."""

    def test_cancel_during_only_file_reports_cancelled(self):
        con = _capture_con()
        stop_event = Event()

        def adopt(path, job_id=None):
            # The cancel fires while the only candidate is being adopted, so the
            # loop-top check has already passed for this iteration.
            stop_event.set()
            return {"action": "imported", "key": "IMPORT-x"}

        with (
            mock.patch.object(job_manager, "get_db_connection", return_value=con),
            mock.patch.object(job_manager, "scan_data_dir_for_untracked", return_value=["/data/only.m4b"]),
            mock.patch.object(job_manager, "adopt_file", side_effect=adopt),
            mock.patch.object(job_manager.announcer, "announce"),
        ):
            job_manager.import_worker(1, nullcontext(), stop_event)

        assert _final_status(con) == "CANCELLED"

    def test_no_cancel_reports_completed(self):
        # Contrast: the same single-file scan with no cancel finishes COMPLETED.
        con = _capture_con()
        stop_event = Event()
        with (
            mock.patch.object(job_manager, "get_db_connection", return_value=con),
            mock.patch.object(job_manager, "scan_data_dir_for_untracked", return_value=["/data/only.m4b"]),
            mock.patch.object(job_manager, "adopt_file", return_value={"action": "imported", "key": "IMPORT-x"}),
            mock.patch.object(job_manager.announcer, "announce"),
        ):
            job_manager.import_worker(1, nullcontext(), stop_event)

        assert _final_status(con) == "COMPLETED"


def _download_con(asins):
    """A DB stand-in for download_worker: the job's items, then the per-book and
    per-job status reads it makes on the way out."""
    con = mock.MagicMock()
    con.__enter__.return_value = con

    def execute(sql, params=None):
        cursor = mock.MagicMock()
        if "SELECT asin, status FROM job_items" in sql:
            cursor.fetchall.return_value = [{"asin": asin, "status": "COMPLETED"} for asin in asins]
        elif "SELECT asin FROM job_items" in sql:
            cursor.fetchall.return_value = [{"asin": asin} for asin in asins]
        elif "SELECT status FROM job_items" in sql:
            cursor.fetchall.return_value = [{"status": "COMPLETED"} for _ in asins]
        elif "SELECT status FROM audiobooks" in sql:
            cursor.fetchone.return_value = {"status": "DOWNLOADED"}
        return cursor

    con.execute.side_effect = execute
    return con


class TestDownloadWorkerCleanupConsent:
    """v0.23.0 #2 (D5): the stale-file cleanup answer is a destructive consent, and
    this is the link that maps the job's params onto the processor. It is
    TRI-STATE: True = consented, False = declined (which vetoes the saved
    setting), None = never asked (scheduled/bulk job, so the setting governs)."""

    def _cleanup_arg(self, job_params):
        con = _download_con(["B001"])
        with (
            mock.patch.object(job_manager, "get_db_connection", return_value=con),
            mock.patch.object(job_manager, "load_settings", return_value={}),
            mock.patch.object(job_manager, "BookProcessor") as processor_cls,
            mock.patch.object(job_manager.announcer, "announce"),
        ):
            job_manager.download_worker(1, nullcontext(), Event(), job_params)
        processor_cls.assert_called_once()
        return processor_cls.call_args.kwargs["cleanup_stale_files"]

    def test_consent_is_passed_through_as_true(self):
        assert self._cleanup_arg({"cleanup_stale_files": True}) is True

    def test_decline_is_passed_through_as_false(self):
        # The failure this pins: coercing to bool here loses the decline entirely.
        assert self._cleanup_arg({"cleanup_stale_files": False}) is False

    def test_absent_key_becomes_none(self):
        assert self._cleanup_arg({}) is None

    def test_no_params_at_all_becomes_none(self):
        # Scheduler-started jobs pass job_params=None.
        assert self._cleanup_arg(None) is None

    def test_non_boolean_value_is_treated_as_no_answer(self):
        # Only a real JSON boolean counts as an answer; a string must not sneak
        # through as a truthy "yes".
        assert self._cleanup_arg({"cleanup_stale_files": "false"}) is None

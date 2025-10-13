# audible_downloader/scheduler.py

import time
from threading import Thread

from apscheduler.schedulers.background import BackgroundScheduler  # type: ignore
from apscheduler.triggers.cron import CronTrigger  # type: ignore

from . import app, settings_changed_event
from .health_check import perform_audible_auth_check
from .job_manager import start_new_job
from .logger import log
from .settings import load_settings

# --- Create a global scheduler instance ---
# We initialize it here but will configure its timezone dynamically at startup.
scheduler = BackgroundScheduler()

# --- Job Functions ---
def _run_fast_sync_job():
    log.info("SCHEDULER: Triggering scheduled FAST library sync...")
    with app.app_context():
        # Start a SYNC job, passing the "FAST" mode parameter.
        job_params = {"sync_mode": "FAST"}
        success, result = start_new_job("SYNC", job_params=job_params)
        if not success:
            log.warning(f"SCHEDULER: Could not start automatic FAST SYNC job: {result.get('error')}")

def _run_deep_sync_job():
    log.info("SCHEDULER: Triggering scheduled DEEP library sync...")
    with app.app_context():
        # Start a SYNC job, passing the "DEEP" mode parameter.
        job_params = {"sync_mode": "DEEP"}
        success, result = start_new_job("SYNC", job_params=job_params)
        if not success:
            log.warning(f"SCHEDULER: Could not start automatic DEEP SYNC job: {result.get('error')}")

def _run_process_job():
    log.info("SCHEDULER: Triggering scheduled processing job...")
    with app.app_context():
        success, result = start_new_job("DOWNLOAD", asins=None)
        if success and result.get("job_id") is None:
            log.info("SCHEDULER: Automatic download check ran, but no books were found to process.")
        elif not success:
            log.warning(f"SCHEDULER: Could not start automatic DOWNLOAD job: {result.get('error')}")

def _run_audible_auth_check_job():
    log.info("SCHEDULER: Triggering periodic Audible connection check...")
    perform_audible_auth_check()

# --- Main Scheduler Management Function ---
def _apply_schedules():
    """Contains the core logic to read settings and apply them to the scheduler."""
    log.info("SCHEDULER: Re-evaluating schedules based on current settings...")

    current_settings = load_settings()
    tz = current_settings["tasks"]["timezone"]

    # --- Fast Sync Job ---
    fast_sync_job = scheduler.get_job("scheduled_fast_sync")
    fast_sync_enabled = current_settings["tasks"]["is_auto_fast_sync_enabled"]
    fast_sync_cron = current_settings["tasks"]["fast_sync_schedule"]["cron"]
    new_fast_sync_trigger = CronTrigger.from_crontab(fast_sync_cron, timezone=tz)

    if fast_sync_enabled:
        if fast_sync_job:
            if str(fast_sync_job.trigger) != str(new_fast_sync_trigger):
                scheduler.reschedule_job("scheduled_fast_sync", trigger=new_fast_sync_trigger)
                log.info(f"SCHEDULER: Rescheduled FAST SYNC job with new cron: '{fast_sync_cron}' in timezone {tz}")
        else:
            scheduler.add_job(_run_fast_sync_job, trigger=new_fast_sync_trigger, id="scheduled_fast_sync")
            log.info(f"SCHEDULER: Added FAST SYNC job with new cron: '{fast_sync_cron}' in timezone {tz}")
    elif fast_sync_job:
        scheduler.remove_job("scheduled_fast_sync")
        log.info("SCHEDULER: Removed FAST SYNC job as it is now disabled.")

    # --- Deep Sync Job ---
    deep_sync_job = scheduler.get_job("scheduled_deep_sync")
    deep_sync_enabled = current_settings["tasks"]["is_auto_deep_sync_enabled"]
    deep_sync_cron = current_settings["tasks"]["deep_sync_schedule"]["cron"]
    new_deep_sync_trigger = CronTrigger.from_crontab(deep_sync_cron, timezone=tz)

    if deep_sync_enabled:
        if deep_sync_job:
            if str(deep_sync_job.trigger) != str(new_deep_sync_trigger):
                scheduler.reschedule_job("scheduled_deep_sync", trigger=new_deep_sync_trigger)
                log.info(f"SCHEDULER: Rescheduled DEEP SYNC job with new cron: '{deep_sync_cron}' in timezone {tz}")
        else:
            scheduler.add_job(_run_deep_sync_job, trigger=new_deep_sync_trigger, id="scheduled_deep_sync")
            log.info(f"SCHEDULER: Added DEEP SYNC job with new cron: '{deep_sync_cron}' in timezone {tz}")
    elif deep_sync_job:
        scheduler.remove_job("scheduled_deep_sync")
        log.info("SCHEDULER: Removed DEEP SYNC job as it is now disabled.")

    # --- Process Job ---
    process_job = scheduler.get_job("scheduled_process")
    process_enabled = current_settings["tasks"]["is_auto_process_enabled"]
    process_cron = current_settings["tasks"]["process_schedule"]["cron"]
    new_process_trigger = CronTrigger.from_crontab(process_cron, timezone=tz)

    if process_enabled:
        if process_job:
            if str(process_job.trigger) != str(new_process_trigger):
                scheduler.reschedule_job("scheduled_process", trigger=new_process_trigger)
                log.info(f"SCHEDULER: Rescheduled PROCESS job with new cron: '{process_cron}' in timezone {tz}")
        else:
            scheduler.add_job(_run_process_job, trigger=new_process_trigger, id="scheduled_process")
            log.info(f"SCHEDULER: Added PROCESS job with new cron: '{process_cron}' in timezone {tz}")
    elif process_job:
        scheduler.remove_job("scheduled_process")
        log.info("SCHEDULER: Removed PROCESS job as it is now disabled.")

    # --- Auth Check Job ---
    auth_job = scheduler.get_job("audible_auth_check")
    auth_interval_hours = current_settings["tasks"]["audible_auth_check_interval_hours"]

    if auth_job:
        if str(auth_job.trigger) != f"interval[{auth_interval_hours:02}:00:00]":
            scheduler.reschedule_job("audible_auth_check", trigger="interval", hours=auth_interval_hours)
            log.info(f"SCHEDULER: Rescheduled AUTH check for every {auth_interval_hours} hours.")
    else:
        scheduler.add_job(
            _run_audible_auth_check_job, "interval", hours=auth_interval_hours, id="audible_auth_check", jitter=120
        )
        log.info(f"SCHEDULER: Added AUTH check job for every {auth_interval_hours} hours.")

def scheduler_worker():
    """
    The main worker function that configures and runs the scheduler.
    It now waits for a signal to re-evaluate settings instead of polling.
    """
    if not scheduler.running:
        log.info("SCHEDULER: Starting the APScheduler engine...")
        scheduler.start()
        log.info("SCHEDULER: APScheduler engine started.")

    # Apply the initial schedule immediately on startup.
    _apply_schedules()

    while True:
        try:
            # The wait() call will block this thread indefinitely until event.set() is called.
            log.info("SCHEDULER: Management thread is now waiting for a settings change signal...")
            settings_changed_event.wait()

            # Once awakened, clear the signal so we can wait again.
            settings_changed_event.clear()
            log.info("SCHEDULER: Woken up by a settings change signal.")

            # Re-apply the schedules using the latest settings from the file.
            _apply_schedules()

        except Exception as e:
            log.error(f"SCHEDULER: An unexpected error occurred in the management loop: {e}", exc_info=True)
            # In case of an error, wait a bit before retrying to avoid a tight error loop.
            time.sleep(60)

def start_scheduler_management_thread():
    """
    Starts the background thread that dynamically configures scheduler jobs.
    The actual scheduler engine is started by the worker itself.
    """
    log.info("Starting background thread for dynamic schedule management...")
    # We now use socketio.start_background_task in main.py to run the worker,
    # so this function now only needs to start a standard Thread.
    scheduler_management_thread = Thread(target=scheduler_worker, daemon=True)
    scheduler_management_thread.start()

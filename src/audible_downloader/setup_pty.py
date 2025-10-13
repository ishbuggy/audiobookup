# audible_downloader/setup_pty.py

import os
from queue import Queue
from threading import Lock

import pexpect  # type: ignore # The correct, high-level library for this task
from flask import request  # type: ignore # type import

# Import necessary components from our other modules
from . import DATABASE_DIR, SETUP_FLAG_FILE, socketio
from .logger import log

# --- PTY management for the interactive setup terminal ---

# A thread-safe queue to pass the final URL from the socket handler to the running thread.
url_queue = Queue()

pty_lock = Lock()
child_pty = None
is_pty_running = False


def pty_lifecycle_thread(setup_data):
    """
    A single, long-running thread that manages the entire lifecycle of the PTY setup process
    using the robust `pexpect` library.
    """
    global child_pty, is_pty_running

    try:
        command = "audible quickstart" # pexpect prefers a string
        env = os.environ.copy()
        env["HOME"] = DATABASE_DIR

        with pty_lock:
            if is_pty_running:
                return
            # Use pexpect.spawn, which returns a powerful child object
            child_pty = pexpect.spawn(command, cwd=DATABASE_DIR, env=env, encoding='utf-8')
            is_pty_running = True

        # --- PART 1: Initial Automated Prompts using pexpect.expect ---
        automation_sequence = [
            ("Please enter a name for your primary profile", f"{setup_data['profile_name']}"),
            ("Enter a country code for the profile", f"{setup_data['country_code']}"),
            ("Please enter a name for the auth file", f"{setup_data['auth_file']}"),
            ("Do you want to encrypt the auth file?", "y" if setup_data["encrypt"] else "n"),
        ]
        if setup_data["encrypt"]:
            automation_sequence.extend(
                [
                    ("Please enter a password for the auth file", f"{setup_data['auth_file_password']}"),
                    ("Please enter the password again for verification", f"{setup_data['auth_file_password']}"),
                ]
            )
        automation_sequence.extend(
            [
                ("Do you want to login with external browser?", "y"),
                # Ruff E501: Break long tuple into multiple lines.
                ("Do you want to login with a pre-amazon Audible account?",
                 "y" if setup_data["with_username"] else "n"),
                ("Do you want to continue?", "y"),
            ]
        )

        for prompt_text, answer_text in automation_sequence:
            # The .expect() method is the core of pexpect. It waits for the prompt.
            child_pty.expect(prompt_text, timeout=20)
            # The .sendline() method sends our answer.
            child_pty.sendline(answer_text)


        # --- PART 2: Extract Login URL and Signal Frontend ---
        # Wait for the prompt text first, to ensure the process is at the right stage.
        child_pty.expect("Please copy the following url and insert it into a web browser of your choice:", timeout=20)

        # NOW, we tell pexpect to look for the URL itself using a regular expression.
        # This is the most robust method. It will read lines until it finds one that matches.
        url_pattern = r"https?://[^\s]+"
        child_pty.expect(url_pattern, timeout=10)

        # pexpect stores the matched text in the `match` attribute.
        login_url = child_pty.match.group(0)

        if not login_url:
             # This should now be virtually impossible, but the check remains for safety.
             raise pexpect.exceptions.TIMEOUT("Could not find login URL after the prompt.")

        log.info(f"PTY_SETUP: Found Audible login URL: {login_url}")
        socketio.emit("audible_login_url_ready", {"url": login_url})


        # --- PART 3: Wait for User to Submit URL via the Queue ---
        log.info("PTY_SETUP: Automation thread is now waiting for user to submit the redirected URL.")
        final_url_from_user = url_queue.get(timeout=600)
        child_pty.sendline(final_url_from_user)


        # --- PART 4: Finalize and Validate ---
        # Wait for the process to naturally terminate. The EOF (End Of File) pattern
        # is a special pexpect pattern that matches when the child process closes its output.
        child_pty.expect(pexpect.EOF, timeout=60)
        # .close() now just reaps the exit status, it doesn't send signals.
        child_pty.close()

        log.info("PTY_SETUP: PTY process finished. Validating...")

        validation_env = os.environ.copy()
        validation_env["HOME"] = DATABASE_DIR
        # Use pexpect for the validation as well for consistency and robustness
        validation_child = pexpect.spawn("audible library list", env=validation_env, encoding='utf-8')
        validation_child.expect(pexpect.EOF)
        validation_child.close()

        if validation_child.exitstatus == 0:
            with open(SETUP_FLAG_FILE, "w") as f:
                f.write("Setup completed successfully.")
            final_message = "SUCCESS! Authentication is valid. You will be redirected automatically."
            socketio.emit("pty_output", {"output": final_message})
        else:
            # Capture the error output from the failed validation command
            error_output = validation_child.before or "Unknown validation error."
            final_message = (
                f"FAILED. The new authentication token could not be validated.\n"
                f"Error: {error_output.strip()}"
            )
            socketio.emit("pty_output", {"output": final_message})

    except (pexpect.exceptions.TIMEOUT, pexpect.exceptions.EOF) as e:
        log.error(f"PTY_SETUP: A timeout or unexpected process exit occurred: {e}", exc_info=True)
        # Get any remaining output from the buffer for debugging
        buffer_contents = child_pty.before if child_pty else ""
        error_msg = (
            f"FAILED: The setup script did not respond as expected.\n\n"
            f"Details:\n{buffer_contents}"
        )
        socketio.emit("pty_output", {"output": error_msg})
    except Exception as e:
        log.error(f"PTY_SETUP: An unexpected error occurred in the lifecycle thread: {e}", exc_info=True)
        socketio.emit("pty_output", {"output": f"FAILED: An internal error occurred: {e}"})
    finally:
        with pty_lock:
            if child_pty and child_pty.isalive():
                child_pty.close(force=True)
            child_pty = None
            is_pty_running = False
        log.info("PTY_SETUP: Lifecycle thread finished.")


@socketio.on("connect")
def connect():
    if os.path.exists(SETUP_FLAG_FILE):
        return
    log.info(f"Client connected to setup GUI: {request.sid}")


@socketio.on("start_audible_setup")
def start_audible_setup(data):
    log.info(f"PTY_SETUP: Received start signal with data: {data}")
    socketio.start_background_task(target=pty_lifecycle_thread, setup_data=data)


@socketio.on("pty_input")
def pty_input(data):
    log.info("PTY_SETUP: Received final URL from user via socket.")
    # The .strip() is important to remove the newline the JS adds
    url_queue.put(data["input"].strip())

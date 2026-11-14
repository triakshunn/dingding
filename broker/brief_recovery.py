"""Notify the owner and resume a prepared brief through OpenClaw."""

import contextlib
import os
import subprocess

from ops import watchdog


def notify(text):
    """Sends a short, content-free progress or failure message."""
    with contextlib.suppress(OSError, ValueError, KeyError):
        watchdog.send(text, dry=False)


def schedule(job_id, attempt):
    """Schedules one agent turn to deliver a ready brief.

    The declaration key makes retrying an ambiguous CLI failure safe. Only a
    job id crosses this boundary; the agent still reads filtered mail through
    the broker socket.
    """
    try:
        owner = watchdog.chat_id()
    except (OSError, ValueError, KeyError):
        return False
    if not owner:
        return False
    prompt = (
        f"Resume saved inbox brief {job_id}. Read the inbox skill, then run "
        f"~/dingding/bin/ia brief-job {job_id}. If it is pending, return "
        "NO_REPLY. If delivering elsewhere or already delivered, return "
        "NO_REPLY. If failed, report "
        "the error. If ready, follow the skill's brief sorting and suggestion "
        "steps. Send the finished brief to this Telegram chat with the "
        "message tool. Only after that send succeeds, run "
        f"~/dingding/bin/ia brief-ack {job_id}, then return NO_REPLY. "
        "If the send fails, leave the job unacknowledged for recovery."
    )
    cmd = [
        "docker",
        "exec",
        os.environ.get("BRIEF_RETRY_CONTAINER", "openclaw"),
        "openclaw",
        "cron",
        "add",
        "--at",
        "1s",
        "--name",
        f"brief-recovery-{job_id}-{attempt}",
        "--declaration-key",
        f"brief-recovery-{job_id}-{attempt}",
        "--agent",
        "dingding",
        "--session",
        "isolated",
        "--message",
        prompt,
        "--announce",
        "--channel",
        "telegram",
        "--to",
        owner,
        "--delete-after-run",
        "--timeout-seconds",
        "240",
    ]
    try:
        result = subprocess.run(
            cmd, capture_output=True, text=True, timeout=30, check=False
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    return result.returncode == 0

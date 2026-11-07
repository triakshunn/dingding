"""Watchdog: runs on the host every 10 minutes and alerts on Telegram.

Checks that the OpenClaw container is healthy, the privacy broker answers,
Ollama answers, the disk has more than 10% free, the scheduled briefs ran, and
the nightly Notion backup ran within 30 hours. One alert per problem per 6
hours, plus a one-time "recovered" note. Never sends email and never prints the
bot token.

    python watchdog.py            # check and alert
    python watchdog.py --dry-run  # check and print what it would send
"""

import argparse
import json
import os
import shutil
import socket
import subprocess
import time
import urllib.parse
import urllib.request
from pathlib import Path

HOME = Path.home()
SOCK = Path(
    os.environ.get(
        "BROKER_SOCK", HOME / "dingding/docker/broker-io/broker.sock"
    )
)
OPENCLAW_CONFIG = HOME / "dingding/docker/state/openclaw.json"
BACKUPS = HOME / "notion-backups"
STATE = HOME / ".local/share/watchdog/state.json"
OLLAMA = os.environ.get("OLLAMA_URL", "http://127.0.0.1:11434")
CONTAINER = os.environ.get("WATCHDOG_CONTAINER", "openclaw")
MIN_FREE_DISK = 0.10  # alert below 10 % free
BACKUP_MAX_AGE_HOURS = 30  # nightly backup + 6 h grace
BRIEF_JOBS = ("Morning brief", "Midday update", "Evening update")
REALERT_SECONDS = 6 * 3600
RECHECK_SECONDS = 30


def run(cmd, timeout=60):
    """Runs a command; returns (returncode, stdout, stderr) without raising."""
    p = subprocess.run(
        cmd, capture_output=True, text=True, timeout=timeout, check=False
    )
    return p.returncode, p.stdout, p.stderr


# ---------- checks: each returns None (ok) or a short problem description
# ----------

BREAKER = (
    Path(__file__).resolve().parents[1] / "data/usage/breaker.json"
)  # set by usage_monitor.py


def check_container():
    """Returns a problem if the OpenClaw container isn't healthy.

    Skipped while the usage breaker (or --pause) keeps OpenClaw stopped.
    """
    if BREAKER.exists():
        # deliberately stopped (usage breaker or --pause); resume with
        # usage_monitor.py --resume
        return None
    rc, out, _ = run(
        [
            "docker",
            "inspect",
            "--format",
            (
                "{{.State.Status}} {{if"
                " .State.Health}}{{.State.Health.Status}}{{end}}"
            ),
            CONTAINER,
        ]
    )
    if rc != 0:
        return "OpenClaw container not found"
    status = out.split()
    if status[0] != "running":
        return f"OpenClaw container is {status[0]}"
    if len(status) > 1 and status[1] == "unhealthy":
        return "OpenClaw container is unhealthy"
    return None


def check_broker():
    """Returns a problem if the privacy broker doesn't answer."""
    if not SOCK.exists():
        return "Privacy broker isn't running (no socket)"
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    # The broker answers through its queue; it may be busy with the local
    # model for a while.
    s.settimeout(300)
    try:
        s.connect(str(SOCK))
        s.sendall(b'{"op": "accounts", "args": {}}\n')
        buf = b""
        while not buf.endswith(b"\n"):
            chunk = s.recv(65536)
            if not chunk:
                break
            buf += chunk
        resp = json.loads(buf or b"{}")
    except TimeoutError:
        return "Privacy broker is not answering (5 min timeout)"
    except (OSError, ValueError) as e:
        return f"Privacy broker error: {type(e).__name__}"
    finally:
        s.close()
    return (
        None
        if resp.get("ok")
        else f"Privacy broker returned an error: {str(resp.get('error'))[:120]}"
    )


def check_ollama():
    """Returns a problem if Ollama doesn't answer."""
    try:
        with urllib.request.urlopen(OLLAMA + "/api/tags", timeout=20) as r:
            json.load(r)
        return None
    except Exception as e:  # noqa: BLE001
        return f"Ollama (local model) not answering: {type(e).__name__}"


def check_disk():
    """Returns a problem if the disk is almost full."""
    u = shutil.disk_usage(HOME)
    free = u.free / u.total
    return (
        None if free > MIN_FREE_DISK else f"Disk almost full: {free:.0%} free"
    )


def check_briefs():
    """Returns a problem if a scheduled brief failed or is overdue.

    Skipped while OpenClaw is deliberately stopped.
    """
    if BREAKER.exists():
        return None  # OpenClaw deliberately stopped: no briefs expected
    rc, out, _err = run(
        ["docker", "exec", CONTAINER, "openclaw", "cron", "list", "--json"],
        timeout=90,
    )
    if rc != 0:
        return "Couldn't read the brief schedule from OpenClaw"
    data = json.loads(out)
    jobs = data.get("jobs", data) if isinstance(data, dict) else data
    now_ms = time.time() * 1000
    problems = []
    for name in BRIEF_JOBS:
        job = next((j for j in jobs if j.get("name") == name), None)
        if not job:
            problems.append(f"'{name}' schedule is missing")
            continue
        if not job.get("enabled", True):
            problems.append(f"'{name}' is disabled")
            continue
        st = job.get("state") or {}
        if st.get("lastRunAtMs") and st.get("lastRunStatus") not in (
            None,
            "ok",
        ):
            problems.append(f"'{name}' last run: {st.get('lastRunStatus')}")
        if st.get("consecutiveErrors", 0) >= 1:
            problems.append(
                f"'{name}' failed {st['consecutiveErrors']}× in a row"
            )
        nxt = st.get("nextRunAtMs") or job.get("nextRunAtMs")
        if nxt and now_ms - nxt > 45 * 60 * 1000:
            problems.append(f"'{name}' is overdue (missed its run)")
    return "; ".join(dict.fromkeys(problems)) or None


def check_backup():
    """Returns a problem if the Notion backup is missing or stale."""
    if not BACKUPS.exists():
        return "No Notion backups found"
    days = [d for d in BACKUPS.iterdir() if d.is_dir() and any(d.iterdir())]
    if not days:
        return "No Notion backups found"
    newest = max(d.stat().st_mtime for d in days)
    hours = (time.time() - newest) / 3600
    return (
        None
        if hours < BACKUP_MAX_AGE_HOURS
        else f"Last Notion backup was {hours:.0f} h ago"
    )


CHECKS = {
    "container": check_container,
    "broker": check_broker,
    "ollama": check_ollama,
    "disk": check_disk,
    "briefs": check_briefs,
    "backup": check_backup,
}


# ---------- alerting ----------


def chat_id():
    """Returns the owner's Telegram chat id.

    TELEGRAM_CHAT_ID wins; otherwise the first `telegram:<id>` entry of
    OpenClaw's commands.ownerAllowFrom (the person who owns the bot).
    """
    if os.environ.get("TELEGRAM_CHAT_ID"):
        return os.environ["TELEGRAM_CHAT_ID"]
    owners = json.loads(OPENCLAW_CONFIG.read_text()).get("commands", {})
    for entry in owners.get("ownerAllowFrom", []):
        if str(entry).startswith("telegram:"):
            return str(entry).split(":", 1)[1]
    return ""


def bot_token():
    """Returns the Telegram bot token from OpenClaw's config (never printed)."""
    return json.loads(OPENCLAW_CONFIG.read_text())["channels"]["telegram"][
        "botToken"
    ]


def send(text, dry):
    """Sends a Telegram message to the owner (only prints it when `dry`)."""
    if dry:
        print("[dry-run] would send:", text)
        return
    owner = chat_id()
    if not owner:
        print(
            "No Telegram chat id (TELEGRAM_CHAT_ID / ownerAllowFrom); not"
            " sending:",
            text,
        )
        return
    data = urllib.parse.urlencode({"chat_id": owner, "text": text}).encode()
    url = f"https://api.telegram.org/bot{bot_token()}/sendMessage"
    try:
        urllib.request.urlopen(
            urllib.request.Request(url, data=data), timeout=20
        ).read()
    except Exception as e:  # noqa: BLE001  (never echo the URL: it contains the token)
        print(f"telegram send failed: {type(e).__name__}")


def main():
    """Runs the checks, alerts on new problems and recoveries, saves state."""
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument(
        "--only", help="comma-separated subset of checks: " + ",".join(CHECKS)
    )
    a = ap.parse_args()
    STATE.parent.mkdir(parents=True, exist_ok=True)
    os.chmod(STATE.parent, 0o700)
    state = json.loads(STATE.read_text()) if STATE.exists() else {}
    now = time.time()
    names = a.only.split(",") if a.only else list(CHECKS)

    def check(name):
        try:
            return CHECKS[name]()
        except Exception as e:  # noqa: BLE001
            return f"check '{name}' crashed: {type(e).__name__}"

    for name in names:
        problem = check(name)
        # Ride out planned restarts: confirm once more before alerting.
        if problem and not a.dry_run:
            time.sleep(RECHECK_SECONDS)
            problem = check(name)
        prev = state.get(name, {})
        print(f"{name:9s} {'OK' if not problem else 'PROBLEM: ' + problem}")
        if problem:
            if not prev.get("since"):
                prev["since"] = now
            if now - prev.get("alerted", 0) >= REALERT_SECONDS:
                send(f"⚠️ Home agent: {problem}", a.dry_run)
                prev["alerted"] = now
            prev["problem"] = problem
        elif prev.get("problem"):
            send(f"✅ Home agent recovered: {prev['problem']}", a.dry_run)
            prev = {}
        state[name] = prev
    if not a.dry_run:
        STATE.write_text(json.dumps(state))


if __name__ == "__main__":
    main()

"""Numbered suggestions waiting for your decision (events to add, emails to reply to).

Shared by the broker and its helpers. File-locked so concurrent writers are safe.
"""

import datetime as dt
import fcntl
import json
import os
from contextlib import contextmanager

from auth import DATA

FILE = DATA / "pending.json"
KEEP_DAYS = 14


@contextmanager
def pending():
    with open(DATA / ".pending.lock", "w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        data = (
            json.loads(FILE.read_text())
            if FILE.exists()
            else {"next": 1, "items": {}}
        )
        yield data
        cutoff = (
            dt.datetime.now().astimezone() - dt.timedelta(days=KEEP_DAYS)
        ).isoformat()
        data["items"] = {
            k: v
            for k, v in data["items"].items()
            if v.get("created", "") >= cutoff
        }
        if not data["items"]:
            data["next"] = (
                1  # numbers start again from 1 once everything is cleared
            )
        tmp = FILE.with_suffix(".tmp")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as f:
            json.dump(data, f)
        os.replace(tmp, FILE)


def add(data, item) -> int:
    n = data["next"]
    data["next"] += 1
    item["id"] = n
    item["created"] = dt.datetime.now().astimezone().isoformat()
    data["items"][str(n)] = item
    return n

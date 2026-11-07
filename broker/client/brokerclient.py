"""Tiny stdlib-only client for the privacy broker socket (used inside the container; holds no keys)."""

import json
import os
import socket
import sys
from pathlib import Path

SOCK = os.environ.get("BROKER_SOCK", str(Path.home() / "broker-io/broker.sock"))


def call(op, **args):
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        s.connect(SOCK)
    except OSError as e:
        sys.exit(
            f"Can't reach the privacy broker at {SOCK} ({e}). Is it running on the laptop?"
        )
    s.sendall((json.dumps({"op": op, "args": args}) + "\n").encode())
    buf = b""
    while not buf.endswith(b"\n"):
        chunk = s.recv(65536)
        if not chunk:
            break
        buf += chunk
    s.close()
    resp = json.loads(buf or b'{"ok": false, "error": "no response"}')
    if not resp.get("ok"):
        sys.exit(resp.get("error", "failed"))
    return resp["result"]


def out(obj):
    print(
        json.dumps(obj, ensure_ascii=False, separators=(",", ":"))
    )  # compact: the agent pays per character

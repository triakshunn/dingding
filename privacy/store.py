"""Host-only store mapping placeholders to real values.

Never mount this into the container.
"""

import json
import os
import re
import uuid
from pathlib import Path

ID_RE = re.compile(r"^[0-9a-f]{32}$")


def state_dir() -> Path:
    """Returns the mappings directory (PRIVACY_STATE), created mode 0700."""
    d = Path(
        os.environ.get(
            "PRIVACY_STATE",
            Path.home() / ".local/share/privacy-broker/mappings",
        )
    )
    d.mkdir(parents=True, exist_ok=True)
    os.chmod(d, 0o700)
    return d


def new_id() -> str:
    """Returns a new random mapping id."""
    return uuid.uuid4().hex


def _path(mapping_id: str) -> Path:
    if not ID_RE.match(mapping_id or ""):
        raise ValueError(f"bad mapping_id: {mapping_id!r}")
    return state_dir() / f"{mapping_id}.json"


def load(mapping_id: str) -> dict:
    """Returns the mapping with this id, or an empty one if it doesn't exist."""
    p = _path(mapping_id)
    if p.exists():
        return json.loads(p.read_text())
    return {"values": {}, "reverse": {}, "counters": {}}


def save(mapping_id: str, m: dict) -> None:
    """Atomically writes the mapping with this id (mode 0600)."""
    p = _path(mapping_id)
    tmp = p.with_suffix(".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        json.dump(m, f)
    os.replace(tmp, p)


def placeholder_for(m: dict, kind: str, value: str) -> str:
    """Same value -> same placeholder within a mapping."""
    if value in m["reverse"]:
        return m["reverse"][value]
    n = m["counters"].get(kind, 0) + 1
    m["counters"][kind] = n
    ph = f"[{kind}_{n}]"
    m["values"][ph] = value
    m["reverse"][value] = ph
    return ph

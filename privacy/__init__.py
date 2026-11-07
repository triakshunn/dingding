"""Privacy filter: deterministic rules (layer 1) plus a local model (layer 2).

Runs on the host only. Raw email and Notion text goes in; redacted text,
summaries and labels come out. The placeholder-to-value mappings are stored on
the host (PRIVACY_STATE) and must never be mounted into the container.
"""

from .core import check_edit_preserves
from .core import filter_email
from .core import filter_page
from .core import locked_placeholders
from .core import redact
from .core import rehydrate

__all__ = [
    "check_edit_preserves",
    "filter_email",
    "filter_page",
    "locked_placeholders",
    "redact",
    "rehydrate",
]

# Model: PRIVACY_MODEL defaults to the Qwen3-30B-A3B (UD-Q2_K_XL) build;
# requests pass keep_alive (default 30m) so it
# stays loaded between jobs. Only one model should be loaded at a time on this
# 22 GB laptop: after cutover the old
# `qwen3:8b` can be removed with `ollama rm qwen3:8b` (not done automatically).

"""Shared mail settings and helpers for the broker.

Holds the user's config (timezone, Gmail query) and turns Gmail message
payloads into plain text. Imported by the broker backend and actions.
"""

import base64
import html
import json
import re
from html.parser import HTMLParser
from zoneinfo import ZoneInfo

from auth import DATA

CONFIG = json.loads((DATA / "config.json").read_text())
TZ = ZoneInfo(CONFIG["timezone"])
# Skip promotions/social/forums: they almost never hold real events, and every
# email costs a local-model call.
GMAIL_QUERY = CONFIG.get(
    "gmail_query",
    "newer_than:2d -category:promotions -category:social -category:forums"
    " -in:chats",
)
MAX_BODY_CHARS = 3000

# Marketing mail pads previews with invisible characters; they only waste
# model tokens.
_INVISIBLE = re.compile(
    "[\u00ad\u034f\u061c\u115f\u1160\u17b4\u17b5\u180e\u200b-\u200f"
    "\u202a-\u202e\u2060-\u206f\u3164\ufeff\uffa0]"
)
_SPACES = re.compile("[ \t\r\u00a0\u2007\u202f]+")
_BLANK_LINES = re.compile(r"\n\s*\n+")
_SKIP_TAGS = ("style", "script")
_BREAK_TAGS = ("br", "p", "div", "tr", "li")


class _TextExtractor(HTMLParser):
    """HTML parser that keeps visible text and turns blocks into newlines."""

    def __init__(self):
        super().__init__()
        self.parts = []
        self._skip = 0

    def handle_starttag(self, tag, attrs):
        """Starts skipping style/script; adds a newline for block tags."""
        del attrs  # Unused.
        if tag in _SKIP_TAGS:
            self._skip += 1
        elif tag in _BREAK_TAGS:
            self.parts.append("\n")

    def handle_endtag(self, tag):
        """Stops skipping after a style/script block."""
        if tag in _SKIP_TAGS:
            self._skip -= 1

    def handle_data(self, data):
        """Keeps text that is not inside style/script."""
        if not self._skip:
            self.parts.append(data)


def html_to_text(markup: str) -> str:
    """Returns the visible text of an HTML document."""
    parser = _TextExtractor()
    parser.feed(markup)
    return html.unescape("".join(parser.parts))


def body_text(payload: dict) -> str:
    """Extracts a message body as clean plain text.

    Args:
        payload: The `payload` of a Gmail API message (format=full).

    Returns:
        The text/plain part (or the text of the HTML part when there is none),
        without invisible padding characters, at most MAX_BODY_CHARS long.
    """
    found = {}

    def walk(part):
        mime = part.get("mimeType", "")
        data = part.get("body", {}).get("data")
        if data and mime in ("text/plain", "text/html") and mime not in found:
            found[mime] = base64.urlsafe_b64decode(data).decode(
                "utf-8", "replace"
            )
        for sub in part.get("parts") or []:
            walk(sub)

    walk(payload)
    text = found.get("text/plain") or html_to_text(found.get("text/html", ""))
    text = _INVISIBLE.sub("", text)
    text = _SPACES.sub(" ", text)
    text = _BLANK_LINES.sub("\n\n", text)
    return text.strip()[:MAX_BODY_CHARS]

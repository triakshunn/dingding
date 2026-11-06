"""Shared helpers: settings (data/config.json), timezone, Gmail query, and email body extraction."""
import base64
import html
import json
import re
from html.parser import HTMLParser
from zoneinfo import ZoneInfo

from auth import DATA

CONFIG = json.loads((DATA / "config.json").read_text())
TZ = ZoneInfo(CONFIG["timezone"])
# Which mail counts as "new" for briefs. Promotions/social/forums tabs are skipped (use `ia cleanup` for those).
GMAIL_QUERY = CONFIG.get("gmail_query", "newer_than:2d -category:promotions -category:social -category:forums -in:chats")
MAX_BODY_CHARS = 3000


class _Text(HTMLParser):
    def __init__(self):
        super().__init__()
        self.parts, self._skip = [], 0

    def handle_starttag(self, tag, attrs):
        if tag in ("style", "script"):
            self._skip += 1
        elif tag in ("br", "p", "div", "tr", "li"):
            self.parts.append("\n")

    def handle_endtag(self, tag):
        if tag in ("style", "script"):
            self._skip -= 1

    def handle_data(self, data):
        if not self._skip:
            self.parts.append(data)


def html_to_text(s: str) -> str:
    p = _Text()
    p.feed(s)
    return html.unescape("".join(p.parts))


def body_text(payload: dict) -> str:
    """Prefer text/plain, fall back to stripped HTML."""
    found = {}

    def walk(part):
        mime = part.get("mimeType", "")
        data = part.get("body", {}).get("data")
        if data and mime in ("text/plain", "text/html") and mime not in found:
            found[mime] = base64.urlsafe_b64decode(data).decode("utf-8", "replace")
        for sub in part.get("parts", []) or []:
            walk(sub)

    walk(payload)
    text = found.get("text/plain") or html_to_text(found.get("text/html", ""))
    # Marketing mail pads previews with invisible characters; drop them so they don't waste model tokens.
    text = re.sub(r"[­͏؜ᅟᅠ឴឵᠎​-‏‪-‮⁠-⁯ㅤ﻿ﾠ]", "", text)
    text = re.sub(r"[ \t\r   ]+", " ", text)
    text = re.sub(r"\n\s*\n+", "\n\n", text)
    return text.strip()[:MAX_BODY_CHARS]

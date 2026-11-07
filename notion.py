"""Notion helpers used by the privacy broker and the nightly backup.

Talks to the Notion API (every call goes through `api`, which the broker
routes via its backend so tests can use a fake), converts pages to text and
Markdown to blocks, and keeps a local log of changes for `nt log` / `nt undo`.
The agent-facing `nt` command lives in broker/client/nt.
"""

import datetime as dt
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
import uuid
from http import HTTPStatus
from pathlib import Path

from auth import DATA

VERSION = "2025-09-03"
MOVE_VERSION = "2026-03-11"  # page move endpoint needs this newer API version
TOKEN_FILE = Path(os.environ.get("NOTION_TOKEN_FILE", DATA / "notion_token"))
LOG = DATA / "notion_changes.jsonl"


# ---------- API ----------


def api(method, path, body=None, version=VERSION):
    """Calls the Notion API and returns the JSON answer.

    Retries rate limits and server errors (up to 5 attempts, honouring
    Retry-After).

    Args:
        method: HTTP method.
        path: API path, e.g. "/pages/<id>".
        body: JSON body, if any.
        version: Notion-Version header.

    Raises:
        SystemExit: With a readable message on other API errors; the broker
            reports it to the agent.
    """
    tok = TOKEN_FILE.read_text().strip()
    for attempt in range(5):
        req = urllib.request.Request(
            "https://api.notion.com/v1" + path,
            method=method,
            data=json.dumps(body).encode() if body is not None else None,
            headers={
                "Authorization": f"Bearer {tok}",
                "Notion-Version": version,
                "Content-Type": "application/json",
            },
        )
        try:
            with urllib.request.urlopen(req, timeout=60) as r:
                return json.load(r)
        except urllib.error.HTTPError as e:
            if (
                e.code == HTTPStatus.TOO_MANY_REQUESTS
                or e.code >= HTTPStatus.INTERNAL_SERVER_ERROR
            ):
                time.sleep(float(e.headers.get("Retry-After", 1 + attempt)))
                continue
            sys.exit(f"Notion error {e.code}: {e.read().decode()[:400]}")
    sys.exit("Notion is rate-limiting; try again shortly.")


def paged(method, path, body=None):
    """Returns all results of a paginated Notion endpoint (GET or POST)."""
    out, cur = [], None
    while True:
        if method == "GET":
            r = api(
                "GET",
                path
                + (
                    ("&" if "?" in path else "?") + f"start_cursor={cur}"
                    if cur
                    else ""
                ),
            )
        else:
            r = api(
                "POST",
                path,
                {**(body or {}), **({"start_cursor": cur} if cur else {})},
            )
        out += r["results"]
        if not r.get("has_more"):
            return out
        cur = r["next_cursor"]


# ---------- text helpers ----------


def plain(rich):
    """Returns the plain text of a Notion rich-text array."""
    return "".join(t.get("plain_text", "") for t in rich or [])


def title_of(obj):
    """Returns the title of a page or database, or "(untitled)"."""
    if obj["object"] in ("database", "data_source"):
        return plain(obj.get("title")) or "(untitled)"
    for p in obj.get("properties", {}).values():
        if p.get("type") == "title":
            return plain(p["title"]) or "(untitled)"
    return "(untitled)"


def _computed(v):
    return (v or {}).get((v or {}).get("type"))


def _date_text(v):
    if not v:
        return v
    return v["start"] + (f" → {v['end']}" if v.get("end") else "")


# Notion property type -> how to show its value as plain Python data.
_PROP_READERS = {
    "title": plain,
    "rich_text": plain,
    "select": lambda v: (v or {}).get("name"),
    "status": lambda v: (v or {}).get("name"),
    "multi_select": lambda v: [o["name"] for o in v],
    "date": _date_text,
    "people": lambda v: [x.get("name") for x in v],
    "relation": lambda v: [x["id"] for x in v],
    "formula": _computed,
    "rollup": _computed,
}


def prop_value(p):
    """Returns a Notion property's value as plain text, a list, or raw data."""
    t = p.get("type")
    reader = _PROP_READERS.get(t)
    return reader(p.get(t)) if reader else p.get(t)


def rt(text):
    """Converts text to Notion rich text, with **bold** and `code` spans.

    Long text is split into 2,000-character pieces (Notion's limit).
    """
    out = []
    for chunk in re.split(r"(\*\*[^*]+\*\*|`[^`]+`)", text):
        if not chunk:
            continue
        part, ann = chunk, {}
        if chunk.startswith("**") and chunk.endswith("**"):
            part, ann = chunk[2:-2], {"bold": True}
        elif chunk.startswith("`") and chunk.endswith("`"):
            part, ann = chunk[1:-1], {"code": True}
        for i in range(0, len(part), 2000):
            out.append(
                {
                    "type": "text",
                    "text": {"content": part[i : i + 2000]},
                    **({"annotations": ann} if ann else {}),
                }
            )
    return out


def md_to_blocks(md):
    """Converts simple Markdown (headings, lists, quotes, code) to blocks."""
    blocks, code = [], None
    for line in md.splitlines():
        if line.startswith("```"):
            if code is None:
                code = {"lang": line[3:].strip() or "plain text", "lines": []}
            else:
                blocks.append(
                    {
                        "type": "code",
                        "code": {
                            "rich_text": rt("\n".join(code["lines"])),
                            "language": code["lang"],
                        },
                    }
                )
                code = None
            continue
        if code is not None:
            code["lines"].append(line)
            continue
        s = line.rstrip()
        if not s.strip():
            continue
        link = re.match(r"^\s*\[\[(page|db):([0-9a-fA-F-]{32,36})\]\]\s*$", s)
        m = re.match(r"^(#{1,3})\s+(.*)", s)
        if link:
            kind = "page_id" if link.group(1) == "page" else "database_id"
            blocks.append(
                {
                    "type": "link_to_page",
                    "link_to_page": {"type": kind, kind: link.group(2)},
                }
            )
        elif m:
            k = f"heading_{len(m.group(1))}"
            blocks.append({"type": k, k: {"rich_text": rt(m.group(2))}})
        elif re.match(r"^\s*[-*]\s+\[( |x)\]\s+", s):
            done = "[x]" in s[:8]
            blocks.append(
                {
                    "type": "to_do",
                    "to_do": {
                        "rich_text": rt(
                            re.sub(r"^\s*[-*]\s+\[( |x)\]\s+", "", s)
                        ),
                        "checked": done,
                    },
                }
            )
        elif re.match(r"^\s*[-*]\s+", s):
            blocks.append(
                {
                    "type": "bulleted_list_item",
                    "bulleted_list_item": {
                        "rich_text": rt(re.sub(r"^\s*[-*]\s+", "", s))
                    },
                }
            )
        elif re.match(r"^\s*\d+[.)]\s+", s):
            blocks.append(
                {
                    "type": "numbered_list_item",
                    "numbered_list_item": {
                        "rich_text": rt(re.sub(r"^\s*\d+[.)]\s+", "", s))
                    },
                }
            )
        elif s.startswith("> "):
            blocks.append({"type": "quote", "quote": {"rich_text": rt(s[2:])}})
        elif s.strip() == "---":
            blocks.append({"type": "divider", "divider": {}})
        else:
            blocks.append(
                {"type": "paragraph", "paragraph": {"rich_text": rt(s)}}
            )
    for b in blocks:
        b["object"] = "block"
    return blocks


def blocks_to_text(block_id, depth, indent=0):
    """Returns a block's children as text lines, recursing `depth` levels."""
    lines = []
    for b in paged("GET", f"/blocks/{block_id}/children?page_size=100"):
        t = b["type"]
        d = b.get(t, {})
        pad = "  " * indent
        txt = plain(d.get("rich_text"))
        if t.startswith("heading_"):
            lines.append(f"{pad}{'#' * int(t[-1])} {txt}")
        elif t == "bulleted_list_item":
            lines.append(f"{pad}- {txt}")
        elif t == "numbered_list_item":
            lines.append(f"{pad}1. {txt}")
        elif t == "to_do":
            lines.append(f"{pad}- [{'x' if d.get('checked') else ' '}] {txt}")
        elif t == "code":
            lines.append(f"{pad}```\n{txt}\n{pad}```")
        elif t == "child_page":
            lines.append(f"{pad}[page] {d.get('title')} ({b['id']})")
        elif t == "child_database":
            lines.append(f"{pad}[database] {d.get('title')} ({b['id']})")
        elif t == "divider":
            lines.append(f"{pad}---")
        elif t == "link_to_page":
            lines.append(
                f"{pad}[link] {d.get('page_id') or d.get('database_id')}"
            )
        elif t in (
            "image",
            "file",
            "pdf",
            "video",
            "bookmark",
            "embed",
            "link_preview",
        ):
            url = d.get("url") or (
                d.get("external") or d.get("file") or {}
            ).get("url", "")
            lines.append(f"{pad}[{t}] {url}")
        elif txt:
            lines.append(f"{pad}{txt}")
        if (
            b.get("has_children")
            and depth > 1
            and t not in ("child_page", "child_database")
        ):
            lines += blocks_to_text(b["id"], depth - 1, indent + 1)
    return lines


# ---------- change log ----------


def log_change(kind, target, before, after_ref=None):
    """Appends a change to the local Notion change log; returns its id.

    Args:
        kind: What happened (create, append, set, move, archive, undo).
        target: The page id.
        before: Snapshot needed to undo the change.
        after_ref: Extra data for undo (e.g. ids of appended blocks).
    """
    cid = uuid.uuid4().hex[:8]
    entry = {
        "id": cid,
        "at": dt.datetime.now().astimezone().isoformat(timespec="seconds"),
        "kind": kind,
        "target": target,
        "before": before,
        "after": after_ref,
    }
    with open(LOG, "a") as f:
        f.write(json.dumps(entry) + "\n")
    return cid


def read_log():
    """Returns all logged Notion changes, oldest first."""
    return (
        [json.loads(line) for line in LOG.read_text().splitlines()]
        if LOG.exists()
        else []
    )


def editable_props(page):
    """Snapshot of properties in a form that can be written back."""
    keep = {}
    for name, p in page.get("properties", {}).items():
        t = p["type"]
        if t in ("title", "rich_text"):
            keep[name] = {
                t: [{"type": "text", "text": {"content": plain(p[t])}}]
            }
        elif t in ("select", "status"):
            keep[name] = {t: {"name": p[t]["name"]} if p[t] else None}
        elif t == "multi_select":
            keep[name] = {t: [{"name": o["name"]} for o in p[t]]}
        elif t in (
            "date",
            "checkbox",
            "number",
            "url",
            "email",
            "phone_number",
        ):
            keep[name] = {t: p[t]}
        elif t == "relation":
            keep[name] = {t: [{"id": x["id"]} for x in p[t]]}
    return keep


# ---------- commands ----------

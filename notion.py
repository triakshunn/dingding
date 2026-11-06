"""`nt` — Notion commands for the OpenClaw agent (and the nightly backup).

  nt search "<text>" [--max 10]           find pages/databases by title
  nt read <id> [--depth 2]                page properties + content as text
  nt dbs                                  list databases with their columns
  nt query <db_id> [--max 20]             rows of a database
  nt create --parent <page_or_db_id> --title "…" [--props JSON] [--md "markdown" | --md-file F]
                                          NEW page (safe: no confirmation needed)
  nt append <page_id> --md "…" --confirmed      add content to an EXISTING page
  nt set <page_id> --props JSON --confirmed      change properties of an EXISTING page
  nt archive <page_id> --confirmed               move a page to Notion's trash (restorable)
  nt move <page_id> --to <parent_page_id> --confirmed   move a regular page under another page (undoable)
  Markdown links to pages: a line "[[page:<id>]]" or "[[db:<id>]]" becomes a Notion link block.
  nt log [--max 10]                       recent changes made by the agent
  nt undo [<change_id>]                   undo the last (or given) change

Editing existing pages requires --confirmed: only pass it after the user said yes to a preview.
Every change is logged with a before-snapshot so it can be undone. Nothing is ever permanently deleted.
"""
import argparse
import datetime as dt
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path

from auth import DATA

VERSION = "2025-09-03"
MOVE_VERSION = "2026-03-11"  # page move endpoint needs this newer API version
TOKEN_FILE = Path(os.environ.get("NOTION_TOKEN_FILE", DATA / "notion_token"))
LOG = DATA / "notion_changes.jsonl"


# ---------- API ----------

def api(method, path, body=None, version=VERSION):
    tok = TOKEN_FILE.read_text().strip()
    for attempt in range(5):
        req = urllib.request.Request(
            "https://api.notion.com/v1" + path, method=method,
            data=json.dumps(body).encode() if body is not None else None,
            headers={"Authorization": f"Bearer {tok}", "Notion-Version": version, "Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=60) as r:
                return json.load(r)
        except urllib.error.HTTPError as e:
            if e.code == 429 or e.code >= 500:
                time.sleep(float(e.headers.get("Retry-After", 1 + attempt)))
                continue
            sys.exit(f"Notion error {e.code}: {e.read().decode()[:400]}")
    sys.exit("Notion is rate-limiting; try again shortly.")


def paged(method, path, body=None):
    out, cur = [], None
    while True:
        if method == "GET":
            r = api("GET", path + (("&" if "?" in path else "?") + f"start_cursor={cur}" if cur else ""))
        else:
            r = api("POST", path, {**(body or {}), **({"start_cursor": cur} if cur else {})})
        out += r["results"]
        if not r.get("has_more"):
            return out
        cur = r["next_cursor"]


# ---------- text helpers ----------

def plain(rich):
    return "".join(t.get("plain_text", "") for t in rich or [])


def title_of(obj):
    if obj["object"] in ("database", "data_source"):
        return plain(obj.get("title")) or "(untitled)"
    for p in obj.get("properties", {}).values():
        if p.get("type") == "title":
            return plain(p["title"]) or "(untitled)"
    return "(untitled)"


def prop_value(p):
    t = p.get("type")
    v = p.get(t)
    if t in ("title", "rich_text"):
        return plain(v)
    if t in ("select", "status"):
        return (v or {}).get("name")
    if t == "multi_select":
        return [o["name"] for o in v]
    if t == "date":
        return v and (v["start"] + (f" → {v['end']}" if v.get("end") else ""))
    if t == "people":
        return [x.get("name") for x in v]
    if t == "relation":
        return [x["id"] for x in v]
    if t in ("formula", "rollup"):
        return (v or {}).get((v or {}).get("type"))
    return v


def rt(text):
    """Plain text -> Notion rich_text (2000-char chunks, **bold** and `code` supported)."""
    out = []
    for part in re.split(r"(\*\*[^*]+\*\*|`[^`]+`)", text):
        if not part:
            continue
        ann = {}
        if part.startswith("**") and part.endswith("**"):
            part, ann = part[2:-2], {"bold": True}
        elif part.startswith("`") and part.endswith("`"):
            part, ann = part[1:-1], {"code": True}
        for i in range(0, len(part), 2000):
            out.append({"type": "text", "text": {"content": part[i:i + 2000]}, **({"annotations": ann} if ann else {})})
    return out


def md_to_blocks(md):
    blocks, code = [], None
    for line in md.splitlines():
        if line.startswith("```"):
            if code is None:
                code = {"lang": line[3:].strip() or "plain text", "lines": []}
            else:
                blocks.append({"type": "code", "code": {"rich_text": rt("\n".join(code["lines"])), "language": code["lang"]}})
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
            blocks.append({"type": "link_to_page", "link_to_page": {"type": kind, kind: link.group(2)}})
        elif m:
            k = f"heading_{len(m.group(1))}"
            blocks.append({"type": k, k: {"rich_text": rt(m.group(2))}})
        elif re.match(r"^\s*[-*]\s+\[( |x)\]\s+", s):
            done = "[x]" in s[:8]
            blocks.append({"type": "to_do", "to_do": {"rich_text": rt(re.sub(r"^\s*[-*]\s+\[( |x)\]\s+", "", s)), "checked": done}})
        elif re.match(r"^\s*[-*]\s+", s):
            blocks.append({"type": "bulleted_list_item", "bulleted_list_item": {"rich_text": rt(re.sub(r"^\s*[-*]\s+", "", s))}})
        elif re.match(r"^\s*\d+[.)]\s+", s):
            blocks.append({"type": "numbered_list_item", "numbered_list_item": {"rich_text": rt(re.sub(r"^\s*\d+[.)]\s+", "", s))}})
        elif s.startswith("> "):
            blocks.append({"type": "quote", "quote": {"rich_text": rt(s[2:])}})
        elif s.strip() == "---":
            blocks.append({"type": "divider", "divider": {}})
        else:
            blocks.append({"type": "paragraph", "paragraph": {"rich_text": rt(s)}})
    for b in blocks:
        b["object"] = "block"
    return blocks


def blocks_to_text(block_id, depth, indent=0):
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
            lines.append(f"{pad}[link] {d.get('page_id') or d.get('database_id')}")
        elif t in ("image", "file", "pdf", "video", "bookmark", "embed", "link_preview"):
            url = d.get("url") or (d.get("external") or d.get("file") or {}).get("url", "")
            lines.append(f"{pad}[{t}] {url}")
        elif txt:
            lines.append(f"{pad}{txt}")
        if b.get("has_children") and depth > 1 and t not in ("child_page", "child_database"):
            lines += blocks_to_text(b["id"], depth - 1, indent + 1)
    return lines


# ---------- change log ----------

def log_change(kind, target, before, after_ref=None):
    cid = uuid.uuid4().hex[:8]
    entry = {"id": cid, "at": dt.datetime.now().astimezone().isoformat(timespec="seconds"),
             "kind": kind, "target": target, "before": before, "after": after_ref}
    with open(LOG, "a") as f:
        f.write(json.dumps(entry) + "\n")
    return cid


def read_log():
    return [json.loads(l) for l in LOG.read_text().splitlines()] if LOG.exists() else []


def editable_props(page):
    """Snapshot of properties in a form that can be written back."""
    keep = {}
    for name, p in page.get("properties", {}).items():
        t = p["type"]
        if t in ("title", "rich_text"):
            keep[name] = {t: [{"type": "text", "text": {"content": plain(p[t])}}]}
        elif t in ("select", "status"):
            keep[name] = {t: {"name": p[t]["name"]} if p[t] else None}
        elif t == "multi_select":
            keep[name] = {t: [{"name": o["name"]} for o in p[t]]}
        elif t in ("date", "checkbox", "number", "url", "email", "phone_number"):
            keep[name] = {t: p[t]}
        elif t == "relation":
            keep[name] = {t: [{"id": x["id"]} for x in p[t]]}
    return keep


def need_confirm(a):
    if not a.confirmed:
        sys.exit("Refused: this changes an EXISTING page. Show the user a preview of the change, and only re-run "
                 "with --confirmed after they explicitly say yes.")


# ---------- commands ----------

def out(x):
    print(json.dumps(x, ensure_ascii=False, indent=1))


def main():
    ap = argparse.ArgumentParser(prog="nt")
    sp = ap.add_subparsers(dest="cmd", required=True)
    p = sp.add_parser("search"); p.add_argument("text"); p.add_argument("--max", type=int, default=10)
    p = sp.add_parser("read"); p.add_argument("id"); p.add_argument("--depth", type=int, default=2)
    sp.add_parser("dbs")
    p = sp.add_parser("query"); p.add_argument("db_id"); p.add_argument("--max", type=int, default=20)
    p = sp.add_parser("create"); p.add_argument("--parent", required=True); p.add_argument("--title", required=True)
    p.add_argument("--props", default="{}"); p.add_argument("--md", default=""); p.add_argument("--md-file")
    p = sp.add_parser("append"); p.add_argument("page_id"); p.add_argument("--md", default=""); p.add_argument("--md-file")
    p.add_argument("--confirmed", action="store_true")
    p = sp.add_parser("set"); p.add_argument("page_id"); p.add_argument("--props", required=True); p.add_argument("--confirmed", action="store_true")
    p = sp.add_parser("archive"); p.add_argument("page_id"); p.add_argument("--confirmed", action="store_true")
    p = sp.add_parser("move"); p.add_argument("page_id"); p.add_argument("--to", required=True)
    p.add_argument("--confirmed", action="store_true")
    p = sp.add_parser("log"); p.add_argument("--max", type=int, default=10)
    p = sp.add_parser("undo"); p.add_argument("change_id", nargs="?")
    a = ap.parse_args()
    md = lambda: Path(a.md_file).read_text() if getattr(a, "md_file", None) else a.md  # noqa: E731

    if a.cmd == "search":
        r = api("POST", "/search", {"query": a.text, "page_size": a.max})["results"]
        out([{"type": x["object"], "title": title_of(x), "id": x["id"], "url": x.get("url"),
              "edited": x.get("last_edited_time")} for x in r])

    elif a.cmd == "read":
        page = api("GET", f"/pages/{a.id}")
        props = {k: prop_value(v) for k, v in page.get("properties", {}).items()}
        text = "\n".join(blocks_to_text(a.id, a.depth))
        out({"title": title_of(page), "url": page.get("url"), "properties": props, "content": text[:15000]})

    elif a.cmd == "dbs":
        res = []
        for x in api("POST", "/search", {"filter": {"property": "object", "value": "data_source"}, "page_size": 100})["results"]:
            res.append({"title": title_of(x), "id": x["id"],
                        "columns": {k: v["type"] for k, v in x.get("properties", {}).items()}})
        out(res)

    elif a.cmd == "query":
        rows = api("POST", f"/data_sources/{a.db_id}/query", {"page_size": a.max})["results"]
        out([{"id": r["id"], "title": title_of(r), "props": {k: prop_value(v) for k, v in r["properties"].items()}} for r in rows])

    elif a.cmd == "create":
        props = json.loads(a.props)
        blocks = md_to_blocks(md())
        # Database row or sub-page?
        try:
            ds = api("GET", f"/data_sources/{a.parent}")
            title_col = next(k for k, v in ds["properties"].items() if v["type"] == "title")
            parent = {"type": "data_source_id", "data_source_id": a.parent}
            props[title_col] = {"title": rt(a.title)}
        except SystemExit:
            parent = {"type": "page_id", "page_id": a.parent}
            props = {"title": {"title": rt(a.title)}}
        page = api("POST", "/pages", {"parent": parent, "properties": props, "children": blocks[:100]})
        for i in range(100, len(blocks), 100):
            api("PATCH", f"/blocks/{page['id']}/children", {"children": blocks[i:i + 100]})
        cid = log_change("create", page["id"], None, {"title": a.title})
        out({"created": page["id"], "url": page.get("url"), "change_id": cid})

    elif a.cmd == "append":
        need_confirm(a)
        blocks = md_to_blocks(md())
        new_ids = []
        for i in range(0, len(blocks), 100):
            r = api("PATCH", f"/blocks/{a.page_id}/children", {"children": blocks[i:i + 100]})
            new_ids += [b["id"] for b in r["results"]]
        cid = log_change("append", a.page_id, None, {"block_ids": new_ids})
        out({"appended_blocks": len(new_ids), "change_id": cid})

    elif a.cmd == "set":
        need_confirm(a)
        before = editable_props(api("GET", f"/pages/{a.page_id}"))
        new = json.loads(a.props)
        api("PATCH", f"/pages/{a.page_id}", {"properties": new})
        cid = log_change("set", a.page_id, {k: before.get(k) for k in new})
        out({"updated": a.page_id, "change_id": cid})

    elif a.cmd == "archive":
        need_confirm(a)
        page = api("GET", f"/pages/{a.page_id}")
        api("PATCH", f"/pages/{a.page_id}", {"in_trash": True})
        cid = log_change("archive", a.page_id, {"title": title_of(page)})
        out({"moved_to_trash": title_of(page), "restorable": True, "change_id": cid})

    elif a.cmd == "move":
        need_confirm(a)
        page = api("GET", f"/pages/{a.page_id}")
        old = page["parent"]
        if old.get("type") not in ("page_id", "data_source_id"):
            sys.exit(f"Can't move this page safely (its current parent is {old.get('type')}, so it couldn't be undone).")
        api("POST", f"/pages/{a.page_id}/move", {"parent": {"type": "page_id", "page_id": a.to}}, version=MOVE_VERSION)
        cid = log_change("move", a.page_id, {"parent": old, "title": title_of(page)}, {"to": a.to})
        out({"moved": title_of(page), "to": a.to, "change_id": cid})

    elif a.cmd == "log":
        out(read_log()[-a.max:])

    elif a.cmd == "undo":
        entries = read_log()
        e = next((x for x in reversed(entries) if x["id"] == a.change_id), None) if a.change_id else \
            next((x for x in reversed(entries) if x["kind"] != "undo"), None)
        if not e:
            sys.exit("Nothing to undo.")
        if e["kind"] == "create" or e["kind"] == "unarchive":
            api("PATCH", f"/pages/{e['target']}", {"in_trash": True})
        elif e["kind"] == "archive":
            api("PATCH", f"/pages/{e['target']}", {"in_trash": False})
        elif e["kind"] == "append":
            for bid in e["after"]["block_ids"]:
                api("PATCH", f"/blocks/{bid}", {"in_trash": True})
        elif e["kind"] == "move":
            api("POST", f"/pages/{e['target']}/move", {"parent": e["before"]["parent"]}, version=MOVE_VERSION)
        elif e["kind"] == "set":
            api("PATCH", f"/pages/{e['target']}", {"properties": {k: v for k, v in e["before"].items() if v}})
        log_change("undo", e["target"], None, {"undid": e["id"]})
        out({"undone": e["id"], "kind": e["kind"]})


if __name__ == "__main__":
    main()

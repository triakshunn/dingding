"""Nightly Notion backup — runs on the laptop, OUTSIDE the container, so the agent can't touch it.

Saves every page the Home Agent connection can see as a Markdown file:
    ~/notion-backups/YYYY-MM-DD/<title>--<id>.md
Keeps the last 14 days.
"""
import datetime as dt
import json
import os
import re
import shutil
from pathlib import Path

os.environ.setdefault("NOTION_TOKEN_FILE", str(Path.home() / ".local/share/privacy-broker/notion_token"))
import notion  # noqa: E402

ROOT = Path.home() / "notion-backups"
KEEP_DAYS = 14


def main():
    day = ROOT / dt.date.today().isoformat()
    day.mkdir(parents=True, exist_ok=True)
    os.chmod(ROOT, 0o700)
    pages = notion.paged("POST", "/search", {"filter": {"property": "object", "value": "page"}, "page_size": 100})
    ok = 0
    for p in pages:
        title = notion.title_of(p)
        name = re.sub(r"[^\w\- ]+", "", title)[:60].strip() or "untitled"
        try:
            props = {k: notion.prop_value(v) for k, v in p.get("properties", {}).items()}
            body = "\n".join(notion.blocks_to_text(p["id"], depth=4))
            (day / f"{name}--{p['id'][:8]}.md").write_text(
                f"# {title}\n\n<!-- id: {p['id']} | url: {p.get('url')} | edited: {p.get('last_edited_time')} -->\n"
                f"```json\n{json.dumps(props, ensure_ascii=False, indent=1, default=str)}\n```\n\n{body}\n")
            ok += 1
        except SystemExit as e:
            print(f"skipped {title}: {e}")
    for old in ROOT.iterdir():
        try:
            if (dt.date.today() - dt.date.fromisoformat(old.name)).days > KEEP_DAYS:
                shutil.rmtree(old)
        except ValueError:
            pass
    print(f"backed up {ok}/{len(pages)} pages to {day}")


if __name__ == "__main__":
    main()

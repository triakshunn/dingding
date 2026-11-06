"""Actions the broker performs for you: add calendar events, save reply drafts (never sends)."""
import base64
import datetime as dt
import hashlib
import json
import urllib.request
from email.mime.text import MIMEText
from email.utils import parseaddr

from googleapiclient.discovery import build

from auth import load
from mailcore import CONFIG, TZ


def calendar():
    return build("calendar", "v3", credentials=load(CONFIG["calendar_account"]))


def calendar_id():
    return CONFIG.get("add_to_calendar_id", CONFIG["calendar_id"])


def add_event(item) -> str:
    cal = calendar()
    start = dt.datetime.fromisoformat(item["start"])
    key = hashlib.sha1(f"{item['title'].lower()}|{start.date()}".encode()).hexdigest()[:16]
    if cal.events().list(calendarId=calendar_id(), privateExtendedProperty=f"ia_key={key}").execute().get("items"):
        return f"#{item['id']} was already on your calendar."
    if item["has_time"]:
        end = start + dt.timedelta(minutes=item.get("minutes") or 60)
        times = {"start": {"dateTime": start.isoformat()}, "end": {"dateTime": end.isoformat()}}
    else:
        times = {"start": {"date": start.date().isoformat()},
                 "end": {"date": (start.date() + dt.timedelta(days=1)).isoformat()}}
    cal.events().insert(calendarId=calendar_id(), body={
        "summary": item["title"], "location": item.get("location") or None,
        "description": f"From email ({item['from']}): {item['link']}",
        "extendedProperties": {"private": {"ia_key": key}}, **times}).execute()
    return f"✅ Added #{item['id']}: {item['label']}"


def upcoming(days):
    start = dt.datetime.combine(dt.datetime.now(TZ).date(), dt.time(), TZ)
    items = calendar().events().list(calendarId=calendar_id(), timeMin=start.isoformat(),
                                     timeMax=(start + dt.timedelta(days=days)).isoformat(),
                                     singleEvents=True, orderBy="startTime").execute().get("items", [])
    if not items:
        return "Nothing on your calendar for that period."
    out = []
    for e in items:
        s = e["start"]
        when = (dt.datetime.fromisoformat(s["dateTime"]).astimezone(TZ).strftime("%a %b %d %I:%M %p")
                if "dateTime" in s else dt.date.fromisoformat(s["date"]).strftime("%a %b %d") + " (all day)")
        out.append(f"• {when} — {e.get('summary', '')}" + (f" @ {e['location']}" if e.get("location") else ""))
    return "\n".join(out)


# ---------- replies (drafts only) ----------

def _raw(item, body):
    m = MIMEText(body)
    m["To"] = item["to"]
    subj = item["subject"]
    m["Subject"] = subj if subj.lower().startswith("re:") else f"Re: {subj}"
    if item.get("message_id"):
        m["In-Reply-To"] = item["message_id"]
        m["References"] = (item.get("references", "") + " " + item["message_id"]).strip()
    return {"raw": base64.urlsafe_b64encode(m.as_bytes()).decode(), "threadId": item["thread_id"]}


def save_draft(item, body):
    """Create or update the Gmail draft (in the account the email came to). Returns draft id."""
    drafts = build("gmail", "v1", credentials=load(item["acct"])).users().drafts()
    msg = _raw(item, body)
    if item.get("draft_id"):
        try:
            return drafts.update(userId="me", id=item["draft_id"], body={"message": msg}).execute()["id"]
        except Exception:
            pass  # draft was deleted in Gmail; make a new one
    return drafts.create(userId="me", body={"message": msg}).execute()["id"]


def delete_draft(item):
    if item.get("draft_id"):
        try:
            build("gmail", "v1", credentials=load(item["acct"])).users().drafts().delete(
                userId="me", id=item["draft_id"]).execute()
        except Exception:
            pass

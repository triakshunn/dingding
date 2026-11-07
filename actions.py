"""Calendar and Gmail-draft actions the broker carries out for the agent.

Adds suggested events to the user's calendar (deduplicated), lists upcoming
events, and creates or deletes Gmail drafts. Nothing here sends email: the user
taps Send in Gmail.
"""

import base64
import contextlib
import datetime as dt
import hashlib
from email.mime.text import MIMEText

from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

from auth import load
from mailcore import CONFIG
from mailcore import TZ


def calendar():
    """Returns a Calendar API client for the configured calendar account."""
    return build("calendar", "v3", credentials=load(CONFIG["calendar_account"]))


def calendar_id():
    """Returns the id of the calendar that suggestions are added to."""
    return CONFIG.get("add_to_calendar_id", CONFIG["calendar_id"])


def add_event(item) -> str:
    """Adds a pending event suggestion to the calendar, at most once.

    Events are keyed by title and start date in a private extended property, so
    adding the same suggestion twice is a no-op.

    Args:
        item: A pending "event" item (title, start, has_time, minutes, location,
            ...).

    Returns:
        A short message for the agent, e.g. that the event was added or was
            already on the calendar.
    """
    cal = calendar()
    start = dt.datetime.fromisoformat(item["start"])
    key = hashlib.sha1(
        f"{item['title'].lower()}|{start.date()}".encode()
    ).hexdigest()[:16]
    if (
        cal.events()
        .list(calendarId=calendar_id(), privateExtendedProperty=f"ia_key={key}")
        .execute()
        .get("items")
    ):
        return f"#{item['id']} was already on your calendar."
    if item["has_time"]:
        end = start + dt.timedelta(minutes=item.get("minutes") or 60)
        times = {
            "start": {"dateTime": start.isoformat()},
            "end": {"dateTime": end.isoformat()},
        }
    else:
        times = {
            "start": {"date": start.date().isoformat()},
            "end": {"date": (start.date() + dt.timedelta(days=1)).isoformat()},
        }
    cal.events().insert(
        calendarId=calendar_id(),
        body={
            "summary": item["title"],
            "location": item.get("location") or None,
            "description": f"From email ({item['from']}): {item['link']}",
            "extendedProperties": {"private": {"ia_key": key}},
            **times,
        },
    ).execute()
    return f"✅ Added #{item['id']}: {item['label']}"


def upcoming(days):
    """Lists calendar events from today's midnight through `days` days ahead.

    Args:
        days: How many days to look ahead.

    Returns:
        One line per event ("• <when> — <title> @ <location>"), or an empty
        string if nothing is scheduled.
    """
    start = dt.datetime.combine(dt.datetime.now(TZ).date(), dt.time(), TZ)
    items = (
        calendar()
        .events()
        .list(
            calendarId=calendar_id(),
            timeMin=start.isoformat(),
            timeMax=(start + dt.timedelta(days=days)).isoformat(),
            singleEvents=True,
            orderBy="startTime",
        )
        .execute()
        .get("items", [])
    )
    if not items:
        return "Nothing on your calendar for that period."
    out = []
    for e in items:
        s = e["start"]
        when = (
            dt.datetime.fromisoformat(s["dateTime"])
            .astimezone(TZ)
            .strftime("%a %b %d %I:%M %p")
            if "dateTime" in s
            else dt.date.fromisoformat(s["date"]).strftime("%a %b %d")
            + " (all day)"
        )
        out.append(
            f"• {when} — {e.get('summary', '')}"
            + (f" @ {e['location']}" if e.get("location") else "")
        )
    return "\n".join(out)


def events(days):
    """Returns events from today's midnight through `days` days ahead.

    Only the suggestions calendar (calendar_id) is read.

    Returns:
        [{"id", "title", "start", "end", "all_day", "location"}, ...] with
        start/end as ISO dates (all-day) or datetimes.
    """
    start = dt.datetime.combine(dt.datetime.now(TZ).date(), dt.time(), TZ)
    items = (
        calendar()
        .events()
        .list(
            calendarId=calendar_id(),
            timeMin=start.isoformat(),
            timeMax=(start + dt.timedelta(days=days)).isoformat(),
            singleEvents=True,
            orderBy="startTime",
        )
        .execute()
        .get("items", [])
    )
    return [
        {
            "id": e["id"],
            "title": e.get("summary", ""),
            "start": e["start"].get("dateTime") or e["start"].get("date"),
            "end": e["end"].get("dateTime") or e["end"].get("date"),
            "all_day": "date" in e["start"],
            "location": e.get("location", ""),
        }
        for e in items
    ]


def get_event(event_id):
    """Returns one event of the suggestions calendar (raw API dict)."""
    return (
        calendar()
        .events()
        .get(calendarId=calendar_id(), eventId=event_id)
        .execute()
    )


def update_event(event_id, body):
    """Replaces one event of the suggestions calendar with `body`."""
    return (
        calendar()
        .events()
        .update(calendarId=calendar_id(), eventId=event_id, body=body)
        .execute()
    )


# ---------- drafts (never sent: the user taps Send in Gmail) ----------


def _raw(item, body):
    m = MIMEText(body)
    m["To"] = item["to"]
    subj = item["subject"]
    m["Subject"] = subj if subj.lower().startswith("re:") else f"Re: {subj}"
    if item.get("message_id"):
        m["In-Reply-To"] = item["message_id"]
        m["References"] = (
            item.get("references", "") + " " + item["message_id"]
        ).strip()
    return {
        "raw": base64.urlsafe_b64encode(m.as_bytes()).decode(),
        "threadId": item["thread_id"],
    }


def save_draft(item, body):
    """Creates or updates the reply draft in the account the email came to.

    If the draft was deleted in Gmail meanwhile, a new one is created.

    Args:
        item: A pending "reply" item (acct, to, subject, thread_id, message ids,
            draft_id).
        body: The reply text, with placeholders already filled in.

    Returns:
        The Gmail draft id.
    """
    drafts = (
        build("gmail", "v1", credentials=load(item["acct"])).users().drafts()
    )
    msg = _raw(item, body)
    if item.get("draft_id"):
        try:
            return drafts.update(
                userId="me", id=item["draft_id"], body={"message": msg}
            ).execute()["id"]
        except HttpError:
            pass  # The draft was deleted in Gmail: make a new one.
    return drafts.create(userId="me", body={"message": msg}).execute()["id"]


def delete_draft(item):
    """Deletes the item's Gmail draft, if it has one and it still exists."""
    if item.get("draft_id"):
        with contextlib.suppress(HttpError):  # Already deleted in Gmail.
            build(
                "gmail", "v1", credentials=load(item["acct"])
            ).users().drafts().delete(
                userId="me", id=item["draft_id"]
            ).execute()

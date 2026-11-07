"""The only place the broker talks to Google and Notion; tests use a fake.

Runs on the host with the full Gmail logins (data/tokens) and the Notion key.
Nothing here sends email: drafts only. Archiving only removes the Inbox/Unread
labels.
"""

import datetime as dt
from email.utils import parsedate_to_datetime

from googleapiclient.discovery import build

import actions
import notion
from auth import TOKENS
from auth import load
from mailcore import CONFIG
from mailcore import GMAIL_QUERY
from mailcore import TZ
from mailcore import body_text

_REAL_NOTION_API = notion.api


class RealBackend:
    """Gmail, Calendar and Notion access with the user's real credentials.

    The broker calls only these methods, so tests can swap in a fake backend.
    """

    gmail_query = GMAIL_QUERY

    def accounts(self):
        """Returns the account nicknames that have a saved login."""
        return [t.stem for t in sorted(TOKENS.glob("*.json"))]

    def _gmail(self, acct):
        return build(
            "gmail", "v1", credentials=load(acct), cache_discovery=False
        )

    def profile_email(self, acct):
        """Returns the email address of an account."""
        return (
            self._gmail(acct)
            .users()
            .getProfile(userId="me")
            .execute()["emailAddress"]
        )

    def mail_list(self, acct, query, max_results):
        """Returns ids of up to `max_results` messages matching `query`."""
        r = (
            self._gmail(acct)
            .users()
            .messages()
            .list(userId="me", q=query, maxResults=max_results)
            .execute()
        )
        return [m["id"] for m in r.get("messages", [])]

    def mail_get(self, acct, mid):
        """Fetches one message with its plain-text body (at most 12,000 chars).

        Returns:
            A dict with account, id, thread_id, from, reply_to, to, subject,
                date (ISO, user's timezone), message_id, references and body.
        """
        msg = (
            self._gmail(acct)
            .users()
            .messages()
            .get(userId="me", id=mid, format="full")
            .execute()
        )
        h = {
            x["name"].lower(): x["value"]
            for x in msg["payload"].get("headers", [])
        }
        try:
            date = parsedate_to_datetime(h["date"]).astimezone(TZ).isoformat()
        except (
            KeyError,
            TypeError,
            ValueError,
        ):  # Missing/garbled Date header.
            date = dt.datetime.fromtimestamp(
                int(msg["internalDate"]) / 1000, TZ
            ).isoformat()
        return {
            "account": acct,
            "id": mid,
            "thread_id": msg["threadId"],
            "from": h.get("from", ""),
            "reply_to": h.get("reply-to", ""),
            "to": h.get("to", ""),
            "subject": h.get("subject", ""),
            "date": date,
            "message_id": h.get("message-id", ""),
            "references": h.get("references", ""),
            "body": body_text(msg["payload"])[:12000],
        }

    def thread_of(self, acct, mid):
        """Returns the id of the conversation containing a message."""
        return (
            self._gmail(acct)
            .users()
            .messages()
            .get(userId="me", id=mid, format="minimal")
            .execute()["threadId"]
        )

    def thread_meta(self, acct, mid):
        """Returns From/To/Date of every message in the conversation of `mid`.

        Headers only: no bodies are fetched.

        Returns:
            {"thread_id": ..., "messages": [{id, from, to, date, labels}, ...]}.
        """
        g = self._gmail(acct).users()
        tid = (
            g.messages()
            .get(userId="me", id=mid, format="minimal")
            .execute()["threadId"]
        )
        t = (
            g.threads()
            .get(
                userId="me",
                id=tid,
                format="metadata",
                metadataHeaders=["From", "To", "Date"],
            )
            .execute()
        )
        msgs = []
        for m in t.get("messages", []):
            h = {
                x["name"].lower(): x["value"]
                for x in m["payload"].get("headers", [])
            }
            when = dt.datetime.fromtimestamp(
                int(m["internalDate"]) / 1000, TZ
            ).isoformat()
            msgs.append(
                {
                    "id": m["id"],
                    "from": h.get("from", ""),
                    "to": h.get("to", ""),
                    "date": when,
                    "labels": m.get("labelIds", []),
                }
            )
        return {"thread_id": tid, "messages": msgs}

    def archive(self, acct, thread_id, mark_read=True):
        """Removes the Inbox (and optionally Unread) label from a conversation.

        Nothing else changes; the mail stays in All Mail.

        Returns:
            {"unread": ids that were unread}, so unarchive can restore them.
        """
        g = self._gmail(acct).users()
        t = (
            g.threads()
            .get(userId="me", id=thread_id, format="minimal")
            .execute()
        )
        unread = [
            m["id"]
            for m in t.get("messages", [])
            if "UNREAD" in m.get("labelIds", [])
        ]
        g.threads().modify(
            userId="me",
            id=thread_id,
            body={
                "removeLabelIds": ["INBOX"] + (["UNREAD"] if mark_read else [])
            },
        ).execute()
        return {"unread": unread if mark_read else []}

    def unarchive(self, acct, thread_id, unread=()):
        """Moves a conversation back to the Inbox.

        Args:
            acct: Account nickname.
            thread_id: The conversation.
            unread: Message ids to mark unread again (as archive returned them).
        """
        g = self._gmail(acct).users()
        g.threads().modify(
            userId="me", id=thread_id, body={"addLabelIds": ["INBOX"]}
        ).execute()
        if unread:
            g.messages().batchModify(
                userId="me",
                body={"ids": list(unread), "addLabelIds": ["UNREAD"]},
            ).execute()

    def list_ids(self, acct, query, cap, include_spam=False):
        """Returns ids matching a Gmail query (paginated, at most `cap`)."""
        g, ids, token = self._gmail(acct).users(), [], None
        while len(ids) < cap:
            r = (
                g.messages()
                .list(
                    userId="me",
                    q=query,
                    includeSpamTrash=include_spam,
                    pageToken=token,
                    maxResults=min(500, cap - len(ids)),
                )
                .execute()
            )
            ids += [m["id"] for m in r.get("messages", [])]
            token = r.get("nextPageToken")
            if not token:
                break
        return ids[:cap]

    def senders(self, acct, ids):
        """Returns the From header of each message in `ids`."""
        g = self._gmail(acct).users()
        out = []
        for mid in ids:
            h = (
                g.messages()
                .get(
                    userId="me",
                    id=mid,
                    format="metadata",
                    metadataHeaders=["From"],
                )
                .execute()
            )
            out += [
                x["value"]
                for x in h["payload"].get("headers", [])
                if x["name"].lower() == "from"
            ]
        return out

    def relabel(self, acct, ids, add=(), remove=()):
        """Adds/removes labels on many messages (1,000 per call).

        Used only for Trash, Inbox and Spam moves by cleanup and its undo.
        """
        g = self._gmail(acct).users()
        for i in range(0, len(ids), 1000):
            g.messages().batchModify(
                userId="me",
                body={
                    "ids": ids[i : i + 1000],
                    "addLabelIds": list(add),
                    "removeLabelIds": list(remove),
                },
            ).execute()

    def save_draft(self, item, body):
        """Creates or updates a reply draft; never sends."""
        return actions.save_draft(
            item, body
        )  # creates/updates a draft; never sends

    def delete_draft(self, item):
        """Deletes a reply draft if it still exists."""
        actions.delete_draft(item)

    def cal_add(self, item):
        """Adds a pending event to the calendar (see actions.add_event)."""
        return actions.add_event(item)

    def cal_list(self, days):
        """Lists calendar events for the next `days` days."""
        return actions.upcoming(days)

    def notion_api(self, method, path, body=None, version=notion.VERSION):
        """Calls the Notion API with the host's integration key."""
        return _REAL_NOTION_API(method, path, body, version=version)

    def config(self):
        """Returns the user's config (data/config.json)."""
        return CONFIG

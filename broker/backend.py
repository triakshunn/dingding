"""The ONLY place the broker touches Google and Notion. Tests replace this with a fake.

Runs on the host with the FULL Gmail logins (~/dingding/data/tokens) and the Notion key.
Nothing here sends email: drafts only. Archiving only removes the Inbox/Unread labels.
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
    gmail_query = GMAIL_QUERY

    def accounts(self):
        return [t.stem for t in sorted(TOKENS.glob("*.json"))]

    def _gmail(self, acct):
        return build(
            "gmail", "v1", credentials=load(acct), cache_discovery=False
        )

    def profile_email(self, acct):
        return (
            self._gmail(acct)
            .users()
            .getProfile(userId="me")
            .execute()["emailAddress"]
        )

    def mail_list(self, acct, query, max_results):
        r = (
            self._gmail(acct)
            .users()
            .messages()
            .list(userId="me", q=query, maxResults=max_results)
            .execute()
        )
        return [m["id"] for m in r.get("messages", [])]

    def mail_get(self, acct, mid):
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
        return (
            self._gmail(acct)
            .users()
            .messages()
            .get(userId="me", id=mid, format="minimal")
            .execute()["threadId"]
        )

    def thread_meta(self, acct, mid):
        """Headers only (From/To/Date) for every message in the conversation containing `mid`. No bodies."""
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
        """Remove the Inbox label (and optionally Unread) from a whole conversation. Nothing else."""
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
        """Message ids matching a Gmail query (paginated, at most `cap`)."""
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
        """Bulk label change (1000 per call). Used for Trash / Inbox / Spam moves only."""
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
        return actions.save_draft(
            item, body
        )  # creates/updates a draft; never sends

    def delete_draft(self, item):
        actions.delete_draft(item)

    def cal_add(self, item):
        return actions.add_event(item)

    def cal_list(self, days):
        return actions.upcoming(days)

    def notion_api(self, method, path, body=None, version=notion.VERSION):
        return _REAL_NOTION_API(method, path, body, version=version)

    def config(self):
        return CONFIG

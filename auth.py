"""One-time Google sign-in for an account.

Usage:
    uv run auth.py <nickname>              # mail-only account
    uv run auth.py <nickname> --calendar   # account that also holds the AI Suggestions calendar

Saves the login to tokens/<nickname>.json (readable only by you).
"""

import os
import sys
from pathlib import Path

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build

HERE = Path(__file__).parent
# Everything private/changing lives in DATA, so the code folder can be mounted read-only.
DATA = Path(os.environ.get("DINGDING_DATA", HERE / "data"))
TOKENS = DATA / "tokens"

MAIL_SCOPES = [
    "https://www.googleapis.com/auth/gmail.readonly",  # read mail
    "https://www.googleapis.com/auth/gmail.compose",  # create drafts (this code never sends)
    "https://www.googleapis.com/auth/gmail.insert",  # drop the morning summary into the inbox
    "https://www.googleapis.com/auth/gmail.modify",  # archive (remove Inbox label) when the user asks
]
# Only calendars this app creates; it cannot touch your other calendars.
CALENDAR_SCOPES = ["https://www.googleapis.com/auth/calendar.app.created"]


READONLY_MAIL_SCOPES = [
    "https://www.googleapis.com/auth/gmail.readonly"
]  # for the OpenClaw container: cannot send or draft


def scopes_for(with_calendar: bool, readonly: bool = False) -> list[str]:
    return (READONLY_MAIL_SCOPES if readonly else MAIL_SCOPES) + (
        CALENDAR_SCOPES if with_calendar else []
    )


def load(nickname: str) -> Credentials:
    """Load saved credentials, refreshing them if expired."""
    path = TOKENS / f"{nickname}.json"
    creds = Credentials.from_authorized_user_file(path)
    if not creds.valid and creds.refresh_token:
        creds.refresh(Request())
        save(path, creds)
    return creds


def save(path: Path, creds: Credentials) -> None:
    TOKENS.mkdir(mode=0o700, exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(creds.to_json())


def main() -> None:
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    if len(args) != 1:
        sys.exit(__doc__)
    nickname = args[0]
    with_calendar = "--calendar" in sys.argv
    readonly = "--readonly" in sys.argv

    flow = InstalledAppFlow.from_client_secrets_file(
        HERE / "credentials.json", scopes_for(with_calendar, readonly)
    )
    print(
        f"Opening your browser. Sign in with the account you want to call '{nickname}'."
    )
    creds = flow.run_local_server(
        port=0, prompt="consent", access_type="offline"
    )

    email = (
        build("gmail", "v1", credentials=creds)
        .users()
        .getProfile(userId="me")
        .execute()["emailAddress"]
    )
    save(TOKENS / f"{nickname}.json", creds)
    print(
        f"\n✅ Connected '{nickname}' = {email}"
        + (" (with calendar)" if with_calendar else "")
        + (" [read-only]" if readonly else "")
        + f" -> {TOKENS}"
    )


if __name__ == "__main__":
    main()

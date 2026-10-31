"""Gmail API access using an OAuth2 refresh token for your own Google account.

Scopes:
  gmail.send      - send match notifications
  gmail.readonly  - read job-alert emails from LinkedIn, Naukri, Indeed and the other portals

Obtain the refresh token once with ``python scripts/gmail_auth.py`` (see README).
"""
from __future__ import annotations

import base64
import logging
from email.message import EmailMessage
from functools import lru_cache

from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build

from backend.config import get_settings

log = logging.getLogger(__name__)

SCOPES = [
    "https://www.googleapis.com/auth/gmail.send",
    "https://www.googleapis.com/auth/gmail.readonly",
]


class GmailNotConfigured(RuntimeError):
    pass


@lru_cache
def get_service():
    s = get_settings()
    if not s.gmail_configured:
        raise GmailNotConfigured("GMAIL_CLIENT_ID / GMAIL_CLIENT_SECRET / GMAIL_REFRESH_TOKEN are not set")
    creds = Credentials(
        token=None,
        refresh_token=s.gmail_refresh_token,
        client_id=s.gmail_client_id,
        client_secret=s.gmail_client_secret,
        token_uri="https://oauth2.googleapis.com/token",
        scopes=SCOPES,
    )
    # cache_discovery=False avoids a noisy file-cache warning inside containers
    return build("gmail", "v1", credentials=creds, cache_discovery=False)


def send_email(to: str, subject: str, text_body: str, html_body: str | None = None) -> str:
    msg = EmailMessage()
    msg["To"] = to
    msg["From"] = "me"
    msg["Subject"] = subject
    msg.set_content(text_body)
    if html_body:
        msg.add_alternative(html_body, subtype="html")
    raw = base64.urlsafe_b64encode(msg.as_bytes()).decode()
    result = get_service().users().messages().send(userId="me", body={"raw": raw}).execute(num_retries=3)
    return result["id"]


def list_message_ids(query: str, max_results: int = 50) -> list[str]:
    resp = (
        get_service()
        .users()
        .messages()
        .list(userId="me", q=query, maxResults=max_results)
        .execute(num_retries=3)
    )
    return [m["id"] for m in resp.get("messages", [])]


def get_message(message_id: str) -> dict:
    """Returns {'id', 'internal_date_ms', 'from', 'subject', 'html', 'text'}."""
    msg = get_service().users().messages().get(userId="me", id=message_id, format="full").execute(num_retries=3)
    headers = {h["name"].lower(): h["value"] for h in msg.get("payload", {}).get("headers", [])}
    html_parts: list[str] = []
    text_parts: list[str] = []

    def walk(part: dict) -> None:
        mime = part.get("mimeType", "")
        data = part.get("body", {}).get("data")
        if data:
            decoded = base64.urlsafe_b64decode(data + "=" * (-len(data) % 4)).decode("utf-8", errors="replace")
            if mime == "text/html":
                html_parts.append(decoded)
            elif mime == "text/plain":
                text_parts.append(decoded)
        for sub in part.get("parts", []) or []:
            walk(sub)

    walk(msg.get("payload", {}))
    return {
        "id": msg["id"],
        "internal_date_ms": int(msg.get("internalDate", 0)),
        "from": headers.get("from", ""),
        "subject": headers.get("subject", ""),
        "html": "\n".join(html_parts),
        "text": "\n".join(text_parts),
    }

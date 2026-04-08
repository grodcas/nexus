#!/usr/bin/env python3
"""
sync_gmail.py — Pull Gmail threads via Gmail API (OAuth2).

First run opens browser for auth. Token is cached for subsequent runs.
Outputs JSON to ~/.nexus/management/raw/gmail.json
Pulls recent threads: unread + last 3 days of read mail.
"""

import os
import json
import base64
from datetime import datetime, timedelta
from email.utils import parsedate_to_datetime

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build

SCOPES = ["https://www.googleapis.com/auth/gmail.readonly"]
CREDS_PATH = os.path.expanduser("~/.nexus/credentials/gmail_credentials.json")
TOKEN_PATH = os.path.expanduser("~/.nexus/credentials/gmail_token.json")
RAW_DIR = os.path.expanduser("~/.nexus/management/raw")


def get_service():
    creds = None
    if os.path.exists(TOKEN_PATH):
        creds = Credentials.from_authorized_user_file(TOKEN_PATH, SCOPES)

    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            flow = InstalledAppFlow.from_client_secrets_file(CREDS_PATH, SCOPES)
            creds = flow.run_local_server(port=0)
        with open(TOKEN_PATH, "w") as f:
            f.write(creds.to_json())

    return build("gmail", "v1", credentials=creds)


def get_header(headers: list, name: str) -> str:
    for h in headers:
        if h["name"].lower() == name.lower():
            return h["value"]
    return ""


def fetch_threads(service, query: str, max_results: int = 50) -> list[dict]:
    """Fetch threads matching query, return enriched thread data."""
    threads = []
    result = service.users().threads().list(
        userId="me", q=query, maxResults=max_results
    ).execute()

    thread_ids = [t["id"] for t in result.get("threads", [])]

    for tid in thread_ids:
        thread = service.users().threads().get(
            userId="me", id=tid, format="metadata",
            metadataHeaders=["From", "To", "Subject", "Date"]
        ).execute()

        messages = thread.get("messages", [])
        if not messages:
            continue

        first_msg = messages[0]
        last_msg = messages[-1]
        headers_first = first_msg.get("payload", {}).get("headers", [])
        headers_last = last_msg.get("payload", {}).get("headers", [])

        # Check if any message is unread
        unread = any("UNREAD" in m.get("labelIds", []) for m in messages)

        # Get labels from first message
        labels = first_msg.get("labelIds", [])

        threads.append({
            "id": tid,
            "subject": get_header(headers_first, "Subject"),
            "from": get_header(headers_first, "From"),
            "to": get_header(headers_first, "To"),
            "date": get_header(headers_first, "Date"),
            "last_date": get_header(headers_last, "Date"),
            "last_from": get_header(headers_last, "From"),
            "message_count": len(messages),
            "unread": unread,
            "labels": labels,
            "snippet": first_msg.get("snippet", ""),
        })

    return threads


def sync(days=3, max_threads=80):
    service = get_service()

    after_date = (datetime.now() - timedelta(days=days)).strftime("%Y/%m/%d")

    # Unread emails (any age, up to limit)
    unread = fetch_threads(service, "is:unread", max_results=50)

    # Recent threads (last N days)
    recent = fetch_threads(service, f"after:{after_date}", max_results=max_threads)

    # Merge: unread first, then recent (deduplicated)
    seen_ids = set()
    all_threads = []
    for t in unread:
        if t["id"] not in seen_ids:
            seen_ids.add(t["id"])
            all_threads.append(t)
    for t in recent:
        if t["id"] not in seen_ids:
            seen_ids.add(t["id"])
            all_threads.append(t)

    unread_count = sum(1 for t in all_threads if t["unread"])

    os.makedirs(RAW_DIR, exist_ok=True)
    out_path = os.path.join(RAW_DIR, "gmail.json")
    with open(out_path, "w") as f:
        json.dump({
            "synced_at": datetime.now().isoformat(),
            "account": "gines.rodriguez.castro@gmail.com",
            "range_days": days,
            "total_threads": len(all_threads),
            "unread_count": unread_count,
            "threads": all_threads,
        }, f, indent=2, ensure_ascii=False)

    print(f"Gmail: {len(all_threads)} threads ({unread_count} unread)")
    return all_threads


if __name__ == "__main__":
    sync()

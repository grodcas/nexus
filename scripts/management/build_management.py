#!/usr/bin/env python3
"""
build_management.py — Build management worktree from raw sync data.

Reads:  ~/.nexus/management/raw/{calendar,reminders,gmail}.json
Writes: ~/.nexus/management/{root.md, calendar.md, reminders.md, email.md}

Same pattern as documents worktree: structured markdown files that Claude
navigates top-down. root.md is the entry point.
"""

import json
import os
from datetime import datetime, timedelta
from email.utils import parsedate_to_datetime

EMAIL_BRIEFING_CHAR_CAP = 400

RAW_DIR = os.path.expanduser("~/.nexus/management/raw")
OUT_DIR = os.path.expanduser("~/.nexus/management")


def load_raw(name: str) -> dict:
    path = os.path.join(RAW_DIR, f"{name}.json")
    if not os.path.exists(path):
        return {}
    with open(path) as f:
        return json.load(f)


def build_calendar_md(data: dict) -> str:
    if not data:
        return "# Calendar\n\nNo calendar data synced yet. Run `sync_calendar.py`.\n"

    events = data.get("events", [])
    today = datetime.now().strftime("%Y-%m-%d")
    tomorrow = (datetime.now() + timedelta(days=1)).strftime("%Y-%m-%d")

    today_events = []
    tomorrow_events = []
    upcoming_events = []

    for e in events:
        start = e.get("start", "")
        if start.startswith(today):
            today_events.append(e)
        elif start.startswith(tomorrow):
            tomorrow_events.append(e)
        else:
            upcoming_events.append(e)

    lines = [f"# Calendar\n"]
    lines.append(f"Last synced: {data.get('synced_at', 'unknown')}")
    lines.append(f"Total events: {data.get('count', 0)}\n")

    def format_event(e):
        start = e.get("start", "?")
        # Extract time portion
        time_str = start[11:16] if len(start) > 11 else "all-day" if e.get("allday") else start
        title = e.get("title", "Untitled")
        cal = e.get("calendar", "")
        loc = e.get("location", "")
        line = f"- **{time_str}** {title}"
        if cal:
            line += f" [{cal}]"
        if loc:
            line += f" — {loc}"
        return line

    lines.append(f"## Today ({today})\n")
    if today_events:
        for e in today_events:
            lines.append(format_event(e))
    else:
        lines.append("No events today.")

    lines.append(f"\n## Tomorrow ({tomorrow})\n")
    if tomorrow_events:
        for e in tomorrow_events:
            lines.append(format_event(e))
    else:
        lines.append("No events tomorrow.")

    lines.append(f"\n## Upcoming\n")
    if upcoming_events:
        # Group by date
        by_date = {}
        for e in upcoming_events:
            date = e.get("start", "?")[:10]
            by_date.setdefault(date, []).append(e)
        for date, evts in sorted(by_date.items()):
            lines.append(f"### {date}")
            for e in evts:
                lines.append(format_event(e))
            lines.append("")
    else:
        lines.append("No upcoming events.")

    return "\n".join(lines) + "\n"


def build_reminders_md(data: dict) -> str:
    if not data:
        return "# Reminders\n\nNo reminders data synced yet. Run `sync_reminders.py`.\n"

    incomplete = data.get("incomplete", [])
    completed = data.get("recently_completed", [])

    lines = [f"# Reminders\n"]
    lines.append(f"Last synced: {data.get('synced_at', 'unknown')}")
    lines.append(f"Pending: {len(incomplete)} | Recently completed: {len(completed)}\n")

    # Group by list
    by_list = {}
    for r in incomplete:
        lst = r.get("list", "Unknown")
        by_list.setdefault(lst, []).append(r)

    lines.append("## Pending\n")
    if by_list:
        for lst, reminders in sorted(by_list.items()):
            lines.append(f"### {lst}\n")
            for r in reminders:
                title = r.get("title", "Untitled")
                due = r.get("due", "")
                flagged = r.get("flagged", False)
                priority = r.get("priority", 0)

                prefix = "- "
                if flagged:
                    prefix = "- [!] "
                if priority and priority > 0:
                    prefix = f"- [P{priority}] "

                line = f"{prefix}**{title}**"
                if due:
                    line += f" (due: {due})"
                if r.get("body"):
                    line += f"\n  {r['body']}"
                lines.append(line)
            lines.append("")
    else:
        lines.append("No pending reminders.\n")

    if completed:
        lines.append("## Recently Completed (last 7 days)\n")
        for r in completed:
            lines.append(f"- ~~{r.get('title', 'Untitled')}~~ (done: {r.get('completed_date', '?')})")
        lines.append("")

    return "\n".join(lines) + "\n"


def build_email_md(data: dict) -> str:
    if not data:
        return "# Email\n\nNo email data synced yet. Run `sync_gmail.py`.\n"

    threads = data.get("threads", [])
    unread_count = data.get("unread_count", 0)

    today = datetime.now().date()

    def thread_date(t):
        raw = t.get("last_date") or t.get("date") or ""
        try:
            return parsedate_to_datetime(raw).astimezone().date()
        except (TypeError, ValueError):
            return None

    todays = [t for t in threads if thread_date(t) == today]

    def format_thread(t):
        subject = t.get("subject", "(no subject)")
        frm = t.get("from", "?")
        if "<" in frm:
            frm = frm.split("<")[0].strip().strip('"')
        return f"- {subject} — {frm}"

    header = (
        f"# Email ({data.get('account', 'unknown')})\n\n"
        f"Last synced: {data.get('synced_at', 'unknown')}\n"
        f"Threads: {len(threads)} | Unread: {unread_count}\n\n"
        f"## Today\n\n"
    )

    if not todays:
        return header + "No emails today.\n"

    body_lines = []
    used = 0
    shown = 0
    for t in todays:
        line = format_thread(t)
        if used + len(line) + 1 > EMAIL_BRIEFING_CHAR_CAP:
            break
        body_lines.append(line)
        used += len(line) + 1
        shown += 1

    remaining = len(todays) - shown
    if remaining > 0:
        body_lines.append(f"(+{remaining} more today)")

    return header + "\n".join(body_lines) + "\n"


def build_root_md(cal_data: dict, rem_data: dict, email_data: dict) -> str:
    today = datetime.now().strftime("%Y-%m-%d")
    now = datetime.now().strftime("%Y-%m-%d %H:%M")

    # Quick counts
    cal_events = cal_data.get("events", [])
    today_str = datetime.now().strftime("%Y-%m-%d")
    today_events = [e for e in cal_events if e.get("start", "").startswith(today_str)]

    incomplete = rem_data.get("incomplete", [])
    unread_count = email_data.get("unread_count", 0)

    lines = [f"# Management Worktree — Daily Overview\n"]
    lines.append(f"Last updated: {now}\n")

    lines.append("## At a Glance\n")
    lines.append(f"- Calendar: **{len(today_events)} events today**, {len(cal_events)} total upcoming")
    lines.append(f"- Reminders: **{len(incomplete)} pending**")
    lines.append(f"- Email: **{unread_count} unread**")
    lines.append("")

    # Today's schedule summary
    lines.append("## Today's Schedule\n")
    if today_events:
        for e in today_events:
            start = e.get("start", "")
            time_str = start[11:16] if len(start) > 11 else "all-day"
            lines.append(f"- {time_str} — {e.get('title', 'Untitled')}")
    else:
        lines.append("No events scheduled today.")
    lines.append("")

    # Priority reminders (flagged or with due date today)
    urgent = [r for r in incomplete if r.get("flagged") or
              (r.get("due", "").startswith(today_str))]
    if urgent:
        lines.append("## Urgent Reminders\n")
        for r in urgent:
            lines.append(f"- {r.get('title', 'Untitled')}")
        lines.append("")

    # Today's emails (matches email.md filtering)
    today_date = datetime.now().date()
    threads = email_data.get("threads", [])
    todays_emails = []
    for t in threads:
        raw = t.get("last_date") or t.get("date") or ""
        try:
            if parsedate_to_datetime(raw).astimezone().date() == today_date:
                todays_emails.append(t)
        except (TypeError, ValueError):
            continue

    if todays_emails:
        lines.append("## Today's Emails\n")
        used = 0
        shown = 0
        for t in todays_emails:
            frm = t.get("from", "?")
            if "<" in frm:
                frm = frm.split("<")[0].strip().strip('"')
            line = f"- {t.get('subject', '(no subject)')} — {frm}"
            if used + len(line) + 1 > EMAIL_BRIEFING_CHAR_CAP:
                break
            lines.append(line)
            used += len(line) + 1
            shown += 1
        remaining = len(todays_emails) - shown
        if remaining > 0:
            lines.append(f"(+{remaining} more today)")
        lines.append("")

    lines.append("## Detail Files\n")
    lines.append("- [calendar.md](calendar.md) — Full schedule (today + 14 days)")
    lines.append("- [reminders.md](reminders.md) — All pending reminders by list")
    lines.append("- [email.md](email.md) — Email threads (unread + recent)")
    lines.append("")

    return "\n".join(lines) + "\n"


def build():
    cal_data = load_raw("calendar")
    rem_data = load_raw("reminders")
    email_data = load_raw("gmail")

    os.makedirs(OUT_DIR, exist_ok=True)

    # Build individual detail files
    with open(os.path.join(OUT_DIR, "calendar.md"), "w") as f:
        f.write(build_calendar_md(cal_data))

    with open(os.path.join(OUT_DIR, "reminders.md"), "w") as f:
        f.write(build_reminders_md(rem_data))

    with open(os.path.join(OUT_DIR, "email.md"), "w") as f:
        f.write(build_email_md(email_data))

    # Build root summary
    with open(os.path.join(OUT_DIR, "root.md"), "w") as f:
        f.write(build_root_md(cal_data, rem_data, email_data))

    print(f"Management worktree built at {OUT_DIR}/")
    print(f"  root.md | calendar.md | reminders.md | email.md")


if __name__ == "__main__":
    build()

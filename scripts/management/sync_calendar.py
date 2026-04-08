#!/usr/bin/env python3
"""
sync_calendar.py — Pull Apple Calendar events via AppleScript.

Outputs JSON to ~/.nexus/management/raw/calendar.json
Pulls events from today through +14 days.
"""

import subprocess
import json
import os
from datetime import datetime, timedelta

RAW_DIR = os.path.expanduser("~/.nexus/management/raw")

APPLESCRIPT = '''
set today to current date
set time of today to 0
set endDate to today + {days} * days

set output to ""

tell application "Calendar"
    repeat with cal in calendars
        set calName to name of cal
        set calEvents to (every event of cal whose start date >= today and start date <= endDate)
        repeat with evt in calEvents
            set evtTitle to summary of evt
            set evtStart to start date of evt
            set evtEnd to end date of evt
            set evtLoc to ""
            try
                set evtLoc to location of evt
            end try
            set evtNotes to ""
            try
                set evtNotes to description of evt
            end try
            set evtAllDay to allday event of evt

            set output to output & "<<EVENT>>" & linefeed
            set output to output & "calendar:" & calName & linefeed
            set output to output & "title:" & evtTitle & linefeed
            set output to output & "start:" & (evtStart as «class isot» as string) & linefeed
            set output to output & "end:" & (evtEnd as «class isot» as string) & linefeed
            set output to output & "location:" & evtLoc & linefeed
            set output to output & "notes:" & evtNotes & linefeed
            set output to output & "allday:" & evtAllDay & linefeed
        end repeat
    end repeat
end tell

return output
'''


def parse_applescript_output(raw: str) -> list[dict]:
    events = []
    blocks = raw.split("<<EVENT>>")
    for block in blocks:
        block = block.strip()
        if not block:
            continue
        evt = {}
        for line in block.split("\n"):
            if ":" in line:
                key, _, val = line.partition(":")
                evt[key.strip()] = val.strip()
        if evt.get("title"):
            evt["allday"] = evt.get("allday", "false").lower() == "true"
            events.append(evt)
    return events


def sync(days=14):
    script = APPLESCRIPT.format(days=days)
    result = subprocess.run(
        ["osascript", "-e", script],
        capture_output=True, text=True, timeout=30
    )
    if result.returncode != 0:
        print(f"AppleScript error: {result.stderr}")
        return []

    events = parse_applescript_output(result.stdout)
    events.sort(key=lambda e: e.get("start", ""))

    os.makedirs(RAW_DIR, exist_ok=True)
    out_path = os.path.join(RAW_DIR, "calendar.json")
    with open(out_path, "w") as f:
        json.dump({
            "synced_at": datetime.now().isoformat(),
            "range_days": days,
            "count": len(events),
            "events": events,
        }, f, indent=2, ensure_ascii=False)

    print(f"Calendar: {len(events)} events synced ({days} days)")
    return events


if __name__ == "__main__":
    sync()

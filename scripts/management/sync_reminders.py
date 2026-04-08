#!/usr/bin/env python3
"""
sync_reminders.py — Pull Apple Reminders via AppleScript.

Outputs JSON to ~/.nexus/management/raw/reminders.json
Pulls all incomplete reminders + recently completed (last 7 days).
"""

import subprocess
import json
import os
from datetime import datetime

RAW_DIR = os.path.expanduser("~/.nexus/management/raw")

APPLESCRIPT_INCOMPLETE = '''
set output to ""

tell application "Reminders"
    repeat with reminderList in lists
        set listName to name of reminderList
        set incompleteItems to (every reminder of reminderList whose completed is false)
        repeat with r in incompleteItems
            set rName to name of r
            set rBody to ""
            try
                set rBody to body of r
            end try
            set rDue to ""
            try
                set rDue to (due date of r as «class isot» as string)
            end try
            set rPriority to priority of r
            set rCreated to (creation date of r as «class isot» as string)
            set rFlagged to flagged of r

            set output to output & "<<REMINDER>>" & linefeed
            set output to output & "list:" & listName & linefeed
            set output to output & "title:" & rName & linefeed
            set output to output & "body:" & rBody & linefeed
            set output to output & "due:" & rDue & linefeed
            set output to output & "priority:" & rPriority & linefeed
            set output to output & "created:" & rCreated & linefeed
            set output to output & "flagged:" & rFlagged & linefeed
            set output to output & "completed:false" & linefeed
        end repeat
    end repeat
end tell

return output
'''

APPLESCRIPT_COMPLETED = '''
set output to ""
set cutoff to (current date) - 7 * days

tell application "Reminders"
    repeat with reminderList in lists
        set listName to name of reminderList
        set doneItems to (every reminder of reminderList whose completed is true and completion date >= cutoff)
        repeat with r in doneItems
            set rName to name of r
            set rDone to (completion date of r as «class isot» as string)

            set output to output & "<<REMINDER>>" & linefeed
            set output to output & "list:" & listName & linefeed
            set output to output & "title:" & rName & linefeed
            set output to output & "completed:true" & linefeed
            set output to output & "completed_date:" & rDone & linefeed
        end repeat
    end repeat
end tell

return output
'''


def parse_applescript_output(raw: str) -> list[dict]:
    reminders = []
    blocks = raw.split("<<REMINDER>>")
    for block in blocks:
        block = block.strip()
        if not block:
            continue
        r = {}
        for line in block.split("\n"):
            if ":" in line:
                key, _, val = line.partition(":")
                r[key.strip()] = val.strip()
        if r.get("title"):
            r["flagged"] = r.get("flagged", "false").lower() == "true"
            r["completed"] = r.get("completed", "false").lower() == "true"
            try:
                r["priority"] = int(r.get("priority", 0))
            except ValueError:
                r["priority"] = 0
            reminders.append(r)
    return reminders


def sync():
    # Incomplete reminders
    result = subprocess.run(
        ["osascript", "-e", APPLESCRIPT_INCOMPLETE],
        capture_output=True, text=True, timeout=30
    )
    incomplete = parse_applescript_output(result.stdout) if result.returncode == 0 else []

    # Recently completed
    result = subprocess.run(
        ["osascript", "-e", APPLESCRIPT_COMPLETED],
        capture_output=True, text=True, timeout=30
    )
    completed = parse_applescript_output(result.stdout) if result.returncode == 0 else []

    os.makedirs(RAW_DIR, exist_ok=True)
    out_path = os.path.join(RAW_DIR, "reminders.json")
    with open(out_path, "w") as f:
        json.dump({
            "synced_at": datetime.now().isoformat(),
            "incomplete_count": len(incomplete),
            "completed_recent_count": len(completed),
            "incomplete": incomplete,
            "recently_completed": completed,
        }, f, indent=2, ensure_ascii=False)

    print(f"Reminders: {len(incomplete)} pending, {len(completed)} recently completed")
    return incomplete, completed


if __name__ == "__main__":
    sync()

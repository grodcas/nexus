#!/usr/bin/env python3
"""
sync_all.py — Sync all sources and rebuild management worktree.

Usage:
    python3 scripts/management/sync_all.py           # sync all + build
    python3 scripts/management/sync_all.py --build    # rebuild from existing raw data (no sync)
    python3 scripts/management/sync_all.py --calendar  # sync only calendar + rebuild
    python3 scripts/management/sync_all.py --reminders # sync only reminders + rebuild
    python3 scripts/management/sync_all.py --email     # sync only email + rebuild
"""

import sys
import time

from sync_calendar import sync as sync_calendar
from sync_reminders import sync as sync_reminders
from sync_gmail import sync as sync_gmail
from build_management import build


def main():
    args = set(sys.argv[1:])
    sync_all = not args or args == {"--build"}
    build_only = "--build" in args

    start = time.time()

    if not build_only:
        if sync_all or "--calendar" in args:
            try:
                sync_calendar()
            except Exception as e:
                print(f"Calendar sync failed: {e}")

        if sync_all or "--reminders" in args:
            try:
                sync_reminders()
            except Exception as e:
                print(f"Reminders sync failed: {e}")

        if sync_all or "--email" in args:
            try:
                sync_gmail()
            except Exception as e:
                print(f"Email sync failed: {e}")

    build()
    elapsed = time.time() - start
    print(f"\nDone in {elapsed:.1f}s")


if __name__ == "__main__":
    main()

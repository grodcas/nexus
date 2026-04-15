#!/usr/bin/env python3
"""
plan1_baseline.py — measure handle_tool cold/warm for every action.

Run:
    python eval/plan1_baseline.py           # all actions including browse
    python eval/plan1_baseline.py --no-browse   # skip the slow claude-subprocess path

Writes events via voice/metrics.py to ~/.nexus/metrics/handle_tool.jsonl.
Prints a summary table to stdout.
"""

from __future__ import annotations

import argparse
import os
import sys
import time

ROOT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.abspath(os.path.join(ROOT, "..", "voice")))
sys.path.insert(0, os.path.abspath(os.path.join(ROOT, "..", "scripts")))

from jarvis_slim import handle_tool  # noqa: E402
from metrics import flush  # noqa: E402


# Each case is (action, query, session). Safe — no state mutations, no
# handoff staged. `code` with query="nexus" and no session returns the
# sessions list; does NOT stage a handoff.
CASES = [
    ("window",    "list",                               ""),
    ("window",    "list",                               ""),  # second list
    ("briefing",  "",                                   ""),
    ("calendar",  "",                                   ""),
    ("email",     "",                                   ""),
    ("reminders", "",                                   ""),
    ("documents", "drone",                              ""),
    ("documents", "sensor",                             ""),
    ("github",    "",                                   ""),
    ("code",      "nexus",                              ""),
]

BROWSE_CASE = ("browse", "what is the weather in barcelona", "")


def run(include_browse: bool):
    cases = list(CASES)
    if include_browse:
        cases.append(BROWSE_CASE)

    results = []
    # Each action is run twice back-to-back so the first run is cold
    # (mark_cold_warm fires the first time for that action label, not
    # the second). Exception: "window list" is duplicated inside CASES
    # above so we can observe a warm second call alongside the cold.
    seen_actions: set[str] = set()
    for action, query, session in cases:
        cold = action not in seen_actions
        seen_actions.add(action)
        label = "cold" if cold else "warm"

        start = time.perf_counter()
        try:
            result, is_long = handle_tool(action, query, session)
            ok = True
            err = None
        except Exception as e:
            result = ""
            is_long = False
            ok = False
            err = str(e)[:200]
        dur_ms = (time.perf_counter() - start) * 1000

        # For any action not duplicated in CASES, run a second warm call.
        results.append({
            "action": action,
            "run": label,
            "duration_ms": round(dur_ms, 1),
            "result_len": len(result),
            "is_long": is_long,
            "ok": ok,
            "error": err,
        })
        status = "OK " if ok else "ERR"
        print(f"  {status}  {action:10s} {label:5s} {dur_ms:9.1f}ms  len={len(result):>5}  "
              f"is_long={str(is_long):<5}  err={err or ''}")

        # For single-listed actions (not window), run once more warm.
        if cold and action not in ("window",):
            start = time.perf_counter()
            try:
                result, is_long = handle_tool(action, query, session)
                ok = True
                err = None
            except Exception as e:
                result = ""
                is_long = False
                ok = False
                err = str(e)[:200]
            dur_ms = (time.perf_counter() - start) * 1000
            results.append({
                "action": action,
                "run": "warm",
                "duration_ms": round(dur_ms, 1),
                "result_len": len(result),
                "is_long": is_long,
                "ok": ok,
                "error": err,
            })
            status = "OK " if ok else "ERR"
            print(f"  {status}  {action:10s} warm  {dur_ms:9.1f}ms  len={len(result):>5}  "
                  f"is_long={str(is_long):<5}  err={err or ''}")

    flush()
    return results


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--no-browse", action="store_true",
                    help="Skip the browse case (slow; spawns claude subprocess).")
    args = ap.parse_args()

    print("Plan 1 baseline — handle_tool cold+warm sweep\n")
    print("  result        action     run        duration  length  long    error")
    print("  ------------------------------------------------------------------")
    results = run(include_browse=not args.no_browse)
    total = sum(r["duration_ms"] for r in results)
    print()
    print(f"  {len(results)} runs, total {total/1000:.1f}s of work")
    print(f"  JSONL: ~/.nexus/metrics/handle_tool.jsonl")


if __name__ == "__main__":
    main()

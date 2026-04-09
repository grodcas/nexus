#!/usr/bin/env python3
"""
playwright_demo.py — first contact with Playwright + screen positioning.

What this does:
  1. Launches a real (headed) Chromium browser via Playwright
  2. Navigates to Google Images and searches for a query
  3. Waits for results to load
  4. Uses scripts/screens.py to position the Chromium window on the
     laptop display (the left half of the virtual desktop)
  5. Leaves the browser open for ~30 seconds so you can watch / interact,
     then closes cleanly

This is the Phase B smoke test from the screen+browser test plan: prove
that a Playwright-controlled browser is just another window we can place
with our existing AppleScript primitives, and that it operates without
stealing the system cursor.

Run:
    cd ~/nexus && source venv/bin/activate
    python scripts/playwright_demo.py "drone lidar sensor"
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

# Make scripts/screens.py importable
sys.path.insert(0, str(Path(__file__).parent))

from playwright.sync_api import sync_playwright

import screens


QUERY = sys.argv[1] if len(sys.argv) > 1 else "drone lidar sensor"

# Target placement: laptop display (left half of virtual desktop).
# From our probe: virtual desktop is (0,0)-(3390,1080).
# The laptop "Color LCD" sits at the left, ~1470 pts wide in logical
# coordinates. The R27qe external sits to its right.
TARGET_X = 50
TARGET_Y = 50
TARGET_W = 1300
TARGET_H = 950


def main() -> None:
    # Capture the user's currently-focused app BEFORE launching the browser,
    # so we can refocus it once the browser window appears. This is the
    # focus-preservation fix — without it, the Playwright window steals
    # focus and any typing the user does goes into the browser instead.
    previous_app = screens.get_frontmost_app()
    print(f"[demo] previously focused app: {previous_app}")

    print(f"[demo] launching headed Chromium")
    with sync_playwright() as pw:
        browser = pw.chromium.launch(
            headless=False,
            args=[
                # Give the window a recognizable title prefix so we can
                # find it via AppleScript by process name "Chromium".
                "--window-name=Nexus-Browser",
                f"--window-size={TARGET_W},{TARGET_H}",
                f"--window-position={TARGET_X},{TARGET_Y}",
            ],
        )
        context = browser.new_context(viewport={"width": TARGET_W, "height": TARGET_H})
        page = context.new_page()

        # IMMEDIATELY refocus the user's previous app — the Chromium
        # window has just appeared and stolen focus. We yank it back
        # before any navigation happens so the user's typing destination
        # is restored as soon as possible (~200ms flicker).
        if previous_app:
            try:
                screens.focus_app(previous_app)
                print(f"[demo] refocused {previous_app}")
            except Exception as e:
                print(f"[demo] could not refocus {previous_app}: {e}")

        # DuckDuckGo Images — no captcha, no consent dialog
        url = f"https://duckduckgo.com/?q={QUERY.replace(' ', '+')}&iar=images&iax=images&ia=images"
        print(f"[demo] navigating to: {url}")
        page.goto(url)

        # Wait for image results
        try:
            page.wait_for_selector("img.tile--img__img", timeout=15000)
            print("[demo] image results loaded")
        except Exception as e:
            print(f"[demo] warning: image results never appeared: {e}")

        # Position the browser window via AppleScript using the
        # known process name (Playwright runs as "Google Chrome for Testing").
        time.sleep(1)
        win = screens.find_window(screens.BROWSER_PROCESS)
        if win is None:
            print("[demo] could not find browser window")
        else:
            print(f"[demo] found: {win}")
            screens.place_window(screens.BROWSER_PROCESS,
                                 TARGET_X, TARGET_Y, TARGET_W, TARGET_H)
            after = screens.find_window(screens.BROWSER_PROCESS)
            print(f"[demo] after placement: {after}")

            # Refocus the user's previous app a SECOND time, because
            # AppleScript window manipulation can briefly grab focus too.
            if previous_app:
                screens.focus_app(previous_app)
                print(f"[demo] refocused {previous_app} after placement")

        # Capture a screenshot for the record
        out = Path(__file__).parent / "playwright_demo_screenshot.png"
        page.screenshot(path=str(out))
        print(f"[demo] screenshot saved: {out}")

        # Leave it open so you can watch / try typing in another app
        # to verify focus is not stolen.
        hold = 30
        print(f"[demo] holding browser open for {hold}s — try typing in")
        print("       another app on your main monitor and confirm your")
        print("       keystrokes are NOT stolen by this browser.")
        time.sleep(hold)

        browser.close()
        print("[demo] done")


if __name__ == "__main__":
    main()

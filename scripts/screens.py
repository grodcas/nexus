#!/usr/bin/env python3
"""
screens.py — Nexus screen and window management primitives (macOS).

Wraps the AppleScript / system_profiler calls that we validated work on
2026-04-09. Use this module from any other Nexus component that needs to:

  - enumerate the connected displays and their bounds
  - find a window by app name or title
  - move and resize a window (including across displays)

Validated environment:
    macOS 15 (Darwin 24.6.0), Apple Silicon
    Accessibility permission GRANTED to the parent terminal/Claude process
    (System Settings → Privacy & Security → Accessibility).
    Without that permission, every `set position`/`set size` call returns
    `osascript is not allowed assistive access. (-1728)`.

What we tried that did NOT work, recorded so we don't try again:
    osascript -e 'tell application "System Events" to tell every desktop \\
                  to get {name, size}'
    → System Events got an error: Can't get size of every desktop. (-1728)
    The "desktop" object in System Events is not multi-display aware.
    Use system_profiler SPDisplaysDataType for display enumeration instead.

What DID work (the raw commands, in case this module ever rots):

    # 1. Enumerate displays
    system_profiler SPDisplaysDataType

    # 2. Get the full virtual desktop bounds (spans all displays)
    osascript -e 'tell application "Finder" to get bounds of window of desktop'
    # → e.g. "0, 0, 3390, 1080"

    # 3. Read a window's geometry by app process name
    osascript -e 'tell application "System Events" \\
        to tell (first process whose name contains "scrcpy") \\
        to get {name, position, size} of windows'
    # → "Nexus-Tablet, 511, 72, 448, 783"

    # 4. Move a window
    osascript -e 'tell application "System Events" \\
        to tell (first process whose name contains "scrcpy") \\
        to set position of window 1 to {1500, 50}'

    # 5. Resize a window
    osascript -e 'tell application "System Events" \\
        to tell (first process whose name contains "scrcpy") \\
        to set size of window 1 to {1024, 768}'

    # 6. Get the frontmost (focused) app — used to refocus after launching
    #    a window that would otherwise steal focus
    osascript -e 'tell application "System Events" \\
        to get name of first process whose frontmost is true'

    # 7. Refocus a previously-frontmost app by name
    osascript -e 'tell application "System Events" \\
        to set frontmost of first process whose name is "iTerm2" to true'
"""

from __future__ import annotations

import re
import subprocess
from dataclasses import dataclass


# =============================================================================
# Known process names — gotchas worth recording
# =============================================================================

# Playwright's headed Chromium runs as "Google Chrome for Testing", NOT
# "Chromium". Always use this substring (or the constant below) to find
# the Playwright browser window via AppleScript.
BROWSER_PROCESS = "chrome"


# =============================================================================
# Low-level helpers
# =============================================================================

def _osa(script: str) -> str:
    """Run an AppleScript snippet and return its stdout (stripped)."""
    result = subprocess.run(
        ["osascript", "-e", script],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        raise RuntimeError(
            f"osascript failed: {result.stderr.strip()}\nscript: {script}"
        )
    return result.stdout.strip()


# =============================================================================
# Display enumeration
# =============================================================================

@dataclass
class Display:
    name: str          # e.g. "Color LCD" or "R27qe"
    width: int         # logical pixels (UI looks like ...)
    height: int
    is_main: bool

    def __repr__(self) -> str:
        m = " (main)" if self.is_main else ""
        return f"<Display {self.name} {self.width}x{self.height}{m}>"


def list_displays() -> list[Display]:
    """
    Return all connected displays.

    Uses `system_profiler SPDisplaysDataType` because System Events does
    not expose multi-display info reliably.

    Note: width/height are the *logical* (UI) pixels — what AppleScript
    coordinates use — not the physical pixel count of a Retina panel.
    """
    raw = subprocess.run(
        ["system_profiler", "SPDisplaysDataType"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout

    displays: list[Display] = []
    current_name: str | None = None
    current_width: int | None = None
    current_height: int | None = None
    current_main = False

    def flush() -> None:
        nonlocal current_name, current_width, current_height, current_main
        if current_name and current_width and current_height:
            displays.append(
                Display(current_name, current_width, current_height, current_main)
            )
        current_name = None
        current_width = None
        current_height = None
        current_main = False

    for line in raw.splitlines():
        stripped = line.strip()
        # A display block starts with "<name>:" indented under "Displays:"
        # and is followed by indented "Resolution:", "Main Display:" lines.
        if stripped.endswith(":") and not stripped.startswith(("Resolution",
                                                                "Main Display",
                                                                "UI Looks like",
                                                                "Mirror",
                                                                "Online",
                                                                "Connection",
                                                                "Rotation",
                                                                "Display Type",
                                                                "Automatically")):
            # New display block
            flush()
            current_name = stripped.rstrip(":")
            continue

        # "UI Looks like: 1920 x 1080 @ 60.00Hz" — preferred over physical
        m = re.search(r"UI Looks like:\s*(\d+)\s*x\s*(\d+)", stripped)
        if m:
            current_width = int(m.group(1))
            current_height = int(m.group(2))
            continue

        # "Resolution: 2560 x 1664 Retina" — fallback if no UI Looks like
        if current_width is None:
            m = re.search(r"Resolution:\s*(\d+)\s*x\s*(\d+)", stripped)
            if m:
                # Retina built-ins report physical pixels here; halve them
                # to approximate logical points.
                w = int(m.group(1))
                h = int(m.group(2))
                if "Retina" in stripped:
                    w //= 2
                    h //= 2
                current_width = w
                current_height = h
                continue

        if "Main Display: Yes" in stripped:
            current_main = True

    flush()
    return displays


def virtual_desktop_bounds() -> tuple[int, int, int, int]:
    """
    Return the bounds of the full virtual desktop spanning all displays
    as (x1, y1, x2, y2). This is what AppleScript coordinates live in.
    """
    raw = _osa('tell application "Finder" to get bounds of window of desktop')
    nums = [int(x.strip()) for x in raw.split(",")]
    return (nums[0], nums[1], nums[2], nums[3])


# =============================================================================
# Window control
# =============================================================================

@dataclass
class Window:
    process: str       # e.g. "scrcpy", "Chromium"
    title: str         # e.g. "Nexus-Tablet"
    x: int
    y: int
    width: int
    height: int


def find_window(process_substr: str) -> Window | None:
    """
    Find the first window of the first process whose name contains the
    given substring. Returns None if not found.
    """
    script = (
        f'tell application "System Events" to tell '
        f'(first process whose name contains "{process_substr}") '
        f'to get {{name, position, size}} of window 1'
    )
    try:
        raw = _osa(script)
    except RuntimeError:
        return None

    # Output looks like: "Nexus-Tablet, 511, 72, 448, 783"
    parts = [p.strip() for p in raw.split(",")]
    if len(parts) < 5:
        return None
    title = parts[0]
    x, y, w, h = (int(p) for p in parts[1:5])
    return Window(process=process_substr, title=title, x=x, y=y, width=w, height=h)


def move_window(process_substr: str, x: int, y: int) -> None:
    """Move the first window of the matching process to (x, y)."""
    _osa(
        f'tell application "System Events" to tell '
        f'(first process whose name contains "{process_substr}") '
        f'to set position of window 1 to {{{x}, {y}}}'
    )


def resize_window(process_substr: str, width: int, height: int) -> None:
    """Resize the first window of the matching process."""
    _osa(
        f'tell application "System Events" to tell '
        f'(first process whose name contains "{process_substr}") '
        f'to set size of window 1 to {{{width}, {height}}}'
    )


def place_window(
    process_substr: str,
    x: int,
    y: int,
    width: int,
    height: int,
) -> None:
    """Move + resize in one call."""
    move_window(process_substr, x, y)
    resize_window(process_substr, width, height)


# =============================================================================
# Focus management — keep the user's typing destination intact when Nexus
# launches windows of its own
# =============================================================================

def get_frontmost_app() -> str | None:
    """Return the name of the currently-focused application, or None."""
    try:
        return _osa(
            'tell application "System Events" to '
            'get name of first process whose frontmost is true'
        )
    except RuntimeError:
        return None


def focus_app(app_name: str) -> None:
    """
    Bring the named application to the front. Used to refocus the user's
    previous app after Nexus launches a window that would otherwise steal
    focus.
    """
    _osa(
        f'tell application "System Events" to '
        f'set frontmost of first process whose name is "{app_name}" to true'
    )


# =============================================================================
# Demo / smoke test
# =============================================================================

if __name__ == "__main__":
    print("Displays:")
    for d in list_displays():
        print(f"  {d}")
    print()
    print(f"Virtual desktop bounds: {virtual_desktop_bounds()}")

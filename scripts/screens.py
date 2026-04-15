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

# PyObjC — used for hang-free window enumeration and frontmost-app
# lookup. Importing Quartz / AppKit is cheap (~30ms) and the resulting
# calls are ~1-10ms against the WindowServer, with no target-process
# involvement, so they cannot hang on unresponsive apps.
#
# The AppleScript paths below are kept for geometry operations
# (move/resize/focus/raise) — those do need to reach the target
# process, and already have a 5s osascript timeout to cap the damage.
# Before any geometry op we gate on _process_exists() so the whole
# class of "talk to a process that isn't there" goes away.
from Quartz import (
    CGWindowListCopyWindowInfo,
    kCGWindowListOptionOnScreenOnly,
    kCGWindowListOptionAll,
    kCGWindowListExcludeDesktopElements,
    kCGNullWindowID,
)
from AppKit import NSWorkspace

# NOTE on macOS permissions:
#
#   - PROCESS ENUMERATION (owner names, bounds, PIDs) is always
#     readable — no permission required.
#   - WINDOW TITLES are gated behind Screen Recording permission since
#     macOS 10.15. Without it, kCGWindowName is empty for apps other
#     than the calling process. _process_exists and geometry ops still
#     work; list_windows returns entries with empty titles until the
#     user grants permission to the parent terminal process.
#     (System Settings → Privacy & Security → Screen Recording)


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

_OSA_TIMEOUT_S = 5.0  # hard ceiling; an unresponsive target process can hang osascript indefinitely


def _osa(script: str, timeout: float = _OSA_TIMEOUT_S) -> str:
    """Run an AppleScript snippet and return its stdout (stripped).

    Always bounded by a subprocess timeout. If the script hangs (a common
    failure mode on macOS when iterating processes and one of them is
    unresponsive — e.g. Electron apps mid-update), we kill osascript and
    raise RuntimeError so callers degrade gracefully instead of blocking
    the whole voice loop for minutes.
    """
    try:
        result = subprocess.run(
            ["osascript", "-e", script],
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        raise RuntimeError(
            f"osascript timed out after {timeout}s (stuck target process?)"
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

    Fast path: read from the WindowServer snapshot (list_windows) —
    which is ~1ms and cannot hang. Falls back to AppleScript only if
    the snapshot has no match but the process is still "around"
    somehow (rare).
    """
    needle = process_substr.lower()
    for w in list_windows():
        if needle in w.process.lower():
            return w
    return None


def move_window(process_substr: str, x: int, y: int) -> None:
    """Move the first window of the matching process to (x, y).

    Gated on _process_exists(): if the WindowServer snapshot does not
    contain the target, we skip the AppleScript call entirely. That
    eliminates the "talk to a dead / unresponsive process and hang"
    failure class which dominated the Plan 1A baseline.
    """
    if not _process_exists(process_substr):
        raise RuntimeError(f"process '{process_substr}' not found in window list")
    _osa(
        f'tell application "System Events" to tell '
        f'(first process whose name contains "{process_substr}") '
        f'to set position of window 1 to {{{x}, {y}}}'
    )


def resize_window(process_substr: str, width: int, height: int) -> None:
    """Resize the first window of the matching process.

    Gated on _process_exists() — see move_window().
    """
    if not _process_exists(process_substr):
        raise RuntimeError(f"process '{process_substr}' not found in window list")
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
    """Return the name of the currently-focused application, or None.

    Uses NSWorkspace — a CoreFoundation-level API. No subprocess, no
    Apple Events, ~0.1ms, cannot hang.
    """
    try:
        app = NSWorkspace.sharedWorkspace().frontmostApplication()
        if app is None:
            return None
        return str(app.localizedName())
    except Exception:
        return None


def _process_exists(name_substr: str) -> bool:
    """
    Return True if a currently-running (and windowed) process has a
    name containing `name_substr` (case-insensitive).

    Uses CGWindowListOptionAll so we also catch minimized / hidden
    apps (a minimized Chrome window should still pass the gate so
    that "move chrome left" works). ~1ms, cannot hang.

    Used as a pre-flight gate on AppleScript geometry ops (move,
    resize, focus, raise) so we never issue an Apple Event to a
    process that isn't there — which is the failure mode that hung
    the whole voice loop for 60-120s in the Plan 1A baseline.
    """
    needle = name_substr.strip().lower()
    if not needle:
        return False
    for w in _cg_window_snapshot(include_offscreen=True):
        owner = (w.get("kCGWindowOwnerName") or "").lower()
        if needle in owner or owner in needle:
            return True
    return False


def _cg_window_snapshot(include_offscreen: bool = False) -> list[dict]:
    """Return the current WindowServer window list as a list of dicts.

    include_offscreen=False: only on-screen windows (default).
        Matches what a user would call "visible windows."
    include_offscreen=True: every window the WindowServer knows about,
        including minimized and hidden. Used by _process_exists so
        that gating is generous — a minimized app should still pass.
    """
    if include_offscreen:
        opts = kCGWindowListOptionAll | kCGWindowListExcludeDesktopElements
    else:
        opts = kCGWindowListOptionOnScreenOnly | kCGWindowListExcludeDesktopElements
    try:
        return list(CGWindowListCopyWindowInfo(opts, kCGNullWindowID) or [])
    except Exception:
        return []


def raise_window(process_substr: str) -> None:
    """
    Bring window 1 of the matching process to the front WITHOUT activating
    its application — preserves the user's current keyboard focus.
    Uses the AXRaise accessibility action. Silently skips if the
    target isn't in the current window list.
    """
    if not _process_exists(process_substr):
        return
    try:
        _osa(
            f'tell application "System Events" to tell '
            f'(first process whose name contains "{process_substr}") to '
            f'perform action "AXRaise" of window 1'
        )
    except RuntimeError:
        pass


def focus_app(app_name: str) -> None:
    """
    Bring the named application to the front. Used to refocus the user's
    previous app after Nexus launches a window that would otherwise steal
    focus. Gated on _process_exists() — silently no-ops if the target
    isn't visible, rather than wedging osascript on a dead app.
    """
    if not _process_exists(app_name):
        return
    try:
        _osa(
            f'tell application "System Events" to '
            f'set frontmost of first process whose name is "{app_name}" to true'
        )
    except RuntimeError:
        pass


# =============================================================================
# List all windows
# =============================================================================

def list_windows() -> list[Window]:
    """
    Return all visible windows across all applications.

    Uses CGWindowListCopyWindowInfo — a direct WindowServer query that
    returns every on-screen window's bounds, owner process, and title
    in a single ~1-10ms call. **No Apple Events, no target-process
    involvement, no hangs.** This is the same API yabai / Rectangle /
    Alfred use.

    The previous AppleScript implementation iterated `every process
    whose visible is true` and sent each one a `get windows` Apple
    Event — which blocked for ~60s per unresponsive target. Plan 1A
    baseline measured 120s per call. The fix is to not talk to the
    target processes at all: the WindowServer already has what we
    need.

    Filters applied:
      - only on-screen windows (skips minimized / off-screen)
      - excludes desktop elements (Finder wallpapers)
      - drops entries with empty titles (Nexus's old rule)
      - drops the macOS menu bar and Dock pseudo-windows
    """
    out: list[Window] = []
    seen_processes: set[str] = set()
    for w in _cg_window_snapshot():
        owner = w.get("kCGWindowOwnerName") or ""
        if not owner or owner in (
            "Window Server", "Dock", "SystemUIServer",
            "Control Center", "Wallpaper", "TextInputMenuAgent",
            "Spotlight", "Notification Center",
        ):
            continue
        title = str(w.get("kCGWindowName") or "")  # may be empty without Screen Recording perm
        b = w.get("kCGWindowBounds") or {}
        try:
            x = int(b.get("X", 0))
            y = int(b.get("Y", 0))
            width = int(b.get("Width", 0))
            height = int(b.get("Height", 0))
        except (TypeError, ValueError):
            continue
        if width < 50 or height < 50:
            continue
        # De-dup by (process, title) so N helper windows of the same
        # app don't spam the list. A process with empty-title windows
        # is still surfaced once.
        key = f"{owner}|{title}"
        if key in seen_processes:
            continue
        seen_processes.add(key)
        out.append(Window(
            process=owner,
            title=title,
            x=x, y=y, width=width, height=height,
        ))
    return out


# =============================================================================
# Close / minimize / maximize
# =============================================================================

def close_window(process_substr: str, window_title: str | None = None) -> bool:
    """
    Close a window. If window_title is given, close the window matching
    that title; otherwise close window 1 of the matching process.
    Returns True on success.
    """
    if not _process_exists(process_substr):
        return False
    if window_title:
        script = (
            f'tell application "System Events" to tell '
            f'(first process whose name contains "{process_substr}") to '
            f'click button 1 of (first window whose name contains "{window_title}")'
        )
    else:
        script = (
            f'tell application "System Events" to tell '
            f'(first process whose name contains "{process_substr}") to '
            f'click button 1 of window 1'
        )
    try:
        _osa(script)
        return True
    except RuntimeError:
        return False


def minimize_window(process_substr: str) -> bool:
    """Minimize window 1 of the matching process."""
    if not _process_exists(process_substr):
        return False
    script = (
        f'tell application "System Events" to tell '
        f'(first process whose name contains "{process_substr}") to '
        f'set value of attribute "AXMinimized" of window 1 to true'
    )
    try:
        _osa(script)
        return True
    except RuntimeError:
        return False


def maximize_window(process_substr: str) -> None:
    """
    Expand window to fill the display it's currently on.
    Determines which display the window is on, then resizes to fill it.
    """
    win = find_window(process_substr)
    if not win:
        raise RuntimeError(f"No window found for '{process_substr}'")

    displays = list_displays()
    if not displays:
        raise RuntimeError("No displays found")

    # Determine which display the window center is on
    bounds = virtual_desktop_bounds()
    win_center_x = win.x + win.width // 2

    # Simple heuristic: main display starts at x=0
    main = [d for d in displays if d.is_main]
    secondary = [d for d in displays if not d.is_main]

    if secondary and main:
        main_w = main[0].width
        if win_center_x >= main_w:
            # Window is on secondary display
            d = secondary[0]
            place_window(process_substr, main_w, 0, d.width, d.height)
        else:
            d = main[0]
            place_window(process_substr, 0, 25, d.width, d.height - 25)
    elif main:
        d = main[0]
        place_window(process_substr, 0, 25, d.width, d.height - 25)


# =============================================================================
# Preset positions — snap to halves / quarters of a display
# =============================================================================

def snap_window(process_substr: str, position: str, screen: str = "current") -> None:
    """
    Snap a window to a preset position on a display.

    position: "left", "right", "top-left", "top-right",
              "bottom-left", "bottom-right", "center", "full"
    screen: "current", "main", "secondary", or "other"
            "other" moves the window to whichever display it's NOT on.
    """
    displays = list_displays()
    if not displays:
        raise RuntimeError("No displays found")

    main = [d for d in displays if d.is_main]
    secondary = [d for d in displays if not d.is_main]
    main_d = main[0] if main else displays[0]
    sec_d = secondary[0] if secondary else None

    # Resolve target display
    if screen == "other":
        win = find_window(process_substr)
        if win and sec_d:
            win_center_x = win.x + win.width // 2
            target_d = sec_d if win_center_x < main_d.width else main_d
            x_offset = main_d.width if target_d is sec_d else 0
        else:
            target_d = main_d
            x_offset = 0
    elif screen == "secondary" and sec_d:
        target_d = sec_d
        x_offset = main_d.width
    else:
        # "main" or "current" — use main
        if screen == "current":
            win = find_window(process_substr)
            if win and sec_d and win.x >= main_d.width:
                target_d = sec_d
                x_offset = main_d.width
            else:
                target_d = main_d
                x_offset = 0
        else:
            target_d = main_d
            x_offset = 0

    dw = target_d.width
    dh = target_d.height
    menu_bar = 25  # macOS menu bar height

    positions = {
        "left":         (x_offset, menu_bar, dw // 2, dh - menu_bar),
        "right":        (x_offset + dw // 2, menu_bar, dw // 2, dh - menu_bar),
        "top-left":     (x_offset, menu_bar, dw // 2, (dh - menu_bar) // 2),
        "top-right":    (x_offset + dw // 2, menu_bar, dw // 2, (dh - menu_bar) // 2),
        "bottom-left":  (x_offset, menu_bar + (dh - menu_bar) // 2, dw // 2, (dh - menu_bar) // 2),
        "bottom-right": (x_offset + dw // 2, menu_bar + (dh - menu_bar) // 2, dw // 2, (dh - menu_bar) // 2),
        "center":       (x_offset + dw // 4, menu_bar + (dh - menu_bar) // 4, dw // 2, (dh - menu_bar) // 2),
        "full":         (x_offset, menu_bar, dw, dh - menu_bar),
    }

    if position not in positions:
        raise ValueError(f"Unknown position '{position}'. Use: {', '.join(positions.keys())}")

    x, y, w, h = positions[position]
    place_window(process_substr, x, y, w, h)


# =============================================================================
# Demo / smoke test
# =============================================================================

if __name__ == "__main__":
    print("Displays:")
    for d in list_displays():
        print(f"  {d}")
    print()
    print(f"Virtual desktop bounds: {virtual_desktop_bounds()}")
    print()
    print("Visible windows:")
    for w in list_windows():
        print(f"  [{w.process}] {w.title} — pos=({w.x},{w.y}) size={w.width}x{w.height}")

# NEXUS — Screen Management & Browser Navigation Spec

> Status: foundation validated 2026-04-09, ready to be wrapped as tools.
> See also: [TABLET_SPEC.md](TABLET_SPEC.md), [TOOLS_SPEC.md](TOOLS_SPEC.md), [VISION_V2.md](VISION_V2.md).

This document captures the design and the validated state of the Nexus
**hands** that operate the Mac itself: window management on macOS and
browser automation via Playwright. It exists so the next session can pick
up without re-discovering anything.

---

## What we proved on 2026-04-09

| Capability | Status | Evidence |
|---|---|---|
| Enumerate connected displays | ✓ | `scripts/screens.py::list_displays()` parses `system_profiler SPDisplaysDataType`. Two displays detected: built-in Color LCD (1280×832 logical, main), R27qe (1920×1080 logical). |
| Get virtual desktop bounds | ✓ | `(0, 0, 3390, 1080)` via `tell application "Finder" to get bounds of window of desktop`. |
| Find a window by app name | ✓ | `scripts/screens.py::find_window()` via `System Events`. |
| Move a window across displays | ✓ | scrcpy window moved from (511,72) to (1500,50). |
| Resize a window | ✓ | Honored width, height clamped to display usable area. |
| Move/resize **without stealing focus** | ✓ | Validated twice: scrcpy and Playwright moves both ran while iTerm2 stayed focused. **Key fact: AppleScript geometry ops do not call `activate`.** |
| Launch a headed Playwright Chromium | ✓ | `scripts/playwright_demo.py`. |
| Find the Playwright window via AppleScript | ✓ | Process name is **`Google Chrome for Testing`** — substring `"chrome"` matches. Recorded as `screens.BROWSER_PROCESS`. |
| Real navigation + image-result render | ✓ | DuckDuckGo image search, screenshot saved. |
| Focus preservation across launch | ✓ | Capture `get_frontmost_app()` before launch → refocus immediately after window appears → refocus again after placement. Confirmed by user: typing in iTerm2 was not redirected to the new browser. |
| Screenshot from Playwright page | ✓ | Validates the screen-reading primitive too. |

## What we proved does NOT work

- `tell application "System Events" to get every desktop` — AppleScript's
  `desktop` object is not multi-display aware. Use `system_profiler` for
  enumeration, `Finder` for the virtual desktop bounds.
- Without Accessibility permission granted to the parent process,
  `set position` / `set size` return `osascript is not allowed assistive
  access. (-1728)`. **This permission is the #1 prerequisite of the entire
  screen-management layer.** Already granted in this environment.

---

## Files added today

| File | Purpose |
|---|---|
| `scripts/screens.py` | macOS screen + window primitives. Wraps the validated AppleScript chain as Python. Documents the raw commands in a comment block for resilience. Exports `list_displays`, `virtual_desktop_bounds`, `find_window`, `move_window`, `resize_window`, `place_window`, `get_frontmost_app`, `focus_app`, and the `BROWSER_PROCESS` constant. |
| `scripts/playwright_demo.py` | First-contact demo: launches headed Chromium, navigates DuckDuckGo Images, screenshots, positions the window via `screens.py`, and preserves focus across the launch. The reference implementation of every primitive we've validated. |
| `TABLET_SPEC.md` | Separate spec for the tablet UI (parked, see file). |

---

## Key facts that are easy to forget

1. **Playwright's headed Chromium runs as `Google Chrome for Testing`,
   not `Chromium`.** Use `screens.BROWSER_PROCESS` everywhere.
2. **Window geometry ops in AppleScript do not steal focus.** This was the
   make-or-break test for parallel operation. It works.
3. **Launching an app *does* steal focus.** Mitigation: capture
   `get_frontmost_app()` before launch, call `focus_app()` immediately
   after the new window appears (and again after any subsequent geometry
   ops, to be safe). ~200 ms visible flicker, no input redirection.
4. **macOS may clamp window height** to fit the usable area of the target
   display. Resize requests above the display's logical height return a
   smaller value. Future `place_window` should clamp to the target
   display's bounds defensively.
5. **Built-in Retina displays report physical pixels** in
   `system_profiler`. `screens.py::list_displays()` halves them when it
   sees "Retina" so the returned values match AppleScript coordinates.
6. **Browser vs. search engine are orthogonal.** Chromium = the program;
   DuckDuckGo / Google = the website it visits. They're not alternatives.

---

## The next layer: BrowserAgent as a tool

We discussed (but did not yet implement) wrapping the demo into a reusable
tool that Gemini and Claude can call. The decision and contract:

### Architectural decision: **persistent browser**

One long-running Chromium process owned by a `BrowserAgent` class:

- Launched once at Nexus startup (or lazily on first tool call).
- Uses `pw.chromium.launch_persistent_context(user_data_dir=…)` so a real
  profile (cookies, login state) survives across runs and sessions.
- Window is visible by default on the laptop screen (the "ghost browsing"
  experience: the user can watch Nexus operate the browser).
- One hotkey (planned: `⌃⌥B`) toggles offscreen-hide / restore so the
  user can hide it temporarily without paying the 1.5 s relaunch cost.
- Closing only on Nexus shutdown.

Open lifecycle question is **closed**: persistent wins because launch is
slow, captchas need profile state, and persistent makes navigation feel
instant after the initial 1.5 s.

### Profile location

`~/.nexus/playwright_profile/` (alongside the existing `~/.nexus/`
worktrees, where everything Nexus-stateful lives).

### Tool contracts

Drafted. To be added to `TOOLS_SPEC.md` once implemented.

```
Name:        search_images
Description: Search the web for images matching a query and return the
             top results. The user can see the results in the Nexus
             browser on their laptop screen while the LLM summarizes them.
Input:       { query: string, limit?: int = 10,
               engine?: "duckduckgo" | "google" = "duckduckgo" }
Output:      { results: [{ title, image_url, source_url, thumbnail_b64? }],
               screenshot_path: string, browser_url: string }
Scope:       Read-only. Navigates the persistent Nexus browser. Leaves
             the browser on the results page so the user can keep
             browsing visually.
```

```
Name:        search_web
Description: Search the web for text results matching a query.
Input:       { query: string, limit?: int = 10, engine?: ... }
Output:      { results: [{ title, url, snippet }],
               screenshot_path: string, browser_url: string }
```

```
Name:        browser_navigate
Description: Navigate the Nexus browser to a specific URL and return
             the page contents (text + screenshot).
Input:       { url: string, wait_for?: string }
Output:      { url, title, text, screenshot_path }
```

These three cover ~80% of "Nexus browses the web" use cases. Click and
form-fill come later.

### Code layout (planned, not yet built)

```
nexus/
  tools/
    __init__.py
    browser.py          ← BrowserAgent class, the three tools above
  scripts/
    screens.py          ← exists
    playwright_demo.py  ← exists
    browser_cli.py      ← planned: terminal driver for BrowserAgent
                          (no Gemini, for cheap iteration)
voice/
  jarvis.py             ← will register search_images as a Gemini
                          function call (one new register_function)
```

### Search engine default

**DuckDuckGo** for the default `engine` parameter:
- Almost never throws captchas at automated browsers
- No EU consent dialog
- More stable HTML for scraping
- Trade-off: slightly worse image-result quality than Google

When the user has a logged-in Google profile (via persistent context),
`engine="google"` is viable as an explicit opt-in.

---

## Concrete next steps for tomorrow

In order, each independently runnable:

1. **Build `nexus/tools/browser.py`** with a minimal `BrowserAgent`
   exposing `search_images(query)` and `navigate(url)`. Uses
   `launch_persistent_context()`, places the window via `screens.py` once,
   refocuses the previous app once. ~150 lines.
2. **Build `scripts/browser_cli.py`** so we can drive the agent from the
   terminal: `python scripts/browser_cli.py search "drone lidar"`. This
   is where we iterate on tool behavior cheaply, before any LLM is
   involved.
3. **Wire `search_images` into `voice/jarvis.py`** as a Gemini function
   call. End state: "Jarvis, search for images of drone lidar sensors" →
   browser visibly updates on the laptop screen → Gemini summarizes the
   results in voice.
4. **Add the `⌃⌥B` hide/show hotkey.** Picks a hotkey daemon (skhd or a
   small `pynput` script — decision deferred from TABLET_SPEC.md).
5. **Defensive `place_window`** that clamps width/height to the target
   display's usable bounds (the height-clamping issue we observed).
6. **Investigate `engine="google"` with the persistent profile** to dodge
   captchas via login state.

---

## Open questions deferred

- **Hotkey daemon choice** (skhd vs Hammerspoon vs pynput). Same
  unanswered question as in TABLET_SPEC.md — should be decided once for
  both consumers.
- **Tab management semantics.** When a future tool says "open the PR
  diff in a new tab", do we return all tab info, or only the active tab?
  Need to define `BrowserAgent.tabs()`, `new_tab()`, `switch_tab()`.
- **What does `search_images` return for `thumbnail_b64`** — should we
  always inline base64 thumbnails for the LLM to display, or only on
  request? Inlining is heavier but lets Claude/Gemini show the user
  images directly in the conversation.

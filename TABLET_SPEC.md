# NEXUS — Tablet Spec

> The tablet is Nexus's visible presence: a glanceable operations console mounted
> above the main monitor. It shows what Nexus is doing, what you need to act on,
> and proof that Nexus is working correctly. It is not a notification surface and
> not a window into external systems.

---

## Hardware & physical setup

| | |
|---|---|
| Device | Samsung Galaxy Tab A9 (SM-X110) |
| OS | Android 14 |
| Resolution | 1340 × 800 (landscape) |
| Density | 213 dpi |
| Connection | USB to Mac (ADB authorized) |
| Position | Above the 30" main monitor, ~50 cm from user |
| Orientation | Landscape (locked) |

The desk layout the tablet plugs into:

```
┌───────────┐  ┌──────────────────┐
│  laptop   │  │   30" main       │  ← user works here
│  13.5"    │  │   monitor        │
│  (Nexus   │  │   (user)         │
│   panel)  │  │                  │
└───────────┘  └──────────────────┘
                       │
                  ┌─────────┐
                  │ tablet  │     ← above the main monitor,
                  │  8"     │       in peripheral vision
                  └─────────┘
```

The laptop on the left is Nexus's primary working surface (where it can render
files, browser, drafts). The tablet above the main monitor is Nexus's
*ambient* surface — what you glance at without leaving your work.

---

## Constraints that drive every design decision

1. **Distance × size = low information density.** 8" at 50 cm ≈ 16" at 100 cm.
   Anything smaller than ~24pt is unreadable at a glance. The tablet cannot
   be a "small monitor that shows email."
2. **Non-reachable by hand.** Mounted above the monitor — touching it is
   uncomfortable. Touch is **not** the interaction model.
3. **Peripheral vision matters.** The eye catches *changes* (motion, color
   shift, a card sliding in) without losing focus on the main monitor. This
   is the tablet's superpower over the laptop screen.
4. **Display-first, interaction-rare.** Most of the time the tablet shows
   things and is not interacted with. Interaction is by Mac keyboard hotkeys
   for the rare moments it's needed (approve, scroll, switch focus).

---

## Purpose: three jobs only

1. **Tasks** — what Nexus is running in parallel right now, with progress
2. **Reminders** — what you need to do or decide (Nexus's judgments, not raw events)
3. **Log** — what Nexus has done under the hood, so you can trust it's working

Hard exclusions: no email feed, no calls, no calendar feed, no chat. Those
live where they belong (in their apps, on the laptop, or in a briefing).
The tablet is a Nexus operations console, not a window into the outside world.

---

## Layout

**Three blocks, always on screen, landscape, with one focused.**

```
┌─────────────────────────┬────────────────┐
│                         │                │
│                         │   block B      │
│                         │   (secondary)  │
│       FOCUSED           │                │
│       block             ├────────────────┤
│       (left half)       │                │
│                         │   block C      │
│                         │   (secondary)  │
│                         │                │
└─────────────────────────┴────────────────┘
        50% width                50% width
                            split vertically
```

- The **focused block** takes the entire left half (50% width × full height).
- The other two blocks share the right half, split vertically (each 50% × 50%).
- The focused block is **always on the left**. The two secondary blocks
  always appear in the same fixed positions on the right (top and bottom).
  Rotating focus does not reorder the secondary blocks — they stay put.
- Manual focus selection via hotkey (`⌃⌥1` / `⌃⌥2` / `⌃⌥3`).
- Auto-promote: when something demands attention (a task fails, an approval
  comes in), the relevant panel auto-focuses itself.

**Block order (fixed, top-to-bottom in canonical state):**
1. Tasks
2. Reminders
3. Log

When Tasks is focused: Reminders top-right, Log bottom-right.
When Reminders is focused: Tasks top-right, Log bottom-right.
When Log is focused: Tasks top-right, Reminders bottom-right.

The two non-focused blocks render their content compressed (smaller font,
fewer rows, summary instead of detail) but stay readable enough to glance at.

---

## Block contents

### Tasks block

Each row = one Nexus background task. Shows:
- State icon (`⟳` running, `✓` done, `⚠` failed, `⏸` waiting for approval)
- One-line description
- Progress (numeric or bar)
- Elapsed time
- Current sub-step ("reading source 4/5", "summarizing", "waiting for OK")

Sorting: items needing attention at the top. Completed tasks linger ~30 s
as `✓ done` then drop off. Failed tasks stay until acknowledged.

Think of it as `htop` for Nexus.

### Reminders block

**Things Nexus has decided you should know about** — its judgments, not
raw events. Examples:

- "ISR §3 still has 2 unresolved comments — finish before tomorrow's meeting"
- "Maria's reply has been sitting for 2 days"
- "Build is failing on master — last passing commit was Tuesday"
- "You haven't taken a break in 2 h"

Plus: **manual reminders you set** via voice or hotkey ("remind me to call
Carlos at 3pm"). Both flow into the same list.

### Log block

Append-only chronological feed of what Nexus has done: tools called, files
read, web pages fetched, decisions made, errors. One line per action.

```
09:42:15  search_worktree("LiDAR")
09:42:15  → 4 matches in web_research/
09:42:18  fetch_url(livox.com/mid-70)
09:42:21  → 12.4 KB
09:42:24  summarizing 4 sources...
09:42:31  ▶ done, drafting comparison table
09:43:02  ⚠ approval needed: send email to Maria
```

This is also how we catch silent failures while *building* Nexus — the log
pays for itself before there are any users.

Verbosity toggle (`⌃⌥0`): verbose (every tool call) for development, quiet
(only "interesting" events) once Nexus is stable.

---

## Interaction: keyboard hotkeys only

No touch. The Mac keyboard drives everything. Hotkeys are global (work from
any active app on the Mac without focus-stealing).

| Hotkey | Action |
|---|---|
| `⌃⌥1` | Focus the **Tasks** block |
| `⌃⌥2` | Focus the **Reminders** block |
| `⌃⌥3` | Focus the **Log** block |
| `⌃⌥↑` / `⌃⌥↓` | Scroll the focused block |
| `⌃⌥↩` | Approve the topmost pending item |
| `⌃⌥⌫` | Reject / dismiss the topmost pending item |
| `⌃⌥0` | Toggle log verbosity (verbose ↔ quiet) |

The "topmost pending item" is always visually highlighted on the tablet so
the user has a clear signal of what `⌃⌥↩` will act on, without having to
look closely.

You never need to look at the tablet to act. Hear/glance/act flow:
1. Nexus says "draft ready" (voice) or you peripherally see the highlighted
   waiting card on the tablet.
2. You hit `⌃⌥↩` without taking your eyes off the main monitor.
3. The action runs; the highlight moves to the next pending item if any.

---

## Architecture

```
┌──────────── MAC ────────────────┐         ┌──── TABLET ────┐
│                                 │         │                │
│  Nexus core (Python)            │         │  Nexus Tablet  │
│      │                          │         │  (Android app) │
│  ┌───▼──────────────────┐       │         │                │
│  │ Tablet adapter       │───────┼──USB────┤  WebView,      │
│  │ - HTTP server        │  adb  │ reverse │  full-screen,  │
│  │   on localhost:8080  │       │         │  always-on,    │
│  │ - sends state JSON   │       │         │  no chrome     │
│  │ - serves HTML/CSS    │       │         │                │
│  │ - receives events    │◄──────┼─────────┤                │
│  └──────────────────────┘       │         │                │
│                                 │         │                │
│  Hotkey daemon (skhd / etc.)    │         │                │
│  → POSTs events to adapter      │         │                │
└─────────────────────────────────┘         └────────────────┘
```

**Transport: `adb reverse tcp:8080 tcp:8080`** — exposes the Mac's
localhost:8080 to the tablet over the existing USB cable. No Wi-Fi, no
pairing, no IP config. Disconnect cable → tablet shows "disconnected".
Plug back in → resumes.

**Mac side:** small FastAPI (or similar) server that Nexus pushes states
to. Endpoints:
- `GET /ui` — returns the HTML page the WebView loads
- `GET /state` — current state JSON, polled by the page (or pushed via SSE)
- `POST /event` — touch/button events from the WebView (rarely used since
  interaction is via Mac hotkeys, but kept for future)
- `POST /state` — Nexus core writes a new state here

**Tablet side:** single-Activity Android app, fullscreen, screen-stays-on
flag, no other UI. The Activity hosts a `WebView` pointed at
`http://localhost:8080/ui`. **All UI is HTML/CSS rendered server-side from
Python.** This is the key win: the APK is built once, installed once, and
never touched again. Every layout change happens in Python on the Mac.

**Hotkeys:** registered via a global hotkey daemon on the Mac (e.g. `skhd`,
or a small Python daemon using `pynput`). Each hotkey just POSTs to the
adapter, which translates it into a state change and pushes the new state
to the WebView.

---

## First build: minimum viable tablet

To prove the channel works end-to-end before investing in real features:

1. Install or build a generic kiosk WebView APK on the tablet (one-time
   ~30 min, or use an existing F-Droid kiosk app to skip the build).
2. Write a ~50-line Python server on the Mac that serves a single HTML page
   with the three-block layout, all three blocks showing hardcoded content.
3. Run `adb reverse tcp:8080 tcp:8080` to bridge over USB.
4. Open the WebView app on the tablet → it shows the layout.
5. From the Mac terminal: `curl -X POST localhost:8080/state -d '{"focus":"reminders"}'`
   → the tablet swaps which block is on the left.
6. Wire one hotkey (`⌃⌥2`) to the same POST. Hit it → tablet updates.

If those six steps work, the entire tablet integration is solved as a
primitive. From then on every new tablet feature is just "render a different
HTML template from Python." We never touch the Android app again.

---

## Open questions (deferred)

These do not block the design but should be answered before the second pass:

1. **Hotkey daemon choice** — `skhd`, Hammerspoon, or a small Python daemon
   using `pynput`? Affects setup time and ergonomics. Decision can wait
   until we wire up the first hotkey.
2. **Log direction** — newest at top or bottom? Default verbose or quiet?
3. **Reminder generation** — what does Nexus actually look at to generate
   judgments like "Maria's reply has been sitting for 2 days"? This is a
   Nexus-core capability question, not a tablet question, and overlaps with
   the management/comms worktrees.
4. **Auto-promote rules** — exact triggers for when a non-focused block
   should grab focus automatically (failed task? approval needed? new
   reminder?). Define when we have real tasks running.

---

## Status

**Design only.** Not yet built. Captured here so the decisions don't get
lost while we focus on more urgent work first.

Validated as of 2026-04-09:
- Tablet is reachable via ADB (`R8YXB0ZNJTK`, authorized)
- AppleScript window control works on the Mac (Accessibility permission
  granted), so the rest of the screen-management primitives can proceed
- `adb reverse` is the chosen transport — no Wi-Fi pairing needed

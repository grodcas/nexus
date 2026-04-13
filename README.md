# Nexus

> Voice-driven operational agent for macOS. You talk; Nexus does.

**This README is the canonical entry point.** If you're a fresh Claude session
opening this repo for the first time, read this file before anything else,
then read [`JARVIS_GUIDE.md`](./JARVIS_GUIDE.md) for the voice-layer
engineering contract. Do not read the rest of the directory looking for
older specs — there are none. Earlier docs were deleted on 2026-04-12 because
they described products that were never built.

---

## What Nexus is, today

Nexus is a single Python process you run on macOS. It opens a microphone,
streams audio to Gemini Live (Google's real-time voice LLM), and exposes
exactly **one tool** that Gemini can call to act on the system: `do(action,
query, session?)`. All routing happens in Python. The "tool" is just a
dispatch table whose branches reach into the rest of the repo:

- **`browse` / `search`** → drives a persistent Playwright Chromium via an
  inner Claude subprocess (`voice/browser.py` + `voice/nav.py`).
- **`window`** → moves, resizes, snaps, and lists Mac windows via
  AppleScript (`scripts/screens.py`).
- **`calendar` / `email` / `reminders` / `briefing`** → reads pre-synced
  markdown files at `~/.nexus/management/` (`scripts/management/`).
- **`documents`** → grep over a pre-built worktree at `~/.nexus/documents/`.
- **`code`** → two-step handoff to Claude Code in voice mode
  (`voice/claude_mode.py`) for actual programming work.
- **`github`** → `gh` CLI subprocess.
- **`sleep`** → ends the session.

The user says *"browser, search images of drone lidar"* and Gemini Live
calls `do(action="browse", query="search images of drone lidar")`. Nexus
launches Chromium (or reuses the persistent profile), navigates, returns a
short summary, and Gemini speaks it. The browser stays visible on screen
the whole time — ghost-browsing — so the user can watch Nexus operate.

That's the whole product. Voice in → operation on the Mac → voice or
visible result out.

---

## What Nexus is NOT (and why this matters)

Earlier iterations of this repo described very different products. None of
them were built. **Do not start working on any of these unless the user
explicitly asks** — they were all considered and pivoted away from for
specific reasons.

| Phantom product | Why it's not the product |
|---|---|
| **Tauri desktop app with split-panel chat + file browser** | Was the original Stage 1 vision (~Feb 2026). Never built. The split panel was abandoned in favor of voice as the primary surface — typing a query and clicking through a panel is slower than just speaking. |
| **Microsoft Graph delta sync of OneDrive** | Was the planned data ingestion path. Replaced by local-filesystem reads against the OneDrive folder that's already synced to disk by the OS-native client. Cheaper, faster, no API tokens. |
| **python-docx + win32com Word editing** | Was the Stage 1 hero feature. Out of scope now. Word editing is not why a voice agent exists. |
| **Hierarchical markdown index built from scratch by an embedded Claude SDK** | The hierarchy concept survived in the form of the `~/.nexus/documents/` worktree, but the build path is completely different (a metadata parser, not Claude). See "External state" below. |
| **Samsung Galaxy Tab A9 as a glanceable operations console** | Aspirational. There is no tablet. There is no UI surface other than voice. |

If a fresh session reads outdated context (an old conversation, an external
brief, a memory entry from a different project), it may try to "continue"
one of these. **Stop and confirm with the user before doing any work that
isn't on the slim voice agent or its supporting tools.**

---

## Why the pivot

The pivot from "Word-editing Tauri app" to "voice-driven operational agent"
happened around **2026-04-06**. Captured in memory as `project_vision_v2`.
Short version of the rationale:

- The 90% logistics tax that motivated the original brief is real, but
  *typing into a desktop app* doesn't reduce it. *Talking* does. Voice
  removes the find-the-window, click-the-input, formulate-the-prose
  overhead.
- Anything that operates the Mac is a "hand" Nexus can borrow. Moving
  windows, driving a browser, reading mail — these are all the same
  pattern: voice → Python → AppleScript/Playwright/CLI. No need for a
  custom UI when macOS already has every UI you need.
- Segment-agnostic by design. The original brief was bolted to one user
  (a drone engineer at Xer Technologies, ~1TB OneDrive, Word-heavy
  workflow). The current product makes no assumptions about file types
  or workflow — `do(action, query)` works the same for any input.
- Real-time voice is now cheap and good enough (Gemini Live native audio,
  sub-second latency). It wasn't when the original brief was written.

---

## Branches

There are exactly three branches. Do not create more without a reason.

| Branch | Purpose | Entry point |
|---|---|---|
| **`master`** | Primary work. Slim voice agent. The only branch you develop in. | `voice/jarvis_slim.py` |
| **`jarvis-full`** | Frozen archive of the previous pipecat-based 9-tool version. **Do not develop here.** Reference only — and as a reminder of what went wrong (the slim file exists *because* the pipecat version threw `1008` and `1011` WebSocket errors under load). | `voice/jarvis.py` |
| **`gemini-bare`** | Frozen bare Gemini Live conversational reference (no tools, no logic). Use this to compare baseline behavior when slim feels off. *"Is this slim's fault or Gemini Live's fault?"* — switch to `gemini-bare`, run, and find out. | `voice/gemini_voice_raw.py` |

If you find yourself wanting to "port slim's improvements back to
jarvis-full" or "merge pipecat back into master to get a missing feature,"
**stop**. The pipecat version is preserved as-is intentionally. Slim is the
product.

---

## Repository map (master)

```
nexus/
├── README.md                      ← This file. The canonical entry point.
├── JARVIS_GUIDE.md                ← Engineering contract for jarvis_slim.py.
│                                    Read this before touching any voice code.
│
├── voice/                         ← The voice agent and its dependencies.
│   ├── jarvis_slim.py             ← THE primary file. Single do() tool, ~110-char
│   │                                system prompt, all routing in Python.
│   ├── claude_mode.py             ← Claude Code coding mode (STT/TTS loop).
│   │                                Reached via do(action="code", session=...).
│   ├── audio.py                   ← STT (faster-whisper) + TTS (Google Cloud)
│   │                                + cached acks. Used by claude_mode.
│   ├── browser.py                 ← Persistent Playwright Chromium server
│   │                                (Unix socket at ~/.nexus/browser.sock).
│   ├── nav.py                     ← Playwright client used by the inner Claude
│   │                                nav agent that handles browse/search.
│   ├── session_manager.py         ← Project + Claude session storage
│   │                                (~/.nexus/sessions.json).
│   └── test_layer2.py             ← Pre-existing tests, do not delete without
│                                    asking the user.
│
├── scripts/
│   ├── screens.py                 ← macOS window + display primitives via
│   │                                AppleScript and system_profiler. Exports
│   │                                list_windows, snap_window, raise_window,
│   │                                focus_app, list_displays, etc.
│   │                                Used by jarvis_slim's `window` action.
│   │
│   └── management/                ← Calendar / reminders / email sync.
│       ├── sync_all.py            ← Entry point. Runs the three syncers.
│       ├── sync_calendar.py       ← AppleScript over Calendar.app.
│       ├── sync_reminders.py      ← AppleScript over Reminders.app.
│       ├── sync_gmail.py          ← Gmail API (OAuth, today-filtered).
│       └── build_management.py    ← Renders raw JSON → markdown briefing
│                                    files at ~/.nexus/management/. Today-
│                                    filters email and caps each section so
│                                    the briefing fits in slim's tool result
│                                    budget.
│
└── venv/                          ← Python venv, gitignored. Recreate with
                                     pip install -r requirements.txt if missing.
```

External state lives outside the repo — see "External state" below.

---

## External state (`~/.nexus/`)

Everything Nexus persists across runs lives under `~/.nexus/`. The directory
is created by the various subsystems on demand. Do not check any of it into
git.

| Path | What it holds | Built by |
|---|---|---|
| `~/.nexus/projects.json` | Map of project name → absolute path. Read at slim startup to populate the `code` action's known projects. | Manual / `session_manager.py` |
| `~/.nexus/sessions.json` | Per-project last/previous Claude Code session IDs for the two-step `code` handoff. | `voice/session_manager.py` |
| `~/.nexus/documents/` | The "documents worktree" — a pre-built hierarchical markdown index over the user's OneDrive folder. ~25 markdown files covering 14,000+ entries. Read by slim's `documents` action via grep. | A separate metadata parser (PyMuPDF / docx / openpyxl / pptx) that reads the locally-synced OneDrive folder directly. **Local-first**: no Microsoft Graph API, no `find` over OneDrive, just `while-read+cat` with `P=5`. See the `reference_metadata_parser` and `reference_onedrive_access` memory entries for the pattern. |
| `~/.nexus/management/` | Pre-rendered markdown briefing files: `root.md`, `calendar.md`, `reminders.md`, `email.md`. Read by slim's `briefing`/`calendar`/`email`/`reminders` actions. | `scripts/management/sync_all.py` → `build_management.py`. Email is filtered to today only and capped at 400 chars to balance with calendar (~190) and reminders (~120). |
| `~/.nexus/playwright_profile/` | Persistent Chromium user-data directory. Cookies, login state, captcha-survival. | `voice/browser.py` on first browser call. |
| `~/.nexus/browser.sock` | Unix socket for the persistent browser server. | `voice/browser.py`. |

---

## Validated facts about the operational layers

The browser and screen layers were proved out on **2026-04-09** and have
not regressed. These facts are easy to forget and expensive to re-derive.

### Window management (`scripts/screens.py`)

- **AppleScript window geometry ops do NOT steal focus.** `set position`,
  `set size`, and the `snap_window` chain run silently — keyboard focus
  stays where the user had it. This was the make-or-break test for
  parallel operation. Validated twice (scrcpy + Playwright moves) while
  iTerm2 stayed focused.
- **Launching an app DOES steal focus.** Mitigation pattern: capture
  `screens.get_frontmost_app()` before launch, then `screens.focus_app(...)`
  immediately after the new window appears, then again after any geometry
  ops. ~200 ms visible flicker, no input redirection.
- **Moving a window without stealing focus uses `raise_window`** (AXRaise),
  not `focus_app` (which calls `activate`). The window comes to the front
  but does not become the keyboard target. This is what slim's `window`
  action calls after every move/snap/maximize.
- **Built-in Retina displays report physical pixels** in `system_profiler`.
  `screens.list_displays()` halves them when it sees "Retina" so the
  returned values match AppleScript coordinates. Don't double-correct.
- **macOS clamps window height** to fit the usable area of the target
  display. Resize requests above the display's logical height return a
  smaller value silently. Defensive clamping in callers if exact dimensions
  matter.
- **Accessibility permission is the #1 prerequisite.** Without it,
  `set position` / `set size` return `-1728 osascript is not allowed
  assistive access`. Already granted in this environment. New machines:
  System Settings → Privacy & Security → Accessibility → enable for
  Terminal / iTerm / whichever process spawns Python.

### Browser (`voice/browser.py`, `voice/nav.py`)

- **Playwright's headed Chromium runs as `Google Chrome for Testing`**, not
  `Chromium`. Substring `"chrome"` matches it via AppleScript. Don't add a
  hardcoded process list.
- **Persistent context is mandatory**, not optional. `pw.chromium.launch_
  persistent_context(user_data_dir=…)` survives across runs and sessions,
  keeps cookies and login state, and dodges captchas via real profile age.
  Without persistence the browser is unusable for anything that requires
  authentication. Profile lives at `~/.nexus/playwright_profile/`.
- **The browser stays visible by default.** "Ghost browsing" is the
  intended UX: the user can watch Nexus operate. Hiding it is a future
  feature, not the default.
- **The inner Claude nav agent is what drives Playwright**, not slim
  directly. Slim's `browse` handler builds a prompt and shells out to
  `claude --print --dangerously-skip-permissions`, which uses
  `voice/nav.py`'s primitives (`state`, `goto`, `click`, `type`, `press`,
  `scroll`). Slim itself does not know how to fill a form. The inner agent
  is told to **prefer direct URLs over clicking** when the site exposes a
  stable URL — but never with app-specific examples.
- **Browser vs. search engine are orthogonal.** Chromium = the program;
  Google / DuckDuckGo / etc = the website. They are not alternatives.
- **DuckDuckGo is the safest default search engine** for automated
  browsing: almost never throws captchas, no EU consent dialog, more
  stable HTML. Google is viable when the persistent profile is logged in.

### Voice agent (`voice/jarvis_slim.py`)

See [`JARVIS_GUIDE.md`](./JARVIS_GUIDE.md) for the eight hard rules. Most
important highlights:

1. **No pipecat. Ever.** Direct `google.genai` websocket. Pipecat threw
   `1008`/`1011` errors under real workloads.
2. **System prompt stays at ~110 chars.** Currently *"Be brief. Answer
   from your own knowledge first. Use the do tool only when the request
   needs an action."* Don't grow it.
3. **One unified `do(action, query, session?)` tool.** Adding a capability
   = a new `elif` branch in `handle_tool`, never a new tool.
4. **No `enum` on dynamic string parameters** (e.g. `enum=list(PROJECTS.
   keys())`). Gemini Live rejects them with `1008`. Surface allowed values
   in the parameter `description` instead.
5. **No app-specific hardcoding anywhere.** No "gmail / shopify / arc /
   chrome" example lists in any prompt or schema string. Use live system
   state (`screens.list_windows()`, etc.). This rule has been violated and
   reverted at least four times — see the JARVIS_GUIDE anti-patterns.
6. **Long-running tools must use the TTS bypass.** Return `(result,
   is_long=True)` so the result is spoken locally via `say` and Gemini
   gets `"Done. Already spoken to user."` instantly. Otherwise the audio
   backlog will trigger `1011` from Gemini Live during long blocks.

---

## Running Nexus

```bash
cd ~/nexus
source venv/bin/activate
python voice/jarvis_slim.py
```

Speak. The mic is hot from launch — there is no wake word yet (known
limitation, see `JARVIS_GUIDE.md`). Ctrl+C to stop.

The first call to a `browse`/`search` action launches the persistent
Chromium and parks it on screen. The first call to `briefing`/`calendar`/
`email`/`reminders` runs `sync_all.py` to refresh the underlying markdown
files. First call to `code` formats the available sessions for the named
project and reads them back; second call (with `session=last|previous|new`)
hands off to Claude mode.

Environment expectations:
- `.env` contains `GEMINI_API_KEY`.
- Google Cloud credentials for TTS are configured (used by `claude_mode`).
- Gmail OAuth is set up for `sync_gmail.py`.
- Accessibility permission granted to the parent terminal process.
- `claude` CLI installed and authenticated (used by `code` and `browse`).
- `gh` CLI installed and authenticated (used by `github`).

---

## Known limitations

These are documented to prevent re-discovery, not to suggest they need
fixing right now. See `JARVIS_GUIDE.md` for the full list.

- **No wake word.** Slim's mic is hot from launch. Background noise
  occasionally triggers Gemini to ask "what do you want?". The fix is to
  port openwakeword gating from the `jarvis-full` branch (it works there).
- **No keepalive on long tool calls.** When a tool blocks for >10s without
  progress, Gemini Live can drop with `1011`. Workarounds: use
  `is_long=True` to return instantly, or send periodic silent audio frames
  during the block.
- **PyAudio → sounddevice handoff is unverified across hardware.** Slim
  closes its PyAudio mic before calling `run_claude_mode`, which opens its
  own sounddevice mic. Works on the dev hardware. If `claude_mode`
  complains the device is busy on a different machine, add a
  `time.sleep(0.3)` between `mic.close()` and `run_claude_mode`.
- **`management.query` is honored only when present.** When Gemini calls
  `do(action='email')` with no query, the handler reads the full file.
  When it includes a query, we still read the full file — there's no
  per-question filtering on the data path. Fine for slim's current cap-
  based flow.

---

## How to extend Nexus

Read `JARVIS_GUIDE.md` first. The short version:

1. Decide the new `action` name. One word, lowercase.
2. Add it to the `action` parameter description's enum-style list in
   `jarvis_slim.py`.
3. Write a new `elif action == "yourname":` branch in `handle_tool`.
   Returns `(result_str, is_long_bool)`.
4. If the result is short and Gemini should summarize → `is_long=False`.
   If the result is long-form data the user wants spoken verbatim, or the
   work blocks for >5s → `is_long=True`.
5. Update `print_budget()`'s actions line so the startup banner is
   accurate.
6. **Do NOT touch the system prompt.** If the new action needs
   instructions Gemini wouldn't infer from the action name, those
   instructions belong in the `query` description hint, not in the prompt.
7. Test by running slim and asking naturally. If Gemini calls the wrong
   action, fix the descriptions, not the prompt.

Anything that needs reasoning, file reading, or multi-step recovery
belongs in an inner Claude subprocess (the pattern the `browse` action
already uses). Anything that's a single shell command or AppleScript call
belongs inline in `handle_tool`. Don't put complex logic in the slim file
itself — it should stay a dispatch table.

---

## File-deletion log (2026-04-12)

Removed during the documentation cleanup that produced this README. They
described products that were never built and were misleading any fresh
session that read them as authoritative.

| Deleted | What it described | Why deleted |
|---|---|---|
| `PROJECT_BRIEF.md` | Tauri desktop app + OneDrive Graph + python-docx Word editing | Pre-pivot phantom. Replaced by this README. |
| `STAGE1_SPEC.md` | Same Tauri app, with implementation detail | Pre-pivot phantom. Never built. |
| `INDEX_BUILD_SPEC.md` | Hierarchical index built by Claude SDK from Microsoft Graph crawl | The hierarchy concept survived as `~/.nexus/documents/` but the build path is now a metadata parser (see memory). The spec described a different runtime than what exists. |
| `TABLET_SPEC.md` | Samsung Galaxy Tab A9 mounted above the main monitor as a console | Aspirational. There is no tablet. There is no UI surface other than voice. |
| `SCREEN_BROWSER_SPEC.md` | Foundation spec for `scripts/screens.py` and Playwright | Foundation has been consumed into slim's `window` and `browse` actions. The validated facts from the spec are preserved in this README's "Validated facts about the operational layers" section. |

If you find yourself missing one of these, the genuinely useful content is
either in this README, in `JARVIS_GUIDE.md`, or recoverable from git
history (`git log --diff-filter=D -- PROJECT_BRIEF.md` etc).

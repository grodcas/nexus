# Jarvis — Engineering Guide

> The voice assistant. How it's built, what NOT to do, and how to extend it.
> **Read this top-to-bottom before touching `voice/jarvis_slim.py`.** The
> "Hard rules" and "Anti-patterns" sections are not opinions — every one of
> them is the result of a bug we already paid for.

---

## TL;DR

- **The primary file is `voice/jarvis_slim.py`.** That is the only voice
  assistant on `master`. Old versions live on archive branches; see
  "Branches" below.
- **No pipecat. Ever.** Direct `google.genai` websocket. Pipecat is on the
  `jarvis-full` archive branch as a cautionary tale.
- **One unified tool: `do(action, query, session?)`.** Slim does not have
  9 separate tools. All routing happens in Python via `handle_tool`'s
  `action` switch. Adding a new capability = adding an `action` branch,
  not a new tool.
- **The system prompt is ~110 chars and stays that way.** The schema and
  Python carry the routing intelligence; the prompt only enforces style
  and the "answer from your own knowledge first" priority.
- **Never hardcode app names, URLs, or product-specific examples** in any
  prompt or schema string. Use live system state instead. See the
  "no app hardcoding" rule below.

---

## Branches

| Branch | Purpose | Entry point |
|---|---|---|
| `master` | **Primary work.** Slim version. This is what you edit. | `voice/jarvis_slim.py` |
| `jarvis-full` | Archive of the old pipecat-based 9-tool version. Do not develop here. Reference only — and as a reminder of what went wrong. | `voice/jarvis.py` |
| `gemini-bare` | Bare Gemini Live conversational reference (no tools, no logic). Use this to compare baseline behavior when slim feels off ("is this slim's fault or Gemini Live's fault?"). | `voice/gemini_voice_raw.py` |

If you find yourself wanting to "port slim's improvements back to jarvis-full,"
**stop**. The pipecat version is preserved as-is intentionally.

---

## Architecture (slim)

```
mic ── pyaudio ──> Gemini Live (websocket, native VAD)
                          │
                          ├── speaks straight to speaker (pyaudio)
                          │
                          └── tool_call: do(action, query, session?)
                                  │
                                  └── Python handle_tool dispatches:
                                       browse / search   → inner Claude + Playwright (nav.py)
                                       calendar / email / reminders / briefing
                                                        → reads ~/.nexus/management/*.md
                                       documents        → grep over ~/.nexus/documents/
                                       github           → gh CLI subprocess
                                       window           → scripts/screens.py (AppleScript)
                                       code             → STAGES handoff to claude_mode
                                       sleep            → exits the session
```

### The `do(action, query, session?)` tool

- **One** `FunctionDeclaration`. Period.
- `action` is a free string the schema describes as an enum-style list:
  `browse, search, calendar, email, reminders, briefing, documents, code,
  github, window, sleep`. It is intentionally NOT a JSON-schema enum
  (see "Hard rules" #4).
- `query` carries everything else in the user's words. The schema's
  `description` is dynamically built at startup with the live `PROJECTS`
  list and a few generic shape examples — no app names.
- `session` is only meaningful for `action=code` and is `'last'`,
  `'previous'`, or `'new'`. Omitting it on a `code` call lists available
  sessions back to the user (the two-step coding handoff).

### The two-step `code` handoff

1. User: *"Connect to nexus."*
2. Gemini calls `do(action='code', query='nexus')` → handler returns
   the formatted session list, Gemini reads it.
3. User: *"Last one."*
4. Gemini calls `do(action='code', query='nexus', session='last')` →
   handler stages `_handoff` (project/session/path) and returns the
   goodbye line.
5. After ~2.5s grace, `receive()` returns. The async-with closes the
   websocket. PyAudio releases the mic. `await run_claude_mode(...)`
   takes over with `voice/claude_mode.py`'s sounddevice-based loop.
6. When the user says **"jarvis"** in Claude mode, `run_claude_mode`
   returns. The outer `while True` reopens PyAudio + Gemini and we're
   back to normal voice mode.

The Claude mode itself (subprocess streaming, session continuity,
notifications) was ported from `jarvis.py` *as-is* and works well.
Don't rewrite it.

### Long tool results — the TTS bypass

`handle_tool` returns `(result, is_long)`. When `is_long=True`:
- The result is spoken locally via macOS `say` (`tts_speak_long`).
- Gemini gets the literal string `"Done. Already spoken to user."`

This pattern keeps long blobs out of the Gemini Live websocket entirely
— zero token cost, zero "read raw data" risk, zero `1011` triggers from
audio backlog during long synchronous tools. Use it for any tool result
> ~300 chars that's meant to be spoken anyway.

---

## Hard rules

These are the things that will silently break Gemini Live or quietly
make slim worse. None are negotiable.

### 1. No pipecat

The `voice/jarvis.py` (pipecat) version threw `1008 Operation is not
implemented, or supported, or enabled` and `1011 Internal error` under
real workloads. Symptoms: robotic voice, garbled STT, reconnect loops.
Pipecat's serialization of FunctionDeclaration parameters is more
fragile than `google.genai`'s. The slim path uses raw
`google.genai.aio.live.connect` and is reliable.

If you're tempted to install pipecat to "get features faster" — **stop**.
The features come from writing Python, not from a framework.

### 2. The system prompt stays small (~110 chars)

Current prompt: *"Be brief. Answer from your own knowledge first. Use
the do tool only when the request needs an action."*

That's everything. No persona. No examples. No "rules:" list. No tool
routing instructions in prose. The schema and the `do` tool's
`description` carry everything else.

Adding even 200 more chars will:
- Bias Gemini toward whatever you wrote about more often.
- Make general-knowledge questions trigger tool calls ("tell me about
  the Airbus lineup" → suddenly Gemini calls `do(action=search)`
  because the prompt mentions actions a lot).
- Eat into Gemini Live's per-turn token budget on a real-time path.

If you think you need more system prompt, you don't. You need a better
schema description. See rule #4.

### 3. Tool description balance

The `do` tool description is `"Execute an actionable request."` — 30
chars. Not 150 chars listing every action you support. Gemini's
attention follows description mass: a long description for one tool (or
in slim's case, one parameter) makes Gemini reach for it on anything
that vaguely matches.

If a parameter description grows past ~250 chars, split it. If you need
to disambiguate, do it via schema-level enums and live data, not prose.

### 4. No `enum` on dynamic string parameters that go through Gemini Live

The Gemini Live function-call validator has bitten us specifically on
**dynamic string enums** (e.g. `enum=list(PROJECTS.keys())`). It rejects
the schema with WebSocket close code `1008` and the audio session goes
into a reconnect loop.

`action` is intentionally a free `STRING` whose description *looks* like
an enum but isn't. Static literal enums (`session: ['last', 'previous',
'new']`) on small parameters seem fine, but anything generated at startup
from project state must stay a free string. Surface the allowed values in
the parameter `description` instead — it costs slightly more chars but
the websocket stays alive.

### 5. No app-specific hardcoding, anywhere, ever

This is the most-broken rule in the project history. It came up at
least four times in one session. Forms it takes:

- ❌ "Available destinations: gmail, shopify, figma, youtube, github" in
  a tool description
- ❌ `if "gmail" in query: dest = "gmail"` in Python
- ❌ `BROWSER_HINTS = ("chrome", "safari", "arc", ...)` constant lists
- ❌ "Gmail spam = `mail.google.com/#spam`" in an inner agent prompt
- ❌ Any other product/site/app name as an example

Reasons it keeps happening: examples *feel* helpful, and copy-paste from
generic tutorials normalizes them. Reasons it's wrong:

- The user's vision is segment-agnostic ("nexus is an operational
  agent for any workflow"). Hardcoded examples bias the LLM toward
  those exact apps and make others second-class.
- Hardcoding rots. Apps rename, URLs change, products die.
- It defeats the whole point of having an LLM.

**The pattern that works instead:** query live system state.
- For window management: `screens.list_windows()` returns the actually
  open processes. Match the user's app token against that list. On a
  miss, return the live list back to Gemini and let it retry with a
  real name.
- For browser destinations: pass the user's query through to the inner
  Claude nav agent unchanged. Don't pre-translate "gmail" to anything.
  The inner agent reads page state and figures it out.
- For URL patterns: tell the inner nav agent to "prefer direct URLs
  when the site exposes a stable URL," not "Gmail spam is at #spam".

Exception: matching against process names that are likely substring
hits (e.g. "chrome" → "Google Chrome") via live `list_windows()` is
fine — the matching is structural, not a hardcoded list.

### 6. Long-running tools must use the TTS bypass

Anything that takes more than ~5 seconds and returns a string Gemini
will speak should set `is_long=True`. Reason: while Gemini is waiting
for the function response, mic audio keeps streaming into the Live
websocket. Past ~10s of backlog, Gemini Live drops the connection with
`1011 Internal error`. The TTS bypass returns `"Done. Already spoken
to user."` instantly so Gemini's flow control stays sane.

`browse`/`search` (browser nav) is the most common offender. It still
returns within the 1011 risk window most of the time, but the next
nav agent improvement should be marking it `is_long`.

### 7. Never edit `voice/jarvis.py` thinking it's slim

`voice/jarvis.py` does not exist on `master` anymore. It lives only on
the `jarvis-full` archive branch. If you somehow find yourself on a
branch where it does exist, *stop* and check `git branch` before
making any "small fix" — the entire pipecat machinery is sitting in
that file and will mislead you for hours.

### 8. The `_exit_to_jarvis` recursion bug

`voice/claude_mode.py:332` had a bug where `_exit_to_jarvis()` returned
a call to itself instead of the literal `"jarvis"` sentinel. It's
fixed (commit `41b4eda`), but if you ever see "maximum recursion depth
exceeded" out of `claude_mode`, look there first — it's the kind of
bug that can come back from a careless refactor.

---

## Anti-patterns (concrete examples of what we already broke)

1. **Adding "for browser, prefer X for image searches" to the system
   prompt.** Did this. Gemini began calling `navigate_browser` for
   every general question because the prompt's mass shifted toward
   browse routing.

2. **Adding `enum=list(PROJECTS.keys())` to a function declaration.**
   Did this. Gemini Live closed with `1008` immediately on session
   open. Reverted to descriptive string.

3. **Per-tool descriptions of 150+ chars listing 5 example use-cases
   each.** Did this. The biggest tool's attention dominated and Gemini
   ignored the others.

4. **Hardcoded `if "gmail" in query` keyword maps in Python tool
   handlers.** Did this. User correctly pointed out it's the same bias
   as in the prompt, just hidden.

5. **Editing `voice/jarvis.py` for ~5 hours believing it was the file
   the user was running.** They were running `voice/jarvis_slim.py`
   the whole time. **Always confirm the entry point before any session
   of edits.**

6. **Mixing `<` and `→` characters into Gemini configs.** Some
   serialization layer between Python and Gemini Live HTML-escapes or
   mis-encodes them under specific conditions. Stick to ASCII in
   anything that goes into `system_instruction` or schema descriptions
   if you want to debug fewer ghosts.

---

## Adding a new capability — checklist

1. **Decide the action name.** One word. Lowercase. Add it to the
   `action` parameter description's enum list.
2. **Write the handler branch.** New `elif action == "yourname":` in
   `handle_tool`. Returns `(result_str, is_long_bool)`.
3. **Decide `is_long`.** If the result is short and Gemini should
   summarize → `False`. If the result is long-form data the user wants
   spoken verbatim, or the work blocks for >5s → `True`.
4. **Update `print_budget`'s actions line** so the startup banner is
   accurate.
5. **Do NOT touch the system prompt.** If the new action needs
   instructions Gemini wouldn't infer from the action name, those
   instructions belong in the action's `query` description hint, not
   in the prompt.
6. **Test by running slim and asking naturally.** If Gemini calls the
   wrong action, fix the descriptions, not the prompt.

---

## Known limitations (still unfixed)

- **No wake word.** Slim's mic is hot from launch. Background noise
  occasionally triggers Gemini to ask "what do you want?". Fix is to
  port the openwakeword gating from `jarvis.py` (it's on the
  `jarvis-full` branch).
- **No keepalive on long tool calls.** When a tool blocks for >10s
  without progress, Gemini Live can drop with `1011`. Workarounds:
  use `is_long=True` to return instantly, or send periodic silent
  audio frames during the block.
- **`code` action assumes PyAudio releases the mic cleanly when its
  context exits.** This works on the dev hardware but isn't proven
  across all macOS audio configurations. If `claude_mode` complains
  the device is busy, add a `time.sleep(0.3)` between `mic.close()`
  and the `run_claude_mode` call.
- **`management.query` is honored only when present.** When Gemini
  calls `do(action='email')` with no query, the handler reads the
  full file. When it includes a query, we currently still read the
  full file — there's no per-question filtering on the email/calendar
  data path. Fine for slim's current cap-based flow, but worth
  knowing if you add one.

---

## Token / char budget (slim baseline)

Roughly:

| Component | Chars |
|---|---|
| System prompt | ~110 |
| `do` tool description | ~30 |
| `action` param description | ~115 |
| `query` param description (with project list) | ~280 |
| `session` param description | ~85 |
| **Total schema baseline** | **~620** |
| Gemini Live overhead | (out of our control) |

The `~900 chars total` claim in `e507e9f`'s commit message is from an
earlier slim with no `session` param and no window vocabulary. Current
baseline is closer to ~1,000 with the additions but is still ~7×
smaller than the pipecat `jarvis.py` ever was (~7,400 chars).

If the baseline ever creeps past 1,500, audit. The root cause will be
either rule #2 (system prompt growth) or rule #3 (description bloat).

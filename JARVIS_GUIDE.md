# Jarvis — Engineering Guide

> How the voice assistant works, how to add features, and where it's going.

---

## Processing Tiers

Jarvis has four processing tiers. Every feature uses one or more. Picking the wrong tier is the most common mistake.

### Tier: Gemini Live

**What it is:** Real-time voice LLM. Always listening, responds instantly.

**Strengths:** Sub-second latency. Native audio in/out. Can call tools. Conversational memory within a session.

**Limitations:** Cannot reason deeply. System prompt must stay under ~400 chars (tool instructions only — the tool schemas carry the weight). Large tool results (>3-4K chars) crash it. No vision. Forgets everything between sessions.

**When to use:** Routing. Gemini decides which tool to call based on what the user said. Tool handlers do the actual work — Gemini just dispatches and summarizes the result in spoken form.

**How to code for it:**
- Tool schema = the brain. Put clear `description`, specific `enum` values, descriptive parameter names. Gemini reads the schema to understand when and how to call the tool.
- System prompt = one line per tool, max. Only add a line if there's a non-obvious behavior (e.g., "browser" keyword always means navigate_browser). If the tool name and schema are self-explanatory, add nothing.
- Handler returns a short string. Gemini summarizes it to the user. Keep results under 3000 chars.
- Handler runs in `asyncio.to_thread()` if it does blocking work (subprocess, AppleScript, file I/O).

**Anti-patterns:**
- Adding examples to the system prompt ("move Chrome to the left" → manage_windows...). The schema already says this.
- Returning raw data. Gemini will try to read it verbatim. Summarize in the handler.
- Complex multi-step logic in the handler. If it needs reasoning, use Claude instead.

### Tier: Haiku

**What it is:** Fast, cheap Claude model for text analysis.

**Strengths:** Good at summarization, classification, extraction. Fast (~1-2s). Cheap.

**Limitations:** Cannot act on the system. No tool use. Limited reasoning depth.

**When to use:** Processing text that's too complex for string matching but doesn't need Claude-level reasoning. Currently used for: context trimming (summarizing old conversation), Claudia explainer (translating Claude Code output to plain English).

**How to code for it:**
- Call via Anthropic SDK directly (not subprocess).
- Single-shot: system prompt + one user message → one response.
- Keep prompts tight — Haiku follows instructions well but drifts on open-ended tasks.

### Tier: Claude Code (subprocess)

**What it is:** Full Claude in a subprocess with `--print` flag. Can read files, run commands, use tools.

**Strengths:** Can do anything. Reasons deeply. Has access to the filesystem, shell, and any CLI tool. Best for multi-step operations that require judgment.

**Limitations:** Slow (10-60s typical). Cannot talk — results must be relayed through Gemini or TTS. Expensive. Output must be capped before passing to Gemini.

**When to use:** Browser navigation (Claude controls nav.py). Coding (Claude mode). Any task that requires multiple steps, error recovery, or reading/writing files.

**How to code for it:**
- Spawn with `subprocess.Popen(["claude", "--print", ...])`.
- Use `--output-format stream-json` to parse progress events.
- Cap the final result (NAV_RESULT_CAP = 3000) before returning to Gemini.
- Set a timeout (90s for navigation, longer for coding).
- Run in `asyncio.to_thread()` since it blocks.

**Anti-patterns:**
- Using Claude for simple lookups. If AppleScript or a CLI command can do it in <1s, don't spawn Claude.
- Letting raw Claude output reach Gemini. Always cap and summarize.

### Tier: TTS / STT

**What it is:** Google Cloud TTS (Neural2-J voice) + faster-whisper for speech-to-text.

**Strengths:** High-quality voice. Whisper is accurate. Caching makes repeated phrases instant.

**Limitations:** TTS is one-way (speak, can't listen while speaking). STT requires silence detection (energy-based VAD). The record→transcribe→process→speak loop adds latency.

**When to use:** Claude mode (voice ↔ Claude Code). Any flow where Gemini Live isn't in the loop — e.g., direct voice commands that bypass Gemini entirely.

**How to code for it:**
- Use `audio.py` functions: `record_speech()`, `transcribe()`, `speak()`.
- The Claude mode handoff (connect_project → run_claude_mode) is the reference implementation.
- Pre-cache common phrases with `init_ack_cache()` for instant playback.

**Anti-patterns:**
- Building a TTS/STT loop when Gemini Live already handles the conversation. Gemini's native audio is always faster than record→transcribe→LLM→TTS.
- Only use TTS/STT when you need a processing tier that Gemini can't provide (Claude reasoning, Haiku analysis).

---

## Adding a New Feature — Decision Tree

```
User wants Jarvis to do X.

1. Can X be done with a single shell command, AppleScript call, or API request?
   YES → Gemini tool handler (subprocess/osascript, return result string)
   NO ↓

2. Does X require multi-step reasoning, error recovery, or file reading?
   YES → Claude Code subprocess (like navigate_browser)
   NO ↓

3. Does X require analyzing/summarizing text?
   YES → Haiku call in the handler
   NO ↓

4. Does X require a voice interaction loop outside Gemini?
   YES → TTS/STT flow (like Claude mode)
   NO → Probably a Gemini tool handler, keep it simple.
```

### Adding a Gemini tool — checklist

1. Write the async handler in `jarvis.py` (follow existing patterns)
2. Add tool schema to `TOOLS` list (clear name, description, enums, parameter descriptions)
3. Register with `llm.register_function()` in `run_pipeline_session()`
4. System prompt: add one line ONLY if there's non-obvious behavior. Otherwise add nothing.
5. Test by running Jarvis and asking for it naturally

---

## Current Capabilities (2026-04-12)

| Tool | Tier | What it does |
|------|------|-------------|
| connect_project | Gemini → Claude mode | Hands off to Claude Code for coding |
| list_sessions | Gemini | Shows coding sessions across projects |
| close_session | Gemini | Kills a Claude Code session |
| management | Gemini (subprocess) | Syncs + reads calendar, reminders, email |
| search_documents | Gemini | Full-text search of local document archive |
| github | Gemini (subprocess) | Recent repos and commits via `gh` CLI |
| sleep | Gemini | Ends voice session, returns to wake word |
| navigate_browser | Gemini → Claude Code | Browser automation via Playwright |
| manage_windows | Gemini (AppleScript) | Move, resize, close, snap windows |

---

## Roadmap — Planned Features

### Open files and programs
**Tier:** Gemini tool handler.
**How:** macOS `open` command handles everything — `open -a "Word" file.docx`, `open -a Spotify`, `open ~/Documents/report.pdf`. One handler, one subprocess call. Can combine with manage_windows to open AND position.

### Spotify / music control
**Tier:** Gemini tool handler (AppleScript).
**How:** Spotify on macOS has a full AppleScript dictionary. Play, pause, next, previous, current track, search, set volume — all via `osascript`. Same pattern as window management.

### Calendar write (add/remove events)
**Tier:** Gemini tool handler (AppleScript).
**How:** AppleScript can create and delete Calendar events. Same `osascript` approach as reading. Parameters: title, date, time, duration, calendar name.

### Reminders write (add/remove tasks)
**Tier:** Gemini tool handler (AppleScript).
**How:** AppleScript can create and complete Reminders items. Parameters: title, list, due date, notes.

### Browser takeover — "take over from where I am"
**Tier:** Gemini → Claude Code.
**How:** Like navigate_browser but context-aware. Instead of "go to kayak.com", it starts with `nav.py state` to read the current page, then continues from there. The user is already on a page and asks Jarvis to continue the task. Same architecture as navigate_browser — the prompt to Claude just says "read current state first, then accomplish the goal" instead of "navigate to destination."

### Screenshot advisor — "look at my screen"
**Tier:** Gemini → Claude Vision (or Haiku Vision).
**How:** Takes a screenshot of the frontmost app window (AppleScript `screencapture` targeting a window). Sends the image to Claude with the user's question. Returns spoken guidance. Cannot act — only advises. Useful for "what does this error mean", "where is the export button", "what am I looking at."

### Background watchers — "tell me when X happens"
**Tier:** Idle loop + Gemini notification injection.
**How:** User registers a watch condition ("email from Anthropic", "build finishes", "PR approved"). Stored in `~/.nexus/jarvis_state.json`. Checked on each idle cycle (already have the notification_monitor pattern from Claude mode). When triggered, Jarvis speaks up.

### Saved routines — "morning briefing" / "set up for coding"
**Tier:** Gemini tool handler.
**How:** Named sequences of tool calls stored in JSON. "morning" = sync management + read briefing + open browser to news + arrange windows. Gemini recognizes the routine name, handler executes the sequence. Keeps the system prompt clean — just one tool with a `routine_name` parameter.

### Context awareness — Jarvis knows what you're doing
**Tier:** Idle loop + state inference.
**How:** On each idle cycle, check: frontmost app, open windows, current calendar event, time of day. Build a one-line context string. Inject into Gemini's initial message on reconnect. "You're in VS Code working on nexus. Meeting in 20 minutes." No AI needed — just time + window state + calendar = context.
